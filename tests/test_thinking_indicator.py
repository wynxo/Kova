"""The thinking indicator: the app mark plus a calm bolt, with a still fallback."""

from __future__ import annotations

import re
from pathlib import Path

ASSETS = Path(__file__).resolve().parents[1] / "src" / "kova" / "web_assets"
CSS = (ASSETS / "thinking.css").read_text(encoding="utf-8")
BOLT = (ASSETS / "bolt.svg").read_text(encoding="utf-8")
LITERAL_COLOUR = re.compile(r"#[0-9a-fA-F]{3,8}\b|\brgba?\(|\bhsla?\(")


def test_stylesheet_and_bolt_are_wired_into_the_shell():
    index = (ASSETS / "index.html").read_text(encoding="utf-8")
    worker = (ASSETS / "sw.js").read_text(encoding="utf-8")
    assert "/app/assets/thinking.css" in index
    assert "/app/assets/thinking.css" in worker and "/app/assets/bolt.svg" in worker


def test_bolt_is_flat_and_uses_the_text_colour():
    assert "currentColor" in BOLT
    assert not LITERAL_COLOUR.search(BOLT)
    assert not re.search(r"gradient|filter|shadow", BOLT, re.I)


def test_styles_use_theme_variables_and_no_glow():
    assert not LITERAL_COLOUR.search(CSS)
    assert "var(--accent)" in CSS
    assert not re.search(r"box-shadow|drop-shadow|gradient", CSS)


def test_reduced_motion_stops_the_animation_but_keeps_a_still_bolt():
    block = re.search(r"@media \(prefers-reduced-motion: reduce\)\s*\{(.*?)\n\}", CSS, re.S)
    assert block, "a reduced-motion block is required"
    assert "animation:none" in block.group(1) and "opacity:1" in block.group(1)


def test_a_hidden_row_does_not_animate():
    assert ".activity[hidden] .thinking-bolt" in CSS
