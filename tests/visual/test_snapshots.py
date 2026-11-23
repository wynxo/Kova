"""Offline snapshots of the real web UI in headless Chromium."""

from __future__ import annotations

import json
import os
import platform
import re
import shutil
import socket
from io import BytesIO
from pathlib import Path
from threading import Thread
from time import monotonic, sleep

import httpx
import pytest
import uvicorn

from kova.client import KovaClient
from kova.config import Config, ModelEntry, save_config
from kova.server import create_app
from kova.theme_contrast import contrast_ratio
from kova.themes import theme_dir

pytestmark = pytest.mark.visual

ROOT = Path(__file__).resolve().parents[2]
BASELINES = Path(__file__).with_name("chromium-linux")
ARTIFACTS = ROOT / "test-results" / "visual"
EXAMPLE_THEME = "forest"
DEMO_REPLY = """# Setup

**A short introduction** with $x^2$ and `code`.

## Configure

```js:example.js
console.log("demo");
```

## Run

Start the example.
"""
UPDATE_SNAPSHOTS = os.environ.get("KOVA_UPDATE_SNAPSHOTS") == "1"
VISUAL_REQUIRED = os.environ.get("KOVA_VISUAL_REQUIRED") == "1"
PAGES = {
    "start": "/app",
    "gallery": "/app/gallery",
    "tokens": "/app/assets/token-sheet.html",
}


def unavailable(reason):
    if VISUAL_REQUIRED:
        pytest.fail(reason, pytrace=False)
    pytest.skip(reason)


@pytest.fixture(scope="module")
def chromium():
    if not (VISUAL_REQUIRED or UPDATE_SNAPSHOTS or os.environ.get("KOVA_VISUAL") == "1"):
        pytest.skip("Visual snapshots are opt-in; set KOVA_VISUAL=1.")
    if platform.system() != "Linux" or platform.machine() != "x86_64":
        unavailable("Visual baselines use Linux x86_64 Chromium; see CONTRIBUTING.md.")
    try:
        from playwright.sync_api import Error, sync_playwright
    except ImportError:
        unavailable("Install the optional visual group: uv sync --group visual.")

    with sync_playwright() as playwright:
        try:
            browser = playwright.chromium.launch()
        except Error as exc:
            if "Executable doesn't exist" in str(exc) or "Host system is missing dependencies" in str(exc):
                unavailable("Install Chromium with: uv run --group visual playwright install chromium.")
            raise
        try:
            yield browser
        finally:
            browser.close()


@pytest.fixture(scope="module")
def kova(chromium, tmp_path_factory):
    home = tmp_path_factory.mktemp("visual-kova")
    themes = theme_dir(home)
    themes.mkdir()
    shutil.copyfile(ROOT / "design" / "themes" / f"{EXAMPLE_THEME}.json",
                    themes / f"{EXAMPLE_THEME}.json")

    def model_stub(request):
        if request.method == "GET":
            assert request.url.path == "/v1/models"
            return httpx.Response(200, json={"data": [{"id": "demo-model"}]})
        assert request.url.path == "/v1/chat/completions"
        if json.loads(request.content)["model"] == "demo-research":
            chunks = [
                {"reasoning_content": "Separate the observed behavior from assumptions."},
                {"content": "**Start with a small, repeatable check.**\n\n"
                            "Run the same example twice and note what changes. "
                            "Then compare it against a known good result."},
            ]
            stream = "".join(f"data: {json.dumps({'choices': [{'delta': chunk}]})}\n\n"
                             for chunk in chunks)
            return httpx.Response(200, text=stream + "data: [DONE]\n\n",
                                  headers={"content-type": "text/event-stream"})
        delta = json.dumps({"choices": [{"delta": {"content": DEMO_REPLY}}]})
        return httpx.Response(200, text=f"data: {delta}\n\ndata: [DONE]\n\n",
                              headers={"content-type": "text/event-stream"})

    with pytest.MonkeyPatch.context() as monkeypatch, socket.socket() as listener:
        monkeypatch.setenv("KOVA_HOME", str(home))
        monkeypatch.setenv("KOVA_GALLERY", "1")
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
        config = Config(home=home, memory_path=home / "memory", files_path=home / "files",
                        port=port, active="demo", persona_configured=True,
                        models=[ModelEntry("demo", "http://localhost/v1", "demo-model"),
                                ModelEntry("research", "http://localhost/v1", "demo-research")])
        save_config(config)
        app = create_app(config, transport=httpx.MockTransport(model_stub))
        server = uvicorn.Server(uvicorn.Config(app, log_level="error", timeout_graceful_shutdown=5))
        thread = Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
        thread.start()
        try:
            deadline = monotonic() + 10
            while not server.started and thread.is_alive() and monotonic() < deadline:
                sleep(0.01)
            assert server.started, "Snapshot server did not start"
            url = f"http://127.0.0.1:{port}"
            with httpx.Client(base_url=url, timeout=5, trust_env=False) as http:
                yield KovaClient(url, app.state.token, http)
        finally:
            server.should_exit = True
            thread.join(10)
            assert not thread.is_alive(), "Snapshot server did not stop"


