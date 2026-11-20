"""Real 200% zoom reflow coverage (#201): no lost content at double size.

WCAG 1.4.10 (Reflow) requires no loss down to 320 CSS px; 200% zoom on a
1280px display is 640 CSS px. These tests load the real app in headless
Chromium at desktop (1280), 200% zoom (640) and reflow (320) widths and
assert the page never scrolls sideways and the composer controls stay
visible and inside the viewport, including with long unbroken content and
the chat-options sheet open. Static guards for the same invariants live in
``tests/test_zoom_reflow.py``.
"""

from __future__ import annotations

import json
import os
import platform
import socket
from pathlib import Path
from threading import Thread
from time import monotonic, sleep

import httpx
import pytest
import uvicorn

from kova.client import KovaClient
from kova.config import Config, ModelEntry, save_config
from kova.server import create_app

pytestmark = pytest.mark.visual

LONG_WORD = "A" * 200
LONG_URL = "https://example.com/" + "b" * 120
LONG_CODE = "x" * 120
DEMO_REPLY = (
    f"# Setup\n\n{LONG_WORD} {LONG_URL}\n\n"
    f"```js:example.js\nconsole.log('{LONG_CODE}');\n```\n"
)

UPDATE_SNAPSHOTS = os.environ.get("KOVA_UPDATE_SNAPSHOTS") == "1"
VISUAL_REQUIRED = os.environ.get("KOVA_VISUAL_REQUIRED") == "1"


def unavailable(reason):
    if VISUAL_REQUIRED:
        pytest.fail(reason, pytrace=False)
    pytest.skip(reason)


@pytest.fixture(scope="module")
def chromium():
    if not (VISUAL_REQUIRED or UPDATE_SNAPSHOTS or os.environ.get("KOVA_VISUAL") == "1"):
        pytest.skip("Visual zoom checks are opt-in; set KOVA_VISUAL=1.")
    if platform.system() != "Linux" or platform.machine() != "x86_64":
        unavailable("Zoom baselines use Linux x86_64 Chromium; see CONTRIBUTING.md.")
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
    from kova.themes import theme_dir
    import shutil

    from kova import themes  # noqa: F401  (keeps the visual home realistic)

    root = Path(__file__).resolve().parents[2]
    home = tmp_path_factory.mktemp("visual-zoom-kova")
    themes_home = theme_dir(home)
    themes_home.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(root / "design" / "themes" / "forest.json", themes_home / "forest.json")

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
            assert server.started, "Zoom server did not start"
            url = f"http://127.0.0.1:{port}"
            with httpx.Client(base_url=url, timeout=5, trust_env=False) as http:
                yield KovaClient(url, app.state.token, http)
        finally:
            server.should_exit = True
            thread.join(10)
            assert not thread.is_alive(), "Zoom server did not stop"


def open_start(page, kova):
    from playwright.sync_api import expect

    url = kova.base_url + "/app"
    with page.expect_response(kova.base_url + "/events"):
        with page.expect_response(kova.base_url + "/app/login"):
            assert page.goto(f"{url}#ticket={kova.app_ticket()}").ok
    expect(page.locator("#prompt")).to_be_focused()
    page.evaluate("() => document.fonts.ready")


def assert_no_page_overflow(page, width: int) -> None:
    sizes = page.evaluate(
        "() => ({scroll: document.documentElement.scrollWidth, inner: window.innerWidth})")
    assert sizes["scroll"] <= width + 1, (
        f"page overflows at {width}px: scrollWidth {sizes['scroll']} > {width}")
    assert sizes["scroll"] <= sizes["inner"] + 1


def assert_composer_usable(page, width: int) -> None:
    from playwright.sync_api import expect

    for selector in ("#prompt", "#send", "#menu"):
        expect(page.locator(selector).first).to_be_visible()
    send = page.locator("#send").first.bounding_box()
    assert send is not None, "send button has no box"
    assert 0 <= send["x"] <= width, "send button left of the viewport"
    assert send["x"] + send["width"] <= width + 1, "send button clipped past the viewport"


@pytest.mark.parametrize("width", [1280, 640, 320], ids=["desktop", "zoom-200", "reflow-320"])
def test_start_screen_reflows_without_sideways_scroll(chromium, kova, width) -> None:
    context = chromium.new_context(viewport={"width": width, "height": 900},
                                   service_workers="block")
    try:
        page = context.new_page()
        open_start(page, kova)
        page.mouse.move(-1, -1)
        assert_no_page_overflow(page, width)
        assert_composer_usable(page, width)
    finally:
        context.close()


def test_long_reply_reflows_at_200_percent(chromium, kova) -> None:
    from playwright.sync_api import expect

    width = 640  # 200% zoom on a 1280px display.
    context = chromium.new_context(viewport={"width": width, "height": 900},
                                   service_workers="block")
    try:
        page = context.new_page()
        open_start(page, kova)
        page.locator("#prompt").fill("Show the example")
        page.locator("#prompt").press("Enter")
        reply = page.locator("#messages .assistant")
        code = reply.locator("pre code")
        expect(code).to_be_visible(timeout=10_000)
        page.evaluate("() => document.fonts.ready")
        assert_no_page_overflow(page, width)
        # A long code line can extend beyond its own box, but the code box
        # itself must fit and expose the rest of the line by scrolling.
        assert code.evaluate("(el) => { el.scrollLeft = el.scrollWidth; return el.scrollLeft > 0; }")
        protruding = page.evaluate(
            "() => [...document.querySelectorAll('#messages *')]"
            ".filter(e => !e.parentElement.closest('pre code'))"
            ".filter(e => e.getBoundingClientRect().right > window.innerWidth + 1)"
            ".slice(0, 5).map(e => e.className || e.tagName)")
        assert protruding == [], f"reply content escapes the viewport at 640px: {protruding}"
    finally:
        context.close()


def test_chat_options_sheet_stays_in_viewport_at_200_percent(chromium, kova) -> None:
    width = 640  # 200% zoom on a 1280px display.
    context = chromium.new_context(viewport={"width": width, "height": 900},
                                   service_workers="block")
    try:
        page = context.new_page()
        open_start(page, kova)
        page.locator("#chat-options").click()
        page.wait_for_timeout(400)
        assert_no_page_overflow(page, width)
        panel = page.evaluate(
            "() => { const r = document.querySelector('#chat-options-panel')"
            ".getBoundingClientRect();"
            " return {left: r.left, right: r.right, width: r.width}; }")
        assert panel["left"] >= -1, "options panel escapes the left edge at 640px"
        assert panel["right"] <= width + 1, "options panel escapes the right edge at 640px"
        page.keyboard.press("Escape")
    finally:
        context.close()
