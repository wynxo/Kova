"""Minimum target sizes in the real app (#201): 24px desktop, 44px touch.

WCAG 2.5.8 asks for 24x24 CSS pixels, and the phone layout has always promised
44px. Static CSS cannot prove either: a control measured on screen may be
smaller than its rule, a label may carry the target instead of the input, and an
invisible hit area is not visible in the stylesheet at all. These tests open the
real app in headless Chromium at a desktop and a phone width, walk the surfaces
a reader actually reaches (start screen, a reply, the chat-options sheet, the
command palette, every Settings section) and measure the effective target of
every visible control: its own box, its label, or its expanded hit area.

Two text fields keep their small visual line on purpose and put the target in
the row around them; ``test_wrapped_text_fields_focus_from_their_row`` clicks
that row and proves the click lands in the field.

Inline links inside a reply are exempt (WCAG 2.5.8's inline exception); nothing
else is.
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

DEMO_REPLY = "# Setup\n\nA **short** reply with `code`.\n"

UPDATE_SNAPSHOTS = os.environ.get("KOVA_UPDATE_SNAPSHOTS") == "1"
VISUAL_REQUIRED = os.environ.get("KOVA_VISUAL_REQUIRED") == "1"

# A control whose visible line is smaller than the minimum, but whose whole row
# is the target: clicking the row focuses it. Proven separately below.
WRAPPED = {
    "#prompt": "#composer",
    "#command-palette-input": ".palette-field",
}


def unavailable(reason):
    if VISUAL_REQUIRED:
        pytest.fail(reason, pytrace=False)
    pytest.skip(reason)


@pytest.fixture(scope="module")
def chromium():
    if not (VISUAL_REQUIRED or UPDATE_SNAPSHOTS or os.environ.get("KOVA_VISUAL") == "1"):
        pytest.skip("Target-size checks are opt-in; set KOVA_VISUAL=1.")
    if platform.system() != "Linux" or platform.machine() != "x86_64":
        unavailable("Target-size checks use Linux x86_64 Chromium; see CONTRIBUTING.md.")
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
    home = tmp_path_factory.mktemp("visual-targets-kova")

    def model_stub(request):
        if request.method == "GET":
            return httpx.Response(200, json={"data": [{"id": "demo-model"}]})
        delta = json.dumps({"choices": [{"delta": {"content": DEMO_REPLY}}]})
        return httpx.Response(200, text=f"data: {delta}\n\ndata: [DONE]\n\n",
                              headers={"content-type": "text/event-stream"})

    listener = socket.socket()
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
        assert server.started, "Target-size server did not start"
        url = f"http://127.0.0.1:{port}"
        with httpx.Client(base_url=url, timeout=5, trust_env=False) as http:
            yield KovaClient(url, app.state.token, http)
    finally:
        server.should_exit = True
        thread.join(10)
        assert not thread.is_alive(), "Target-size server did not stop"


def open_app(page, kova):
    from playwright.sync_api import expect

    with page.expect_response(kova.base_url + "/events"):
        with page.expect_response(kova.base_url + "/app/login"):
            assert page.goto(f"{kova.base_url}/app#ticket={kova.app_ticket()}").ok
    expect(page.locator("#prompt")).to_be_focused()
    page.evaluate("() => document.fonts.ready")


SCAN = r"""
(minimum) => {
  const SEL = 'a[href],button,input:not([type="hidden"]),select,textarea,summary,'
    + '[role="button"],[role="checkbox"],[role="switch"],[role="radio"],'
    + '[role="tab"],[role="menuitem"],[role="option"],[tabindex]:not([tabindex="-1"])';
  const small = [];
  for (const el of document.querySelectorAll(SEL)) {
    const style = getComputedStyle(el);
    if (style.display === 'none' || style.visibility === 'hidden') continue;
    if (!el.getClientRects().length) continue;
    if (el.closest('#messages p a, #messages li a')) continue;  // inline prose link
    // A form control inside a label is clicked through the label: use its box.
    const target = el.closest('label') || el;
    const rect = target.getBoundingClientRect();
    if (!rect.width && !rect.height) continue;
    // An invisible hit area is a positioned pseudo-element of the target.
    const hit = name => {
      const pseudo = getComputedStyle(target, name);
      if (pseudo.position !== 'absolute' && pseudo.position !== 'fixed') return 0;
      return Math.max(parseFloat(pseudo.width) || 0, parseFloat(pseudo.height) || 0);
    };
    const width = Math.max(rect.width, hit('::after'), hit('::before'));
    const height = Math.max(rect.height, hit('::after'), hit('::before'));
    if (width < minimum || height < minimum) {
      small.push({
        tag: el.tagName.toLowerCase(),
        id: el.id,
        cls: typeof el.className === 'string' ? el.className : '',
        name: (el.getAttribute('aria-label') || el.textContent || '')
          .trim().replace(/\s+/g, ' ').slice(0, 60),
        width: Math.round(width * 10) / 10,
        height: Math.round(height * 10) / 10,
      });
    }
  }
  return small;
}
"""


def scan(page, minimum, surface):
    found = page.evaluate(SCAN, minimum)
    problems = []
    for item in found:
        selector = f"#{item['id']}" if item["id"] else f"{item['tag']}.{item['cls'].split(' ')[0]}"
        if selector in WRAPPED:
            wrapper = page.evaluate(
                """(selector) => {
                    const el = document.querySelector(selector);
                    if (!el) return null;
                    const r = el.getBoundingClientRect();
                    return [r.width, r.height];
                }""", WRAPPED[selector])
            if wrapper and wrapper[0] >= minimum and wrapper[1] >= minimum:
                continue
        problems.append(f"{surface}: {item['width']}x{item['height']} "
                        f"<{item['tag']}> {selector} {item['name']!r}")
    return problems


def walk_surfaces(page, minimum):
    """Every surface a reader can reach, with its problems collected."""
    from playwright.sync_api import expect

    problems = []
    problems += scan(page, minimum, "start screen")

    page.locator("#prompt").fill("Show the example")
    page.locator("#prompt").press("Enter")
    expect(page.locator("#messages .assistant .message-actions")).to_be_visible(timeout=10_000)
    page.wait_for_timeout(300)
    problems += scan(page, minimum, "reply")

    page.locator("#chat-options").click()
    expect(page.locator("#chat-options-panel")).to_be_visible()
    page.wait_for_timeout(200)
    problems += scan(page, minimum, "chat options")
    page.keyboard.press("Escape")

    page.keyboard.press("Control+k")
    expect(page.locator(".palette-field")).to_be_visible()
    page.wait_for_timeout(200)
    problems += scan(page, minimum, "command palette")
    page.keyboard.press("Escape")

    # The two appearance dialogs the palette owns.
    for command, surface in (("Browse themes", "theme gallery"),
                             ("Customize theme", "theme editor")):
        page.keyboard.press("Control+k")
        expect(page.locator(".palette-field")).to_be_visible()
        page.locator(".palette-input").fill(command)
        page.wait_for_timeout(150)
        page.locator(".palette-option", has_text=command).first.click()
        page.wait_for_timeout(600)
        problems += scan(page, minimum, surface)
        page.keyboard.press("Escape")
        page.wait_for_timeout(300)

    if not page.locator("#open-settings").is_visible():
        # Phones keep Settings in the conversation drawer.
        page.locator("#menu").click()
        page.wait_for_timeout(300)
    page.locator("#open-settings").click()
    expect(page.locator(".settings-page")).to_be_visible()
    page.wait_for_timeout(300)
    tabs = page.locator(".settings-nav [role=tab]")
    for index in range(tabs.count()):
        tabs.nth(index).click()
        page.wait_for_timeout(120)
        label = tabs.nth(index).inner_text().strip().lower()
        problems += scan(page, minimum, f"settings · {label}")

    # The dialogs Settings owns, opened the way its rows open them.
    for action, surface in (
        ("open-model-settings", "model settings"),
        ("open-memory", "memory"),
        ("open-reminders", "reminders"),
        ("open-mcp", "MCP"),
        ("open-extensions", "extensions"),
        ("add-provider", "connect a provider"),
        ("open-privacy", "privacy"),
        ("connect-phone", "phone"),
        ("install-settings", "install"),
    ):
        page.evaluate("id => document.getElementById(id)?.click()", action)
        page.wait_for_timeout(500)
        if page.locator("dialog[open]").count():
            problems += scan(page, minimum, surface)
            page.keyboard.press("Escape")
            page.wait_for_timeout(200)
    return problems


MINIMUMS = [(1280, 900, 24, "desktop"), (390, 844, 44, "phone")]


@pytest.mark.parametrize("width,height,minimum,name", MINIMUMS, ids=[m[3] for m in MINIMUMS])
def test_every_control_meets_the_minimum_target(chromium, kova, width, height, minimum, name):
    context = chromium.new_context(viewport={"width": width, "height": height},
                                   service_workers="block")
    try:
        page = context.new_page()
        open_app(page, kova)
        problems = walk_surfaces(page, minimum)
        assert not problems, (
            f"{name}: {len(problems)} control(s) under {minimum}px\n" + "\n".join(problems))
    finally:
        context.close()


def wrapped_field_focus(page, wrapper_selector, field_selector):
    from playwright.sync_api import expect

    box = page.evaluate(
        """(selector) => {
            const r = document.querySelector(selector).getBoundingClientRect();
            return {x: r.x, y: r.y, width: r.width, height: r.height};
        }""", wrapper_selector)
    # Two pixels inside the row's top edge, away from the field's own text line.
    page.mouse.click(box["x"] + box["width"] / 2, box["y"] + 2)
    expect(page.locator(field_selector)).to_be_focused()


@pytest.mark.parametrize("width,height,minimum,name", MINIMUMS, ids=[m[3] for m in MINIMUMS])
def test_wrapped_text_fields_focus_from_their_row(chromium, kova, width, height, minimum, name):
    context = chromium.new_context(viewport={"width": width, "height": height},
                                   service_workers="block")
    try:
        page = context.new_page()
        open_app(page, kova)
        composer = page.evaluate(
            "() => { const r = document.querySelector('#composer').getBoundingClientRect();"
            " return [r.width, r.height]; }")
        assert min(composer) >= minimum, f"{name}: the composer row is only {composer}"
        wrapped_field_focus(page, "#composer", "#prompt")

        page.keyboard.press("Control+k")
        from playwright.sync_api import expect
        expect(page.locator(".palette-field")).to_be_visible()
        field = page.evaluate(
            "() => { const r = document.querySelector('.palette-field').getBoundingClientRect();"
            " return [r.width, r.height]; }")
        assert min(field) >= minimum, f"{name}: the palette field is only {field}"
        wrapped_field_focus(page, ".palette-field", "#command-palette-input")
    finally:
        context.close()


def test_splitter_drag_target_reaches_into_the_conversation(chromium, kova):
    """The divider is drawn as a hairline, but its pointer target is 24px."""
    context = chromium.new_context(viewport={"width": 1280, "height": 900},
                                   service_workers="block")
    try:
        page = context.new_page()
        open_app(page, kova)
        hit = page.evaluate("""() => {
            const el = document.querySelector('#sidebar-splitter');
            const r = el.getBoundingClientRect();
            const target = document.elementFromPoint(r.left + r.width + 8, 400);
            return {id: target && target.id, width: r.width};
        }""")
        assert hit["id"] == "sidebar-splitter", (
            f"8px past the divider the pointer hits {hit['id']!r}, not the splitter: {hit}")
    finally:
        context.close()


def test_scanner_reports_a_deliberately_small_control(chromium, kova):
    """The gate fails for the reason it claims: a 10px button is reported."""
    context = chromium.new_context(viewport={"width": 1280, "height": 900},
                                   service_workers="block")
    try:
        page = context.new_page()
        open_app(page, kova)
        page.evaluate("""() => {
            const tiny = document.createElement('button');
            tiny.id = 'tiny-target';
            tiny.textContent = 'tiny';
            tiny.style.cssText = 'position:fixed;left:200px;top:200px;width:10px;height:10px';
            document.body.append(tiny);
        }""")
        problems = scan(page, 24, "self-check")
        assert any("#tiny-target" in problem for problem in problems), problems
    finally:
        context.close()