def open_screen(page, kova, screen, theme):
    from playwright.sync_api import expect

    url = kova.base_url + PAGES[screen]
    custom = kova.theme(theme) if theme == EXAMPLE_THEME else None
    appearance = custom["appearance"] if custom else theme
    root = page.locator("html")
    if screen == "start":
        kova.set_preferences({"theme": theme})
        # Paint the built-in first; the app reads the installed choice once signed in.
        page.add_init_script(f"localStorage.setItem('kova-theme', '{appearance}')")
        with page.expect_response(kova.base_url + "/events") as events:
            with page.expect_response(kova.base_url + "/app/login") as login:
                assert page.goto(f"{url}#ticket={kova.app_ticket()}").ok
        assert login.value.ok, "One-use browser login failed"
        assert events.value.ok, "The empty start screen did not connect to Kova"
        # The app focuses the composer after loading models, history and health.
        expect(page.locator("#prompt")).to_be_focused()
        expect(page.locator("body.is-empty")).to_be_visible()
        if custom:
            expect(root).to_have_css("--accent-default", custom["tokens"]["accent.default"])
    else:
        assert page.goto(url).ok
        if custom:
            # These standalone references offer only built-in toggles. Paint the
            # installed theme's server-resolved roles before the toggle renders.
            root.evaluate("""(root, tokens) => {
                for (const [name, value] of Object.entries(tokens)) {
                    root.style.setProperty(`--${name.replace('.', '-')}`, value);
                }
            }""", custom["tokens"])
        attr = "data-theme-value" if screen == "gallery" else "data-mode"
        toggle = page.locator(f'button[{attr}="{appearance}"]')
        toggle.dispatch_event("click")
        expect(toggle).to_have_attribute("aria-pressed", "true")
        if screen == "tokens":
            expect(page.locator("#pairs tr").first).to_be_visible()
        if screen == "gallery":
            sample = page.locator("[data-markdown-example]")
            expect(sample.locator("math")).to_have_count(2, timeout=12_000)
            expect(sample.locator(".callout")).to_have_count(5)
            expect(sample.locator(".code-filename")).to_have_text("example.js")
            expect(sample.locator(".markdown-diagram")).to_have_attribute(
                "data-diagram-theme", appearance, timeout=12_000)
            page.wait_for_function("document.querySelector('.markdown-diagram')?.naturalWidth > 0")

    expect(root).to_have_attribute("data-theme", appearance)
    root.evaluate("root => { root.dataset.motion = 'off'; }")
    page.evaluate("() => document.fonts.ready")
    page.mouse.move(-1, -1)


