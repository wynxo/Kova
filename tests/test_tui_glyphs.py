"""The terminal chat's glyph set and its plain-ASCII fallback.

``KOVA_ASCII=1``, a non-UTF-8 output encoding or the Linux console switch every
glyph, rule and tree to ASCII (docs/TERMINAL-DESIGN.md). The meaning never
depends on the glyph: each step also names itself in words.
"""
from __future__ import annotations

import io

import pytest

from kova.tui import glyphs as glyphs_module
from kova.tui.glyphs import ASCII, SPIN_ASCII, SPIN_UNICODE, UNICODE, ascii_only, glyphs, use
from kova.tui.render import Renderer
from kova.tui.widgets import input_box, panel, rule
from kova.tui.markdown import Palette


@pytest.fixture(autouse=True)
def fresh_glyphs():
    """Each test starts from the environment it set, not the process's first look."""
    glyphs_module._DEFAULT = None
    yield
    glyphs_module._DEFAULT = None


def test_unicode_is_the_default(monkeypatch):
    monkeypatch.delenv("KOVA_ASCII", raising=False)
    monkeypatch.setenv("TERM", "xterm-256color")
    g = glyphs()
    assert not g.ascii and g.think == UNICODE["think"] and g.spin(0) == SPIN_UNICODE[0]
    assert g.rule == "─" and g.spine == "│" and g.ellipsis == "…" and g.minus == "−"


@pytest.mark.parametrize("value", ["1", "true", "yes", "on"])
def test_asking_for_ascii_switches_every_glyph(monkeypatch, value):
    monkeypatch.setenv("KOVA_ASCII", value)
    monkeypatch.setenv("TERM", "xterm-256color")
    g = glyphs()
    assert g.ascii
    for name, glyph in ASCII.items():
        assert getattr(g, name) == glyph
        assert glyph.isascii(), name
    assert g.spin(0) == SPIN_ASCII[0] and all(s.isascii() for s in SPIN_ASCII)


@pytest.mark.parametrize("value", ["0", "false", "no", "off"])
def test_asking_against_ascii_keeps_unicode(monkeypatch, value):
    monkeypatch.setenv("KOVA_ASCII", value)
    monkeypatch.setenv("TERM", "linux")      # the explicit answer wins
    assert not glyphs().ascii


def test_the_linux_console_and_a_non_utf8_encoding_fall_back(monkeypatch):
    monkeypatch.delenv("KOVA_ASCII", raising=False)
    monkeypatch.setenv("TERM", "linux")
    assert glyphs().ascii
    monkeypatch.setenv("TERM", "xterm")
    assert ascii_only(stream=io.TextIOWrapper(io.BytesIO(), encoding="ascii"))
    monkeypatch.setenv("TERM", "xterm")
    assert not ascii_only(stream=io.TextIOWrapper(io.BytesIO(), encoding="utf-8"))


def test_a_turn_in_ascii_mode_prints_no_wide_glyphs(monkeypatch):
    monkeypatch.setenv("KOVA_ASCII", "1")
    use(None)
    out = io.StringIO()
    r = Renderer(out, color=0)
    r.begin_turn()
    r.user("hello")
    r.handle({"type": "reasoning", "text": "A thought. More."})
    r.handle({"type": "plan", "items": [{"text": "Look", "status": "in_progress"},
                                        {"text": "Fix", "status": "pending"}]})
    r.handle({"type": "tool_call", "id": "a", "name": "read_file", "args": {"path": "a.py"}})
    live = "\n".join(r.live_lines(0))
    r.handle({"type": "tool_result", "id": "a", "name": "read_file", "ok": True,
              "output": "a\nb\nc"})
    r.handle({"type": "text", "text": "Done."})
    r.finish_turn()
    text = out.getvalue() + live
    assert "| hello" in text and ": thought" in text and "= plan" in text
    assert "> read" in text and "|  " in text
    for char in text:
        assert char.isascii() or char == "\n", repr(char)


def test_rules_panels_and_the_input_bar_are_ascii_too(monkeypatch):
    monkeypatch.setenv("KOVA_ASCII", "1")
    use(None)
    p = Palette(False)
    assert set(rule(20, p)) == {"-"}
    rows = panel(["body"], 20, p, title="Title", footer="esc")
    assert rows[0].startswith("-- Title ") and rows[-1].endswith(" esc -")
    lines, _caret = input_box("hi", 2, 20, p)
    assert lines[0] == "-" * 20 and lines[1].startswith("| hi")
    assert all(line.isascii() for line in lines)


def test_glyph_sets_are_switched_not_copied_by_the_module():
    use(True)
    assert glyphs().ascii
    use(False)
    assert not glyphs().ascii
    assert glyphs_module._DEFAULT is not None
