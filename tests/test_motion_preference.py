"""The Motion setting (#181): one attribute, one block, no per-component rules.

``motion.js`` resolves the Full / Reduced / Off preference — shared with the
server's UI preferences document — onto ``<html data-motion>``, and one block in
``app.css`` carries it for every component by overriding the duration tokens and
stopping loops. These checks run in the normal suite and need no browser.
"""

from __future__ import annotations

import re
from pathlib import Path

ASSETS = Path(__file__).resolve().parents[1] / "src" / "kova" / "web_assets"
INDEX = ASSETS / "index.html"
SCRIPT = ASSETS / "motion.js"
STYLESHEET = ASSETS / "app.css"
COLOUR_LITERAL = re.compile(r"#[0-9a-fA-F]{3,8}\b|\brgba?\(")


def without_comments(text: str) -> str:
    """Comments carry issue numbers like #181, which are not colours."""
    return re.sub(r"/\*.*?\*/", "", text, flags=re.S)


def motion_block() -> str:
    """The one block that applies the setting, from its comment to its last rule."""
    text = without_comments(STYLESHEET.read_text(encoding="utf-8"))
    start = text.index('[data-motion="reduced"] {')
    end = text.index(".rules-dialog", start)
    return text[start:end]


def test_motion_script_ships_with_the_page_before_the_body() -> None:
    page = INDEX.read_text(encoding="utf-8")
    worker = (ASSETS / "sw.js").read_text(encoding="utf-8")
    tag = re.search(r'<script[^>]*src="/app/assets/motion\.js"[^>]*>', page)
    assert tag, "index.html loads motion.js"
    assert "defer" not in tag.group(0) and "async" not in tag.group(0), \
        "motion.js paints <html> before the first frame"
    assert page.index("tokens.css") < tag.start() < page.index("<body")
    assert "/app/assets/motion.js" in worker, "motion.js belongs in the offline shell"


def test_reduced_shortens_the_tokens_and_stops_every_loop_but_the_spinner() -> None:
    block = motion_block()
    tokens = re.search(r'\[data-motion="reduced"\]\s*\{([^}]*)\}', block)
    assert tokens, "Reduced must override the duration tokens"
    for token in ("--motion-fast", "--motion-base", "--motion-slow"):
        assert f"{token}:" in tokens.group(1), f"Reduced should shorten {token}"
    reduced = block[block.index('[data-motion="reduced"] *'):block.index('[data-motion="off"] *')]
    assert "animation-iteration-count:1" in reduced, "Reduced must stop looping animations"
    assert "animation:spin 1.2s linear infinite !important" in reduced, \
        "the spinner is the one loop that keeps turning under Reduced"
    assert ".thinking-anim" in reduced and "animation:none !important" in reduced, \
        "every thinking animation keeps its still frame"


def test_off_removes_every_transition_and_animation() -> None:
    block = motion_block()[motion_block().index('[data-motion="off"] *'):]
    assert "animation:none !important" in block
    assert "transition:none !important" in block
    assert "color:var(--fg-muted) !important" in block, \
        "text a stopped animation was painting stays readable"


def test_the_motion_block_is_token_only() -> None:
    block = motion_block()
    assert COLOUR_LITERAL.findall(block) == [], "the block hard-codes a colour; use a token"
    assert "var(--motion-fast)" in block


def test_motion_script_reads_the_shared_preference_and_the_device() -> None:
    text = without_comments(SCRIPT.read_text(encoding="utf-8"))
    for needed in ("'data-motion'", "'full'", "'reduced'", "'off'", "'/preferences'",
                   "'(prefers-reduced-motion: reduce)'", "window.KovaMotion"):
        assert needed in text, f"motion.js lost {needed}"
    assert "localStorage" in text, "the setting must paint before the server answers"