def compare_snapshot(image, name, max_changed):
    from PIL import Image, ImageChops, ImageOps

    baseline = BASELINES / f"{name}.png"
    if UPDATE_SNAPSHOTS:
        baseline.parent.mkdir(parents=True, exist_ok=True)
        image.save(baseline, optimize=True)
        return

    output = ARTIFACTS / name
    if not baseline.exists():
        output.mkdir(parents=True, exist_ok=True)
        image.save(output / "actual.png")
        pytest.fail(f"Missing baseline: {baseline.name}. See CONTRIBUTING.md to generate it.")

    with Image.open(baseline) as png:
        expected = png.convert("RGB")
    same_size = image.size == expected.size
    if same_size:
        red, green, blue = ImageChops.difference(expected, image).split()
        delta = ImageChops.lighter(ImageChops.lighter(red, green), blue)
        # Ignore small channel noise on composite screens; token colours are exact.
        mask = delta.point(lambda value: 255 if value > (8 if max_changed else 0) else 0, mode="1")
        changed = mask.histogram()[255]
        if changed <= max_changed:
            return
        reason = f"{changed} changed pixels (allowed {max_changed})"
        diff = Image.composite(Image.new("RGB", image.size, "magenta"), ImageOps.grayscale(expected).convert("RGB"), mask)
    else:
        reason = f"size changed from {expected.size} to {image.size}"
        # A resize invalidates pixel alignment. Show both frames side by side.
        diff = Image.new("RGB", (expected.width + image.width, max(expected.height, image.height)), "magenta")
        diff.paste(expected, (0, 0))
        diff.paste(image, (expected.width, 0))

    output.mkdir(parents=True, exist_ok=True)
    expected.save(output / "expected.png")
    image.save(output / "actual.png")
    diff.save(output / "diff.png")
    pytest.fail(f"{name}: {reason}. See test-results/visual/{name}/.")


@pytest.mark.parametrize("screen", PAGES)
@pytest.mark.parametrize("theme", ["dark", "light", EXAMPLE_THEME])
@pytest.mark.parametrize("width,height", [(390, 844), (1280, 900)], ids=["phone", "desktop"])
def test_visual_snapshot(chromium, kova, screen, theme, width, height):
    try:
        from PIL import Image
    except ImportError:
        unavailable("Install the optional visual group: uv sync --group visual.")

    context = chromium.new_context(viewport={"width": width, "height": height}, device_scale_factor=1,
                                   locale="en-US", timezone_id="UTC", color_scheme="dark",
                                   reduced_motion="reduce", service_workers="block")
    try:
        page = context.new_page()
        open_screen(page, kova, screen, theme)
        screenshot = page.screenshot(full_page=screen != "start", animations="disabled", caret="hide")
    finally:
        context.close()
    with Image.open(BytesIO(screenshot)) as png:
        image = png.convert("RGB")
    image.info.clear()  # Never carry capture metadata into baselines or artifacts.
    compare_snapshot(image, f"{screen}-{theme}-{width}", max_changed=0 if screen == "tokens" else 40)


def test_bot_workspace_keeps_histories_separate(chromium, kova):
    from playwright.sync_api import expect

    kova.bot_create({"id": "workspace-bot", "name": "Workspace Bot"})
    normal = kova.create_chat("Regular conversation")
    direct = kova.create_chat("Bot conversation", bot_id="workspace-bot")
    group = kova._request("POST", "/chats", json={
        "title": "Team conversation", "members": ["workspace-bot"],
    }).json()
    try:
        with chromium.new_context(viewport={"width": 1280, "height": 900},
                                  service_workers="block") as context:
            page = context.new_page()
            open_screen(page, kova, "start", "dark")
            history = page.locator("#chat-list")
            expect(history).to_contain_text("Regular conversation")
            expect(history).not_to_contain_text("Bot conversation")
            expect(history).not_to_contain_text("Team conversation")
            history.get_by_role("button", name="Regular conversation", exact=True).click()
            expect(page.locator("#chat-title")).to_have_text("Regular conversation")

            page.locator("#app-switch").click()
            page.get_by_role("menuitemradio", name=re.compile(r"^Kova Bots")).click()
            expect(page.locator("#bot-chat-header")).to_contain_text("Workspace Bot")
            expect(history).not_to_contain_text("Regular conversation")
            expect(history.get_by_role("button", name="Workspace Bot", exact=True)).to_be_visible()
            expect(history).to_contain_text("Team conversation")
            history.get_by_role("button", name="Team conversation", exact=True).click()
            expect(page.locator("#chat-title")).to_have_text("Team conversation")
            expect(page.locator("#app-switch-label")).to_have_text("Kova Bots")
            history.get_by_role("button", name="Workspace Bot", exact=True).click()
            expect(page.locator("#bot-chat-header")).to_be_visible()
            expect(page.locator("#bot-chat-header")).to_contain_text("Workspace Bot")
            page.get_by_role("button", name="Work panel").click()
            panel = page.locator("#side-panel")
            expect(panel).to_be_visible()
            expect(panel.get_by_text("Tool calls and approvals will appear here")).to_be_visible()
            panel.get_by_role("button", name="Files", exact=True).click()
            expect(panel.get_by_text("Files attached or changed in this thread will appear here")).to_be_visible()
            page.get_by_role("button", name="Open bot").click()
            expect(history).to_be_hidden()
            page.get_by_role("button", name="Back to inbox").click()
            expect(history).to_be_visible()

            page.locator("#app-switch").click()
            page.get_by_role("menuitemradio", name=re.compile(r"^Kova Chat")).click()
            expect(page.locator("#bot-chat-header")).to_be_hidden()
            expect(history).to_contain_text("Regular conversation")
            expect(history).not_to_contain_text("Bot conversation")
    finally:
        for chat in (normal, direct, group):
            kova.delete_chat(chat["id"])
        kova.bot_delete("workspace-bot")


