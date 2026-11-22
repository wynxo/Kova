"""The terminal chat's glyphs, with a plain-ASCII set for terminals that need it.

Every symbol the trace, the rules and the dialogs draw is named here once.
``KOVA_ASCII=1``, a non-UTF-8 output encoding or the Linux console
(``TERM=linux``, whose font lacks most of these) switch to ASCII; the meaning
never depends on the glyph alone, because each step also names itself in words.
See docs/TERMINAL-DESIGN.md for what each one stands for.
"""
from __future__ import annotations

import os
import sys

UNICODE = {
    "think": "∴",      # ∴ reasoning
    "look": "▹",       # ▹ a tool that only reads
    "change": "▸",     # ▸ a tool that can change things
    "ask": "◆",        # ◆ waiting on you
    "plan": "≡",       # ≡ the checklist
    "failed": "✗",     # ✗
    "stopped": "■",    # ■
    "steer": "↳",      # ↳ a message slipped into a running turn
    "note": "·",       # · a quiet notice on the spine
    "spine": "│",      # │
    "branch": "├",     # ├
    "last": "└",       # └
    "fork": "┬",       # ┬
    "rule": "─",       # ─
    "user": "▌",       # ▌ the user's bar
    "gate": "┃",       # ┃ the approval bar
    "done": "✓",       # ✓ plan item done
    "doing": "▸",      # ▸ plan item in progress
    "todo": "◦",       # ◦ plan item to do
    "bar_full": "▰",   # ▰ progress
    "bar_empty": "▱",  # ▱
    "pointer": "▸",    # ▸ selected row in a list
    "caret": "▏",      # ▏ a typed filter's caret
    "ellipsis": "…",
    "minus": "−",
    "enter": "⏎",
    "updown": "↑↓",
}
SPIN_UNICODE = ("◐", "◓", "◑", "◒")   # ◐◓◑◒ the live node turning

ASCII = {
    "think": ":", "look": ">", "change": ">", "ask": "?", "plan": "=", "failed": "x",
    "stopped": "#", "steer": ">", "note": "-", "spine": "|", "branch": "|", "last": "`",
    "fork": "+", "rule": "-", "user": "|", "gate": "|", "done": "x", "doing": ">",
    "todo": "-", "bar_full": "#", "bar_empty": ".", "pointer": ">", "caret": "_",
    "ellipsis": "...", "minus": "-", "enter": "enter", "updown": "up/down",
}
SPIN_ASCII = ("-", "\\", "|", "/")


def ascii_only(env=None, stream=None) -> bool:
    """Whether to draw with ASCII: asked for, or the terminal cannot show the rest."""
    env = os.environ if env is None else env
    forced = env.get("KOVA_ASCII", "").strip().lower()
    if forced in ("1", "true", "yes", "on"):
        return True
    if forced in ("0", "false", "no", "off"):
        return False
    if env.get("TERM", "") == "linux":
        return True
    encoding = getattr(stream if stream is not None else sys.stdout, "encoding", None) or "utf-8"
    return "utf" not in encoding.lower().replace("-", "")


class Glyphs:
    """One glyph set: ``g.think``, ``g.spin(frame)`` ..."""

    def __init__(self, ascii: bool | None = None):
        self.ascii = ascii_only() if ascii is None else bool(ascii)
        table = ASCII if self.ascii else UNICODE
        for name, value in table.items():
            setattr(self, name, value)
        self._spin = SPIN_ASCII if self.ascii else SPIN_UNICODE

    def spin(self, frame: int) -> str:
        """The live node at animation ``frame``."""
        return self._spin[frame % len(self._spin)]

    def __getitem__(self, name: str) -> str:
        return getattr(self, name)


_DEFAULT: Glyphs | None = None


def glyphs() -> Glyphs:
    """The process-wide glyph set, decided once from the environment."""
    global _DEFAULT
    if _DEFAULT is None:
        _DEFAULT = Glyphs()
    return _DEFAULT


def use(ascii: bool | None) -> Glyphs:
    """Switch the process-wide set (tests, ``KOVA_ASCII``); returns it."""
    global _DEFAULT
    _DEFAULT = Glyphs(ascii)
    return _DEFAULT


__all__ = ["ASCII", "Glyphs", "SPIN_ASCII", "SPIN_UNICODE", "UNICODE", "ascii_only", "glyphs", "use"]
