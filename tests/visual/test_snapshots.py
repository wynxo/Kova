"""Offline snapshots of the real web UI in headless Chromium."""

from __future__ import annotations

import json
import os
import platform
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
                        models=[ModelEntry("demo", "http://localhost/v1", "demo-model")])
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
        panel.get_by_role("button", name="Close panel").click()

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
