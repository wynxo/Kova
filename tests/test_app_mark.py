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
    assert html.count('class="kova-mark') == 2
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