@pytest.mark.parametrize("theme", ["dark", "light"])
@pytest.mark.parametrize("width,height", [(390, 844), (1280, 900)], ids=["phone", "desktop"])
def test_bot_contact_snapshots(chromium, kova, theme, width, height):
    from PIL import Image, ImageDraw
    from playwright.sync_api import expect

    def snapshot(page, name):
        page.mouse.move(-1, -1)
        # The stub's wall-clock speed varies in both usage readouts.
        page.add_style_tag(content="#status-bar-speed { display: none !important; }")
        page.evaluate(r"""async () => {
            for (const meter of document.querySelectorAll('.usage-meter')) {
                meter.textContent = meter.textContent.replace(/ · [\d.]+ tok\/s$/, '');
            }
            for (const time of document.querySelectorAll('.bot-contact-time')) {
                time.textContent = '10:24 AM';
            }
            document.activeElement.blur();
            await document.fonts.ready;
            await Promise.all([...document.images].map(image => image.decode().catch(() => {})));
        }""")
        with Image.open(BytesIO(page.screenshot(animations="disabled", caret="hide"))) as capture:
            image = capture.convert("RGB")
        image.info.clear()
        compare_snapshot(image, f"bot-{name}-{theme}-{width}", max_changed=40)

    kova.bot_create({"id": "scribe", "name": "Scribe", "avatar": {"icon": "message-square"}})
    try:
        with chromium.new_context(viewport={"width": width, "height": height}, device_scale_factor=1,
                                  locale="en-US", timezone_id="UTC", color_scheme=theme,
                                  reduced_motion="reduce", service_workers="block") as context:
            page = context.new_page()
            page.set_default_timeout(8_000)
            errors = []
            page.on("pageerror", lambda error: errors.append(error.message))
            open_screen(page, kova, "start", theme)
            menu = page.get_by_role("button", name="Toggle sidebar")
            contacts = page.locator(".bot-contacts")
            if width == 390:
                menu.click()
            # Contacts live in the Kova Bots app (the switch at the top of the list).
            page.locator("#app-switch").click()
            page.get_by_role("menuitemradio", name=re.compile(r"^Kova Bots")).click()
            contact = contacts.get_by_role("button", name="Scribe", exact=True)
            expect(contact).to_be_visible()
            colours = contact.evaluate("""row => [
                getComputedStyle(row.querySelector('.bot-contact-preview')).color,
                getComputedStyle(row.closest('#sidebar')).backgroundColor,
            ]""")
            assert contrast_ratio(*colours) >= 4.5
            snapshot(page, "inbox")
            page.locator("#new-workspace-bot").click()
            editor = page.get_by_role("region", name="New bot")
            expect(editor.get_by_role("heading", name="New bot")).to_be_focused()
            editor.get_by_label("Name", exact=True).fill("Atlas")
            editor.get_by_label("Description", exact=True).fill("Plans small experiments.")
            expect(editor.get_by_label("Handle")).to_have_value("atlas")
            editor.get_by_role("textbox", name="Persona", exact=True).fill(
                "Help plan small experiments. State assumptions, suggest one check, and keep replies short.")
            editor.get_by_label("Model", exact=True).select_option("research")
            expect(editor.get_by_label("Enable tools")).to_be_checked()
            expect(editor.get_by_label("Approval limit")).to_have_value("manual")
            # Measure the rendered roles, including the small avatar initial
            # and the input boundary against the actual page it sits on.
            pairs = editor.evaluate("""editor => {
                const styles = selector => getComputedStyle(editor.querySelector(selector));
                const input = styles('#bot-name'), mark = styles('#bot-preview .bot-avatar');
                const page = getComputedStyle(editor.parentElement).backgroundColor;
                // A bot with no icon or picture shows its character face, not a letter on a tile.
                const letter = !editor.querySelector('#bot-preview .bot-avatar.has-character');
                return [
                    [input.color, input.backgroundColor, 4.5],
                    ...(letter ? [[mark.color, mark.backgroundColor, 4.5]] : []),
                    [styles('.bot-photo-actions .bot-note').color, page, 4.5],
                    [input.borderColor, page, 3],
                ];
            }""")
            for foreground, background, minimum in pairs:
                assert contrast_ratio(foreground, background) >= minimum
            snapshot(page, "editor")
            editor.get_by_label("Approval limit").scroll_into_view_if_needed()
            expect(editor.get_by_label("Approval limit")).to_be_in_viewport()

            photo = BytesIO()
            tile = Image.new("RGB", (64, 64), "#405b71")
            line = ImageDraw.Draw(tile)
            line.line([(10, 44), (27, 25), (38, 38), (46, 30), (55, 44)], fill="#ececec", width=4)
            line.ellipse((41, 15, 49, 23), fill="#ececec")
            tile.save(photo, "PNG")
            page.locator("#bot-image").set_input_files(
                {"name": "atlas.png", "mimeType": "image/png", "buffer": photo.getvalue()})
            expect(editor.get_by_role("button", name="Remove image")).to_be_visible()
            editor.get_by_role("button", name="Create bot").click()
            expect(editor).to_be_hidden()
            header = page.locator("#bot-chat-header")
            expect(header.locator(".bot-chat-name")).to_have_text("Atlas")
            expect(header.locator(".bot-chat-model")).to_have_text("Plans small experiments.")
            expect(header.locator("img")).to_be_visible()
            page.wait_for_function("document.querySelector('#bot-chat-header img')?.naturalWidth > 0")

            prompt = page.get_by_role("textbox", name="Message Atlas")
            prompt.fill("How should I check a new idea?")
            prompt.press("Enter")
            reply = page.locator(".message.assistant")
            expect(reply.locator(".bot-author-name")).to_have_text("Atlas")
            expect(reply.locator(".reasoning")).to_be_visible()
            expect(reply.get_by_text("Start with a small, repeatable check.")).to_be_visible()
            expect(page.get_by_role("button", name="Stop the reply")).to_be_hidden()
            [chat] = [chat for chat in kova.list_chats() if chat["bot_id"] == "atlas"]
            saved = kova.get_chat(chat["id"])
            assert any(message.get("author") == "atlas" for message in saved["messages"])
            if width == 390:
                snapshot(page, "trace")
                menu.click()
            expect(contacts.get_by_role("button", name="Atlas", exact=True)).to_be_visible()
            # The title arrives after the reply; don't capture the placeholder.
            expect(page.locator("#chat-list")).to_contain_text("Start with a small, repeatable check.")
            snapshot(page, "contacts")

            # An API-side edit reaches the open header without a reload. The
            # existing reply keeps the speaker name stored with its messages.
            kova.bot_update("atlas", {"name": "Field notes"})
            expect(header.locator(".bot-chat-name")).to_have_text("Field notes")
            contacts.get_by_role("button", name="Field notes", exact=True).click()
            expect(reply.locator(".bot-author-name")).to_have_text("Atlas")
            assert len([chat for chat in kova.list_chats() if chat["bot_id"] == "atlas"]) == 1

            header.get_by_role("button", name="Edit bot").click()
            editor = page.get_by_role("region", name="Edit bot")
            expect(editor.get_by_label("Name", exact=True)).to_have_value("Field notes")
            editor.get_by_label("Enable tools").uncheck()
            editor.get_by_label("Approval limit").select_option("auto")
            editor.get_by_role("button", name="Remove image").click()
            editor.get_by_role("button", name="Save changes").click()
            expect(editor).to_be_hidden()
            expect(header.locator("img")).to_have_count(0)
            bot = kova.bot_get("atlas")
            assert bot["description"] == "Plans small experiments."
            assert bot["ceiling"] == "auto"
            assert "*" in bot["definition"]["permissions"]["deny"]
            assert bot["image"] is None
            if width == 390:
                menu.click()
            contacts.get_by_label("Actions for Field notes").click()
            contacts.get_by_role("button", name="New chat", exact=True).click()
            expect(page.get_by_role("heading", name="Chat with Field notes")).to_be_visible()
            assert len([chat for chat in kova.list_chats() if chat["bot_id"] == "atlas"]) == 2
            assert not errors, errors
    finally:
        for chat in kova.list_chats():
            if chat["bot_id"] == "atlas":
                kova.delete_chat(chat["id"])
        kova.bot_delete("atlas")
        kova.bot_delete("scribe")


