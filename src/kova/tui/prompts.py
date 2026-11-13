"""Inline pickers for the setup wizard, drawn like the chat's own.

``select`` asks for one of several choices and ``multi_select`` for any
number; both draw a framed list under the cursor (no full-screen takeover),
take arrow keys, number keys and typed filters, and leave one tidy summary
line behind once answered. They return ``None`` when cancelled (Esc or
Ctrl+C), and raise ``Unavailable`` where raw keys are impossible (a pipe,
Windows), so the caller can fall back to a plain numbered prompt.
"""
from __future__ import annotations

import os
import sys

from .style import ACCENT, ACCENT_SOFT, FAINT, Palette, colour_level
from .widgets import box, fit, pad


class Unavailable(RuntimeError):
    """No interactive POSIX terminal to draw a picker in."""


class _List:
    """The state of an open picker: rows, the highlighted row, a filter and marks."""

    ROWS = 10

    def __init__(self, title: str, choices: list[str], *, multi: bool, default=0,
                 marked: set[int] | None = None, subtitle: str = ""):
        self.title, self.choices, self.multi, self.subtitle = title, choices, multi, subtitle
        self.query = ""
        self.marked = set(marked or ())
        self.selected = default if isinstance(default, int) and 0 <= default < len(choices) else 0

    @property
    def shown(self) -> list[int]:
        if not self.query:
            return list(range(len(self.choices)))
        query = self.query.casefold()
        return [index for index, label in enumerate(self.choices) if query in label.casefold()]

    def move(self, step: int) -> None:
        shown = self.shown
        if shown:
            position = shown.index(self.selected) if self.selected in shown else 0
            self.selected = shown[(position + step) % len(shown)]

    def type(self, text: str) -> None:
        self.query += text
        shown = self.shown
        self.selected = shown[0] if shown else self.selected

    def erase(self) -> None:
        self.query = self.query[:-1]

    def render(self, width: int, p: Palette) -> list[str]:
        inner = width - 4
        lines = [p.dim(fit(self.subtitle, inner))] if self.subtitle else []
        if self.query or len(self.choices) > self.ROWS:
            lines.append(p.faint("Filter: ") + self.query + p.paint(ACCENT, "▏")
                         if self.query else p.faint("Type to filter"))
        if lines:
            lines.append("")
        shown = self.shown
        position = shown.index(self.selected) if self.selected in shown else 0
        start = max(0, min(position - self.ROWS // 2, len(shown) - self.ROWS))
        for index in shown[start:start + self.ROWS]:
            label = fit(self.choices[index], inner - 8)
            box_mark = ""
            if self.multi:
                box_mark = (p.green("◉ ") if index in self.marked else p.faint("○ ")) \
                    if p.color else ("[x] " if index in self.marked else "[ ] ")
            if index == self.selected:
                lines.append(p.paint("1;" + ACCENT, "❯ ") + box_mark
                             + p.paint("1;" + ACCENT_SOFT, label))
            else:
                lines.append("  " + box_mark + label)
        if not shown:
            lines.append(p.dim("  Nothing matches"))
        if len(shown) > self.ROWS:
            lines.append(p.faint(f"  {position + 1}/{len(shown)}"))
        lines.append("")
        keys = ("↑↓ move · space mark · enter done · esc cancel" if self.multi
                else "↑↓ move · enter choose · esc cancel")
        return box(lines, width, p, ACCENT, title=p.bold(fit(self.title, width - 8)),
                   footer=p.faint(fit(keys, width - 10)))


def _run(state: _List, out=None):
    """Draw ``state`` under the cursor and answer keys until it is decided."""
    if os.name != "posix" or not sys.stdin.isatty() or not sys.stdout.isatty():
        raise Unavailable("no interactive terminal")
    from .terminal import KeyReader, RawMode, Screen
    out = out or sys.stdout
    p = Palette(colour_level(stream=out))
    screen = Screen(out)
    fd = sys.stdin.fileno()
    reader = KeyReader(fd)
    answer = None
    with RawMode(fd, out):
        try:
            while True:
                width = min(screen.size()[0] - 1, 72)
                screen.set_live(["", *state.render(width, p)])
                screen.render()
                for key in reader.read(0.5):
                    name = key.name
                    if name in ("up", "down"):
                        state.move(-1 if name == "up" else 1)
                    elif name in ("esc", "ctrl-c", "ctrl-d"):
                        return None
                    elif name == "enter":
                        if not state.shown:
                            continue
                        if state.multi:
                            answer = sorted(state.marked or {state.selected})
                        else:
                            answer = state.selected
                        return answer
                    elif name == "backspace":
                        state.erase()
                    elif name == "text":
                        text = key.text
                        if state.multi and text == " ":
                            state.marked ^= {state.selected}
                        elif text.isdigit() and not state.query and not state.multi \
                                and 1 <= int(text) <= len(state.choices) <= 9:
                            state.selected = int(text) - 1
                        else:
                            state.type(text)
        finally:
            screen.close()
            if answer is not None:
                chosen = answer if isinstance(answer, list) else [answer]
                names = ", ".join(state.choices[index] for index in chosen)
                out.write(summary(p, state.title, names) + "\n")
                out.flush()


def summary(p: Palette, title: str, value: str) -> str:
    """The line a picker leaves behind: ``✓ Title  value``."""
    return (p.green("✓") + " " + pad(p.paint(FAINT if p.level >= 2 else "2", title), 15)
            + p.paint("1;" + ACCENT_SOFT, fit(value, 60)))


def select(title: str, choices: list[str], *, default: int | None = 0, subtitle: str = "",
           out=None) -> int | None:
    """The index of the chosen row, or None if cancelled."""
    return _run(_List(title, list(choices), multi=False, default=default or 0,
                      subtitle=subtitle), out)


def multi_select(title: str, choices: list[str], *, marked=(0,), subtitle: str = "",
                 out=None) -> list[int] | None:
    """The indexes of the marked rows (the highlighted one if none), or None."""
    return _run(_List(title, list(choices), multi=True, marked=set(marked),
                      subtitle=subtitle), out)


__all__ = ["Unavailable", "multi_select", "select", "summary"]
