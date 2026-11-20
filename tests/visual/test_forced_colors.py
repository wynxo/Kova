"""Forced-colors support for the interactive chat controls (#201).

The shell already returned its theme surfaces to the measured palette under
``forced-colors``. These checks cover what a system palette silently erases:
a control whose only boundary or selected state was a fill, the switch and the
mode radio group, the plan progress bar, the tool-card status marks, and the
masked icon set — painted with ``background-color``, which forced colours
otherwise flattens into the surface. They run in headless Chromium with the
emulated forced-colors palette, so the values asserted are the ones a reader
actually gets, not the authored ones.

Static guards for the same theme live in ``tests/test_ui_a11y_contrast.py``.
"""

from __future__ import annotations

import json
import os
import platform
import socket
from threading import Thread
from time import monotonic, sleep

import httpx
import pytest
import uvicorn

from kova.client import KovaClient
from kova.config import Config, ModelEntry, save_config
from kova.server import create_app

pytestmark = pytest.mark.visual

DEMO_REPLY = "# Setup\n\n**A short introduction** with `code`.\n"

UPDATE_SNAPSHOTS = os.environ.get("KOVA_UPDATE_SNAPSHOTS") == "1"
VISUAL_REQUIRED = os.environ.get("KOVA_VISUAL_REQUIRED") == "1"


def unavailable(reason):
    if VISUAL_REQUIRED:
        pytest.fail(reason, pytrace=False)
    pytest.skip(reason)


@pytest.fixture(scope="module")
def chromium():
    if not (VISUAL_REQUIRED or UPDATE_SNAPSHOTS or os.environ.get("KOVA_VISUAL") == "1"):
        pytest.skip("Forced-colors checks are opt-in; set KOVA_VISUAL=1.")
    if platform.system() != "Linux" or platform.machine() != "x86_64":
        unavailable("Forced-colors checks use Linux x86_64 Chromium; see CONTRIBUTING.md.")
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        unavailable("Install the optional visual group: uv sync --group visual.")

    with sync_playwright() as playwright:
        try:
            browser = playwright.chromium.launch()
        except Exception as exc:
            if "Executable doesn't exist" in str(exc) or "Host system" in str(exc):
                unavailable("Install Chromium with: uv run --group visual playwright install chromium.")
            raise
        try:
            yield browser
        finally:
            browser.close()


@pytest.fixture(scope="module")
def kova(chromium, tmp_path_factory):
    home = tmp_path_factory.mktemp("visual-forced-colors-kova")

    def model_stub(request):
        if request.method == "GET":
            return httpx.Response(200, json={"data": [{"id": "demo-model"}]})
        delta = json.dumps({"choices": [{"delta": {"content": DEMO_REPLY}}]})
        return httpx.Response(200, text=f"data: {delta}\n\ndata: [DONE]\n\n",
                              headers={"content-type": "text/event-stream"})

    with socket.socket() as listener:
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
            assert server.started, "Forced-colors server did not start"
            url = f"http://127.0.0.1:{port}"
            with httpx.Client(base_url=url, timeout=5, trust_env=False) as http:
                yield KovaClient(url, app.state.token, http)
        finally:
            server.should_exit = True
            thread.join(10)
            assert not thread.is_alive(), "Forced-colors server did not stop"


def open_start(page, kova):
    from playwright.sync_api import expect

    with page.expect_response(kova.base_url + "/events"):
        with page.expect_response(kova.base_url + "/app/login"):
            assert page.goto(f"{kova.base_url}/app#ticket={kova.app_ticket()}").ok
    expect(page.locator("#prompt")).to_be_focused()


def open_reply_and_options(page):
    from playwright.sync_api import expect

    page.locator("#prompt").fill("Show the example")
    page.locator("#prompt").press("Enter")
    expect(page.locator("#messages .assistant .message-actions")).to_be_visible(timeout=10_000)
    page.locator("#chat-options").click()
    expect(page.locator("#chat-options-panel")).to_be_visible()


def style(page, selector, *properties):
    return page.evaluate(
        """([selector, properties]) => {
            const el = document.querySelector(selector);
            if (!el) return null;
            const computed = getComputedStyle(el);
            return Object.fromEntries(properties.map(name => [name, computed[name]]));
        }""",
        [selector, list(properties)])


def assert_bordered(page, selector):
    computed = style(page, selector, "borderTopWidth", "borderTopStyle")
    assert computed is not None, f"{selector} is missing"
    assert computed["borderTopStyle"] != "none", f"{selector} has no border in forced colours"
    assert float(computed["borderTopWidth"].removesuffix("px")) >= 1, (
        f"{selector} has a {computed['borderTopWidth']} border in forced colours")


