"""Offline snapshots of the real web UI in headless Chromium."""

from __future__ import annotations

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
        assert request.method == "GET" and request.url.path == "/v1/models"
        return httpx.Response(200, json={"data": [{"id": "demo-model"}]})

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