def test_chat_layouts_reply_outline_and_zen_shortcut(chromium, kova):
    from playwright.sync_api import expect

    context = chromium.new_context(viewport={"width": 1280, "height": 900},
                                   permissions=["clipboard-read", "clipboard-write"],
                                   service_workers="block")
    try:
        page = context.new_page()
        open_screen(page, kova, "start", "dark")
        page.locator("#prompt").fill("Show the example")
        page.locator("#prompt").press("Enter")
        reply = page.locator("#messages .assistant")
        expect(reply.locator("math")).to_be_visible()
        expect(reply.get_by_text("example.js", exact=True)).to_be_visible()
        reply.get_by_role("button", name="Copy as plain text").click()
        copied = page.evaluate("navigator.clipboard.readText()")
        assert "A short introduction" in copied and "**" not in copied and "Wrap" not in copied

        reply.get_by_role("button", name="Open reply outline").click()
        panel = page.get_by_role("complementary", name="Reply outline")
        panel.get_by_role("button", name="Configure").click()
        expect(reply.get_by_role("heading", name="Configure")).to_be_focused()
        page.locator("#side-panel").get_by_role("button", name="Hide panel").click()

        page.get_by_role("button", name="Settings", exact=True).click()
        page.get_by_role("tab", name="Chat", exact=True).click()
        page.get_by_role("radio", name="Bubbles", exact=True).click()
        expect(page.locator("html")).to_have_attribute("data-message-style", "bubbles")
        page.get_by_role("radio", name="Compact", exact=True).click()
        expect(page.locator("html")).to_have_attribute("data-message-style", "compact")
        page.get_by_role("radio", name="Always", exact=True).click()
        page.get_by_role("button", name="Enter: Zen mode").click()
        expect(page.locator(".topbar")).to_be_hidden()
        expect(page.locator("#sidebar")).to_be_hidden()
        expect(page.locator("#status-bar")).to_be_hidden()
        expect(reply.locator("time")).to_be_visible()
        page.keyboard.press("Control+Alt+z")
        expect(page.locator(".topbar")).to_be_visible()
    finally:
        context.close()
        kova.set_preferences({"message_style": "theme", "message_timestamps": "never"})


