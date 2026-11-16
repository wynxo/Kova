"""The Kova mark, the app icon and the thinking indicator.

The logo is an asset, so the checks are structural: the SVG stays flat and
currentColor-driven, the installed icons exist at the sizes the manifest asks
for, and the one piece of motion on the start screen still has a still, visible
fallback for people who ask for reduced motion.
"""

from __future__ import annotations

import re
import struct
from pathlib import Path

ASSETS = Path(__file__).resolve().parents[1] / "src" / "kova" / "web_assets"
LOGO = ASSETS / "logo.svg"
BOLT = ASSETS / "bolt.svg"
ICON = ASSETS / "icon.svg"
STYLES = ASSETS / "logo.css"
INDEX = ASSETS / "index.html"
APP_JS = ASSETS / "app.js"

# Hex colours and functional colour notations. `currentColor` and `transparent`
# are not colours in this sense: they carry no value of their own.
LITERAL_COLOUR = re.compile(r"#[0-9a-fA-F]{3,8}\b|\b(?:rgb|hsl)a?\(", re.IGNORECASE)
COMMENT = re.compile(r"/\*.*?\*/", re.DOTALL)

ICON_SIZES = {
    "icon-16.png": 16,
    "icon-32.png": 32,
    "icon-180.png": 180,
    "icon-192.png": 192,
    "icon-512.png": 512,
}


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def without_comments(text: str) -> str:
    return COMMENT.sub("", text)


def png_size(path: Path) -> tuple[int, int]:
    header = path.read_bytes()[:24]
    assert header[:8] == b"\x89PNG\r\n\x1a\n", f"{path.name} is not a PNG"
    assert header[12:16] == b"IHDR", f"{path.name} has no IHDR"
    return struct.unpack(">II", header[16:24])


def test_logo_is_the_k_in_a_circle() -> None:
    text = read(LOGO)
    assert 'viewBox="0 0 120 120"' in text
    assert text.count("<circle") == 1
    # Stem, mountain ridge and lower leg; the ridge is the three-point path.
    assert text.count("<path") == 3
    assert 'stroke="currentColor"' in text
    assert "stroke-linecap=\"round\"" in text and "stroke-linejoin=\"round\"" in text


def test_flat_assets_have_no_gradients_filters_or_shadows() -> None:
    for path in (LOGO, BOLT, ICON):
        text = read(path).lower()
        for banned in ("lineargradient", "radialgradient", "<filter", "filter=", "drop-shadow"):
            assert banned not in text, f"{path.name} uses {banned}"


def test_svg_mark_has_no_literal_colour() -> None:
    for path in (LOGO, BOLT):
        assert not LITERAL_COLOUR.search(without_comments(read(path))), (
            f"{path.name} must inherit currentColor instead of naming a colour"
        )


def test_app_icon_is_monochrome_and_keeps_the_mark() -> None:
    text = read(ICON)
    colours = set(LITERAL_COLOUR.findall(without_comments(text)))
    assert colours == {"#14121c", "#e8e6f0"}, (
        "the app icon is the one asset allowed literal colours: its tile and mark"
    )
    assert text.count("<path") == 3 and text.count("<circle") == 1


def test_installed_icons_exist_at_their_declared_sizes() -> None:
    for name, size in ICON_SIZES.items():
        path = ASSETS / name
        assert path.is_file(), f"missing {name}"
        assert png_size(path) == (size, size), f"{name} is not {size}x{size}"


def test_index_loads_the_mark_stylesheet_and_uses_the_mark() -> None:
    index = read(INDEX)
    assert 'href="/app/assets/logo.css"' in index
    assert index.index("app.css") < index.index("logo.css"), "logo.css must follow app.css"
    assert 'class="logo-mark"' in index, "the sidebar brand uses the drawn mark"
    assert 'class="logo-mark welcome-mark"' in index, "the start screen uses the drawn mark"
    # The favicon still points at the installed app icon, but the page itself no
    # longer ships the old gradient tile as an <img>.
    assert 'src="/app/assets/icon.svg"' not in index


def test_thinking_indicator_wiring() -> None:
    js = read(APP_JS)
    assert "el('span', 'thinking-mark')" in js
    assert "el('span', 'thinking-bolt')" in js
    assert "el('span', 'activity-text', 'Thinking…')" in js
    assert "el('span', 'logo-mark avatar')" in js


def test_styles_use_tokens_and_the_shared_assets() -> None:
    css = without_comments(read(STYLES))
    assert not LITERAL_COLOUR.search(css), "the thinking indicator must use semantic tokens"
    assert "url(\"/app/assets/logo.svg\")" in css
    assert "url(\"/app/assets/bolt.svg\")" in css
    assert "var(--fg-default)" in css and "var(--accent-default)" in css


def test_reduced_motion_keeps_a_visible_still_fallback() -> None:
    css = without_comments(read(STYLES))
    block = re.search(r"@media\s*\(prefers-reduced-motion:\s*reduce\)\s*\{(.*?)\n\}", css, re.DOTALL)
    assert block, "logo.css has no prefers-reduced-motion block"
    rules = block.group(1)
    assert ".thinking-bolt" in rules
    assert "animation:none" in rules
    # The bolt is decoration; the mark and the written state stay visible.
    assert "display:none" not in rules
    assert "opacity:0" in rules
    assert ".thinking-mark .logo-mark" in rules
    assert "'Thinking…'" in read(APP_JS), "the label is the fallback when motion is off"
    assert ".activity[hidden] .thinking-bolt" in css, "a hidden turn must not keep animating"