def test_chat_controls_keep_a_boundary_icons_and_focus(chromium, kova):
    context = chromium.new_context(viewport={"width": 1280, "height": 900},
                                   forced_colors="active", service_workers="block")
    try:
        page = context.new_page()
        open_start(page, kova)
        assert page.evaluate("matchMedia('(forced-colors: active)').matches"), (
            "Chromium did not emulate forced colours")
        open_reply_and_options(page)

        # Standalone chat controls: without these, the system palette leaves a
        # borderless icon with no visible target in the message box or transcript.
        for selector in ("#compose-more", "#chat-options", "#send", ".message-action"):
            assert_bordered(page, selector)

        page_background = style(page, "body", "backgroundColor")["backgroundColor"]
        for selector in ("#compose-more .icon", "#send .icon", ".message-action .icon"):
            painted = style(page, selector, "backgroundColor")["backgroundColor"]
            assert painted != page_background, (
                f"{selector} is painted in the surface colour under forced colours")

        # Keyboard focus stays unmistakable on the same controls.
        page.locator("#prompt").focus()
        page.keyboard.press("Shift+Tab")
        focused = page.evaluate(
            """() => {
                const el = document.activeElement;
                const computed = getComputedStyle(el);
                return {id: el.id, style: computed.outlineStyle, width: computed.outlineWidth,
                        colour: computed.outlineColor,
                        background: getComputedStyle(document.body).backgroundColor};
            }""")
        assert focused["id"] == "compose-more", focused
        assert focused["style"] != "none", f"#compose-more has no focus ring: {focused}"
        assert float(focused["width"].removesuffix("px")) >= 2, focused
        assert focused["colour"] != focused["background"], (
            f"the focus ring uses the page colour: {focused}")
    finally:
        context.close()


def test_selected_states_survive_the_forced_palette(chromium, kova):
    from playwright.sync_api import expect

    context = chromium.new_context(viewport={"width": 1280, "height": 900},
                                   forced_colors="active", service_workers="block")
    try:
        page = context.new_page()
        open_start(page, kova)
        open_reply_and_options(page)

        # A switch, and the two mode radios, must still read as on/off.
        states = page.evaluate(
            """() => {
                const network = document.querySelector('#web');
                const thumb = network.querySelector('.switch-thumb');
                const snap = () => [getComputedStyle(network).backgroundColor,
                                    getComputedStyle(thumb).backgroundColor,
                                    getComputedStyle(network).borderTopColor];
                const on = snap();
                network.setAttribute('aria-checked', 'false');
                const off = snap();
                network.setAttribute('aria-checked', 'true');
                const work = document.querySelector('#mode-switch button[data-value="work"]');
                const chat = document.querySelector('#mode-switch button[data-value="chat"]');
                const read = el => [getComputedStyle(el).backgroundColor, getComputedStyle(el).color];
                return {on, off, work: read(work), chat: read(chat)};
            }""")
        assert states["on"] != states["off"], (
            f"the Web search switch looks the same on and off: {states}")
        assert states["work"] != states["chat"], (
            f"the selected mode looks like the other: {states}")

        # The real pinned plan card: boundary, a track around the fill, a dot.
        page.evaluate("""() => window.KovaPlanCard.update([
            {text: 'Read the issue', status: 'done'},
            {text: 'Write the fix', status: 'in_progress'},
        ])""")
        expect(page.locator(".plan-card")).to_be_visible()
        assert_bordered(page, ".plan-card-toggle")
        assert_bordered(page, ".plan-card-dismiss")
        progress = page.evaluate(
            """() => {
                const bar = getComputedStyle(document.querySelector('.plan-card-bar'));
                const fill = getComputedStyle(document.querySelector('.plan-card-fill'));
                const dot = document.querySelector('.plan-card-list .plan-in_progress .plan-mark');
                return {track: bar.backgroundColor, fill: fill.backgroundColor,
                        outline: `${bar.outlineWidth} ${bar.outlineStyle}`,
                        dot: getComputedStyle(dot, '::after').backgroundColor,
                        surface: getComputedStyle(document.body).backgroundColor};
            }""")
        assert progress["fill"] != progress["track"], (
            f"the plan progress fill disappears into its track: {progress}")
        assert progress["outline"] != "0px none", (
            f"the plan progress track has no outline: {progress}")
        assert progress["dot"] != progress["surface"], (
            f"the in-progress plan dot is invisible: {progress}")

        # The real tool card: a boundary on the group row, a painted running
        # mark and a painted status tick.
        page.evaluate("""() => {
            const done = KovaToolCards.create({now: () => 0});
            done.add({name: 'run', args: {command: 'test'}});
            done.result({name: 'run', ok: true, output: 'ok'});
            done.finish();
            document.querySelector('#messages').append(done.node);
            const running = KovaToolCards.create({now: () => 0});
            running.add({name: 'read', args: {path: 'a.txt'}});
            document.querySelector('#messages').append(running.node);
        }""")
        assert_bordered(page, ".tool-card-summary")
        marks = page.evaluate(
            """() => {
                const read = selector => getComputedStyle(
                    document.querySelector(selector)).backgroundColor;
                return {running: read('.tool-card-mark.running'),
                        tick: read('.tool-card-mark.ok'),
                        surface: getComputedStyle(document.body).backgroundColor};
            }""")
        assert marks["running"] != marks["surface"], (
            f"the running tool mark is invisible: {marks}")
        assert marks["tick"] != marks["surface"], (
            f"the completed tool tick is invisible: {marks}")
    finally:
        context.close()