def test_phone_long_press_sheet_keeps_actions_keyboard_reachable(chromium, kova):
    from playwright.sync_api import expect

    context = chromium.new_context(viewport={"width": 390, "height": 844}, has_touch=True,
                                   is_mobile=True, forced_colors="active", service_workers="block")
    try:
        page = context.new_page()
        open_screen(page, kova, "start", "light")
        page.locator("#prompt").fill("Show the example")
        page.locator("#prompt").press("Enter")
        reply = page.locator("#messages .assistant")
        trigger = reply.get_by_role("button", name="Open message actions")
        expect(trigger).to_be_visible()
        reply.locator(".markdown p").first.dispatch_event("pointerdown", {
            "pointerType": "touch", "isPrimary": True, "clientX": 30, "clientY": 40,
        })
        sheet = page.get_by_role("dialog", name="Message actions")
        expect(sheet.get_by_role("button", name="Copy as plain text")).to_be_visible()
        page.keyboard.press("Escape")
        expect(sheet).to_be_hidden()
        expect(trigger).to_be_focused()
        trigger.press("Enter")
        expect(sheet.get_by_role("button", name="Branch from here")).to_be_visible()
    finally:
        context.close()


def test_saved_chat_style_overrides_theme_defaults_on_first_login(chromium, kova):
    from playwright.sync_api import expect

    response = kova._request("POST", "/themes", json={
        "id": "chat-defaults", "name": "Chat defaults", "version": "1.0.0", "appearance": "light",
        "tokens": {"chat": {"width": "wide", "avatars": "off", "message-style": "bubbles",
                            "zen": "on", "bubble-shape": "sharp"}},
    })
    assert response.status_code == 201
    kova.set_preferences({"theme": "chat-defaults", "message_style": "compact"})
    context = chromium.new_context(service_workers="block")
    try:
        page = context.new_page()
        page.goto(f"{kova.base_url}/app#ticket={kova.app_ticket()}")
        root = page.locator("html")
        for name, value in {"chat-width": "wide", "avatars": "off", "message-style": "compact",
                            "bubble-shape": "sharp"}.items():
            expect(root).to_have_attribute(f"data-{name}", value)
        expect(page.locator(".topbar")).to_be_hidden()
        page.get_by_role("button", name="Exit zen mode").click()
        expect(page.locator(".topbar")).to_be_visible()
    finally:
        context.close()
        kova.set_preferences({"theme": "system", "message_style": "theme"})


