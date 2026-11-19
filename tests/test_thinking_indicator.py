"""The thinking indicator: the app mark with one calm animation (#171)."""

from __future__ import annotations

import re
from pathlib import Path

ASSETS = Path(__file__).resolve().parents[1] / "src" / "kova" / "web_assets"
CSS = (ASSETS / "thinking.css").read_text(encoding="utf-8")
SHELL = (ASSETS / "app.js").read_text(encoding="utf-8")
BOLT = (ASSETS / "bolt.svg").read_text(encoding="utf-8")
LITERAL_COLOUR = re.compile(r"#[0-9a-fA-F]{3,8}\b|\brgba?\(|\bhsla?\(")


def without_comments(text: str) -> str:
    """Comments carry issue numbers like #171, which are not colours."""
    return re.sub(r"/\*.*?\*/", "", text, flags=re.S)

# Each animation's preference value, the element it moves, and its keyframes.
ANIMATIONS = {
    "thunderstorm": ("thinking-bolt", ["thinking-bolt"]),
    "orbit": ("thinking-orbit", ["thinking-orbit"]),
    "collide": ("thinking-collide", ["thinking-collide-a", "thinking-collide-b"]),
    "breathe": ("thinking-logo", ["thinking-breathe"]),
    "charge": ("thinking-fill", ["thinking-charge"]),
}


def rule(selector: str) -> str:
    """The declarations of one plain rule; an animation is never the base."""
    found = re.search(re.escape(selector) + r"\s*\{([^}]*)\}", CSS)
    assert found, f"thinking.css has no rule for {selector}"
    return found.group(1)


def test_stylesheet_and_script_are_wired_into_the_shell():
    index = (ASSETS / "index.html").read_text(encoding="utf-8")
    worker = (ASSETS / "sw.js").read_text(encoding="utf-8")
    assert "/app/assets/thinking.css" in index and "/app/assets/thinking.js" in index
    for name in ("thinking.css", "thinking.js", "bolt.svg"):
        assert f"/app/assets/{name}" in worker, f"{name} belongs in the offline shell"


def test_bolt_is_flat_and_uses_the_text_colour():
    assert "currentColor" in BOLT
    assert not LITERAL_COLOUR.search(BOLT)
    assert not re.search(r"gradient|filter|shadow", BOLT, re.I)


def test_styles_use_theme_variables_and_no_glow():
    styles = without_comments(CSS)
    assert not LITERAL_COLOUR.search(styles)
    assert "var(--accent-default)" in styles
    assert not re.search(r"box-shadow|drop-shadow|gradient", styles)


def test_each_animation_is_gated_by_the_preference_and_has_its_keyframes():
    for name, (part, keyframes) in ANIMATIONS.items():
        assert f'[data-thinking="{name}"] .{part}' in CSS, f"{name} is never selected"
        for keyframe in keyframes:
            assert f"@keyframes {keyframe}" in CSS, f"{name} has no {keyframe} keyframes"
            assert f"animation:{keyframe} " in CSS, f"{name} never uses its keyframes"
    for name in ("orbit", "collide", "charge"):
        assert f'[data-thinking="{name}"]' in CSS and "display:block" in CSS


def test_every_part_rests_at_its_still_frame():
    """The rule that is left when animation is off must already show the state."""
    for part in ("thinking-bolt", "thinking-orbit", "thinking-collide", "thinking-fill"):
        base = rule(f".{part}")
        assert "animation" not in base, f".{part} depends on movement to be visible"
        assert "opacity:0" not in base, f".{part} would disappear without its animation"
    assert "animation" not in rule(".thinking-logo"), "the mark rests still"


def test_reduced_motion_keeps_a_still_frame_and_the_label():
    block = re.search(r"@media \(prefers-reduced-motion: reduce\)\s*\{(.*?)\n\}", CSS, re.S)
    assert block, "a reduced-motion block is required"
    assert ".thinking-anim" in block.group(1) and "animation:none !important" in block.group(1)
    # Every animated element carries the class, so none keeps moving.
    for part in ("thinking-bolt", "thinking-orbit", "thinking-collide", "thinking-fill",
                 "thinking-logo"):
        line = next(
            (line for line in SHELL.splitlines() if part in line and "el('" in line), None
        )
        assert line is not None, f"the activity mark no longer builds {part}"
        assert "thinking-anim" in line, f"{part} would keep moving under reduced motion"
    assert "Thinking…" in SHELL, "the state is written, not only animated"


def test_a_hidden_row_does_not_animate():
    assert ".activity[hidden] .thinking-anim" in CSS
