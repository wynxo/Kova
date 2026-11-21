"""The app mark: one geometry in scripts/build_icons.py, every icon file generated from it."""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ASSETS = ROOT / "src" / "kova" / "web_assets"


def _load_builder():
    spec = importlib.util.spec_from_file_location("kova_build_icons", ROOT / "scripts" / "build_icons.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


build_icons = _load_builder()


def test_icon_files_are_fresh() -> None:
    stale = [str(path.relative_to(ROOT)) for path, data in build_icons.outputs().items()
             if not build_icons.is_fresh(path, data)]
    assert not stale, f"run python scripts/build_icons.py; stale: {stale}"


def test_pngs_carry_no_metadata_chunks() -> None:
    for path in build_icons.outputs():
        if path.suffix != ".png":
            continue
        data = path.read_bytes()
        chunks, offset = [], 8
        while offset < len(data):
            length = int.from_bytes(data[offset:offset + 4], "big")
            chunks.append(data[offset + 4:offset + 8])
            offset += length + 12
        assert chunks == [b"IHDR", b"IDAT", b"IEND"], (path.name, chunks)


def test_static_icon_is_monochrome() -> None:
    svg = (ASSETS / "icon.svg").read_text(encoding="utf-8")
    assert set(re.findall(r'fill="(#[0-9a-f]{6})"', svg)) == {"#14121c", "#ffffff"}
    assert "Gradient" not in svg


def test_mark_is_a_cut_out_mask() -> None:
    svg = (ASSETS / "mark.svg").read_text(encoding="utf-8")
    assert 'fill-rule="evenodd"' in svg and "fill=\"#" not in svg


def test_ui_draws_the_mark_with_the_theme_colour() -> None:
    html = (ASSETS / "index.html").read_text(encoding="utf-8")
    js = (ASSETS / "app.js").read_text(encoding="utf-8")
    css = (ASSETS / "app.css").read_text(encoding="utf-8")
    assert html.count('class="kova-mark') == 3
    assert "'kova-mark avatar'" in js
    rule = re.search(r"^\.kova-mark \{(.*?)\}", css, re.S | re.M).group(1)
    assert "var(--fg-default)" in rule and "mark.svg" in rule
    # The sidebar, start screen and message avatar no longer load the static icon.
    assert '<img src="/app/assets/icon.svg"' not in html
    assert "avatar'); mark.src" not in js


def test_mark_is_cached_for_offline_use() -> None:
    assert "'/app/assets/mark.svg'" in (ASSETS / "sw.js").read_text(encoding="utf-8")


def test_the_start_screen_shows_the_mark_once():
    """Two merges once left both the old <img> logo and the new mark on the start screen."""
    import re
    from pathlib import Path
    page = (Path(__file__).resolve().parents[1] / "src" / "kova" / "web_assets" / "index.html").read_text(encoding="utf-8")
    welcome = re.search(r'<div id="welcome".*?</div>', page, re.S).group(0)
    assert welcome.count("welcome-mark") == 1
    assert welcome.index("welcome-mark") < welcome.index("welcome-greeting"), "the greeting sits under the mark"
import struct
import zlib


def _tray_png(name: str) -> tuple[int, int, list[int]]:
    """Decode a tray PNG to (width, height, flat RGBA bytes)."""
    data = (ROOT / "src/kova/desktop" / "assets" / name).read_bytes()
    width, height = struct.unpack(">II", data[16:24])
    idx, idat = 8, b""
    while idx < len(data):
        length = int.from_bytes(data[idx:idx + 4], "big")
        if data[idx + 4:idx + 8] == b"IDAT":
            idat += data[idx + 8:idx + 8 + length]
        idx += 12 + length
    raw = zlib.decompress(idat)
    stride = width * 4
    px = bytearray()
    for y in range(height):
        pos = y * (stride + 1) + 1
        px += raw[pos:pos + stride]
    return width, height, px


def _dot_peak(name: str) -> tuple[int, int, int]:
    """The composited status dot: brightest opaque pixel in the dot's window.

    The dot sits at a fixed spot — center (384,384) at 512px -> (48,48) at 64px,
    radius 12. Sample a window there, clear of the K which sits in the middle, so
    the K's white glyph cannot masquerade as the dot."""
    width, height, px = _tray_png(name)
    peak, best = (0, 0, 0), -1
    for y in range(44, 53):
        for x in range(44, 53):
            i = (y * width + x) * 4
            r, g, b, a = px[i], px[i + 1], px[i + 2], px[i + 3]
            if a > 200 and r + g + b > best:
                best, peak = r + g + b, (r, g, b)
    return peak


def test_tray_working_carries_a_white_dot():
    r, g, b = _dot_peak("tray-working.png")
    assert r > 200 and g > 200 and b > 200, (r, g, b)


def test_tray_waiting_carries_an_amber_dot():
    r, g, b = _dot_peak("tray-waiting.png")
    assert r > 200 and 130 < g < 200 and 50 < b < 120, (r, g, b)


def test_tray_dots_differ():
    """Working (white) and waiting (amber) must not render the same dot."""
    assert _dot_peak("tray-working.png") != _dot_peak("tray-waiting.png")


def test_tray_outputs_are_working_and_waiting_only():
    """The tray assets are the per-state dots; the old single overlay is gone."""
    names = {p.name for p in build_icons.outputs() if p.name.startswith("tray-")}
    assert names == {"tray-working.png", "tray-waiting.png"}
    assert not any(p.name == "overlay.png" for p in build_icons.outputs())