def test_simple_theme_exports_and_reimports_by_drop(chromium, kova):
    from playwright.sync_api import expect

    theme_id = "roundtrip-demo"
    previous = kova.preferences()
    viewport = {"width": 390, "height": 844}

    def open_editor(page):
        page.keyboard.press("Control+k")
        page.locator("#command-palette-input").fill("Customize theme")
        page.keyboard.press("Enter")
        panel = page.locator(".theme-editor")
        expect(panel.locator("#theme-editor-tab-simple")).to_have_attribute("aria-selected", "true")
        expect(panel.get_by_role("button", name="Export", exact=True)).to_be_enabled()
        return panel

    def export(page, panel):
        with page.expect_download() as download:
            panel.get_by_role("button", name="Export", exact=True).click()
        return json.loads(download.value.path().read_text())

    try:
        with chromium.new_context(viewport=viewport, service_workers="block") as context:
            page = context.new_page()
            open_screen(page, kova, "start", "dark")
            root = page.locator("html")
            panel = open_editor(page)
            panel.locator("#theme-editor-name").fill("Roundtrip demo")
            panel.locator("#theme-editor-id").fill(theme_id)
            panel.locator("#theme-editor-simple-accent-hex").fill("#2dd4bf")
            expect(root).to_have_css("--accent-default", "#2dd4bf")
            base = root.evaluate("el => getComputedStyle(el).getPropertyValue('--bg-base').trim()")
            panel.get_by_role("radio", name="Warm").click()
            expect(root).not_to_have_css("--bg-base", base)
            ink = root.evaluate("el => getComputedStyle(el).getPropertyValue('--fg-default').trim()")
            panel.locator("#theme-editor-contrast").press("End")
            expect(root).not_to_have_css("--fg-default", ink)
            panel.locator("#theme-editor-radius").press("End")
            panel.locator("#theme-editor-density-compact").click()
            panel.locator("#theme-editor-font-mono").click()
            expect(root).to_have_attribute("data-radius", "round")
            expect(root).to_have_attribute("data-density", "compact")
            expect(root).to_have_attribute("data-font", "mono")
            document = export(page, panel)
            panel.get_by_role("button", name="Cancel", exact=True).click()
            expect(panel).to_be_hidden()

        with chromium.new_context(viewport=viewport, service_workers="block") as context:
            page = context.new_page()
            open_screen(page, kova, "start", "dark")
            panel = open_editor(page)
            transfer = page.evaluate_handle("""document => {
                const transfer = new DataTransfer();
                transfer.items.add(new File([JSON.stringify(document)], 'demo.json', {
                    type: 'application/json'
                }));
                return transfer;
            }""", document)
            panel.dispatch_event("drop", {"dataTransfer": transfer})
            expect(panel.locator("#theme-editor-id")).to_have_value(theme_id)
            expect(page.locator("html")).to_have_css("--accent-default", "#2dd4bf")
            assert export(page, panel) == document
            panel.get_by_role("button", name="Save", exact=True).click()
            expect(panel).to_be_hidden()
            assert kova.preferences()["theme"] == theme_id
            assert kova.theme(theme_id)["tokens"]["accent.default"] == "#2dd4bf"
            page.reload()
            expect(page.locator("html")).to_have_css("--accent-default", "#2dd4bf")
    finally:
        kova.set_preferences({key: previous[key] for key in ("theme", "radius", "density", "font")})
        if any(option["id"] == theme_id for option in kova.themes()):
            kova.remove_theme(theme_id)


@pytest.fixture
def backdrop_theme(kova):
    previous = kova.preferences()
    kova.save_theme({
        "id": "backdrop",
        "name": "Backdrop",
        "version": "1.0.0",
        "appearance": "dark",
        "extends": "kova-dark",
        "background": {"kind": "gradient", "stops": ["#14121c", "#1c1a26"]},
    })
    kova.set_preferences({"theme": "backdrop", "backgrounds": "on", "motion": "full"})
    try:
        yield
    finally:
        kova.set_preferences({key: previous[key] for key in ("theme", "backgrounds", "motion")})
        kova.remove_theme("backdrop")


def assert_solid_panels(page, *, forced_colors=False, backgrounds_off=False):
    from playwright.sync_api import expect

    sidebar_token = "--bg-base" if backgrounds_off else "--bg-surface"
    for selector, token in [("#sidebar", sidebar_token), ("main", "--bg-base"),
                            (".composer", "--bg-surface")]:
        panel = page.locator(selector)
        expected = panel.evaluate("""(node, token) => {
            const probe = document.createElement('span');
            probe.style.backgroundColor = token ? 'var(' + token + ')' : 'Canvas';
            node.append(probe);
            const color = getComputedStyle(probe).backgroundColor;
            probe.remove();
            return color;
        }""", None if forced_colors else token)
        expect(panel).to_have_css("backdrop-filter", "none")
        expect(panel).to_have_css("background-color", expected)


@pytest.mark.parametrize("fallback", ["reduced-transparency", "motion-off", "forced-colors", "backgrounds-off"])
def test_background_opt_outs_use_solid_theme_panels(chromium, kova, backdrop_theme, fallback):
    from playwright.sync_api import expect

    with chromium.new_context(viewport={"width": 1280, "height": 800},
                              service_workers="block") as context:
        page = context.new_page()
        page.route("**/app/assets/blur.js", lambda route: route.fulfill(body=""))
        with page.expect_response(kova.base_url + "/events") as events:
            assert page.goto(f"{kova.base_url}/app#ticket={kova.app_ticket()}").ok
        assert events.value.ok
        root = page.locator("html")
        expect(root).to_have_attribute("data-background", "gradient")
        expect(page.locator("main")).not_to_have_css("backdrop-filter", "none")
        if fallback == "reduced-transparency":
            session = context.new_cdp_session(page)
            session.send("Emulation.setEmulatedMedia", {
                "features": [{"name": "prefers-reduced-transparency", "value": "reduce"}],
            })
        elif fallback == "forced-colors":
            page.emulate_media(forced_colors="active")
        elif fallback == "motion-off":
            page.evaluate("window.KovaMotion.setMode('off')")
            expect(root).to_have_attribute("data-motion", "off")
        else:
            page.locator("#open-settings").click()
            group = page.get_by_role("radiogroup", name="Show theme backgrounds")
            group.get_by_role("radio", name="Off", exact=True).click()
            expect(root).to_have_attribute("data-backgrounds", "off")
            assert page.evaluate("getComputedStyle(document.body, '::before').backgroundImage") == "none"
            assert kova.preferences()["theme"] == "backdrop"
        assert_solid_panels(page, forced_colors=fallback == "forced-colors",
                            backgrounds_off=fallback == "backgrounds-off")


def test_slow_blur_fallback_uses_solid_theme_panels(chromium, kova, backdrop_theme):
    """A slow-compositor decision swaps the live backdrop blur for solid panels
    (#206). Set the decision directly to check the CSS independently of
    the runner's frame timing."""
    from playwright.sync_api import expect

    with chromium.new_context(viewport={"width": 1280, "height": 800},
                              service_workers="block") as context:
        page = context.new_page()
        page.route("**/app/assets/blur.js", lambda route: route.fulfill(body=""))
        with page.expect_response(kova.base_url + "/events") as events:
            assert page.goto(f"{kova.base_url}/app#ticket={kova.app_ticket()}").ok
        assert events.value.ok
        root = page.locator("html")
        expect(root).to_have_attribute("data-background", "gradient")

        # The live panels blur what's behind them.
        expect(page.locator("main")).not_to_have_css("backdrop-filter", "none")

        # A slow-compositor decision forces the solid fallback.
        root.evaluate("el => el.setAttribute('data-blur', 'slow')")
        assert_solid_panels(page)
