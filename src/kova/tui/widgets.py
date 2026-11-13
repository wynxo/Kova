"""The pieces of Kova's live region, each rendered to styled lines.

Everything here is pure: given state and a width it returns lines (and, for
the input box, where the caret goes). The TUI loop decides which widgets are
on screen and the ``Screen`` draws them.

Ported and adapted from Kaine's ``kaine/cli_widgets.py``; colours and wording
match Kova.
"""
from __future__ import annotations

import os
import random

from .markdown import ACCENT, BASH, MEMORY, MUTED, SELECT, USER, WARNING, Palette, display_width, \
    strip_ansi

GLYPHS = "\u00b7\u2722\u2733\u2736\u273b\u273d\u273b\u2736\u2733\u2722"
VERBS = ("Thinking", "Pondering", "Mulling", "Working", "Brewing", "Crafting", "Tinkering",
         "Reckoning", "Percolating", "Conjuring", "Synthesizing", "Untangling", "Sketching",
         "Considering", "Noodling", "Cooking", "Musing", "Assembling", "Weaving", "Distilling")
PLACEHOLDERS = ('Try "what can you do?"', 'Try "remind me to stretch in an hour"',
                'Try "summarize @README.md"', 'Try "search the web for today\'s news"',
                'Try "what do you remember about me?"')
SHORTCUTS = (("! for a shell command", "/ for commands", "@ for file paths", "# to memorize"),
             ("double tap esc to clear input", "\\\u23ce or shift+\u23ce for newline",
              "esc to interrupt", "ctrl+l to clear the screen"),
             ("shift+tab to cycle permissions", "tab to complete", "\u2191\u2193 for history",
              "ctrl+d or ctrl+c twice to exit"))

MODE_COLOURS = {"manual": "38;5;81", "auto": "38;5;215", "full": "38;5;203"}
_TRUECOLOR = os.environ.get("COLORTERM", "").lower() in ("truecolor", "24bit")
_REDUCED = bool(os.environ.get("KOVA_REDUCED_MOTION"))


def reduced_motion() -> bool:
    return _REDUCED


def pad(text: str, width: int) -> str:
    """Pad a styled string with spaces to ``width`` visible columns."""
    return text + " " * max(0, width - display_width(strip_ansi(text)))


def fit(text: str, width: int) -> str:
    """Shorten plain text to ``width`` columns with an ellipsis."""
    text = " ".join(str(text).split()) if "\n" not in str(text) else str(text)
    if display_width(text) <= width:
        return text
    out, used = "", 0
    for char in text:
        step = display_width(char)
        if used + step > max(0, width - 1):
            break
        out, used = out + char, used + step
    return out + "\u2026"


def wrap(text: str, width: int) -> list[str]:
    """Hard-wrap plain text into rows of at most ``width`` columns."""
    rows = []
    for line in str(text).split("\n"):
        row, used = "", 0
        for char in line:
            step = display_width(char)
            if used + step > width:
                rows.append(row)
                row, used = "", 0
            row, used = row + char, used + step
        rows.append(row)
    return rows


# ------------------------------------------------------------------- effects
def shimmer(text: str, phase: float, p: Palette, base=(180, 169, 255),
            light=(230, 226, 255)) -> str:
    """Text with a soft highlight sweeping across it; ``phase`` is the highlight's
    position in characters (it may run past either end)."""
    if not p.color:
        return text
    if _REDUCED:
        return p.paint(ACCENT, text)
    out = []
    for index, char in enumerate(text):
        strength = max(0.0, 1.0 - abs(index - phase) / 2.5)
        if _TRUECOLOR:
            rgb = [round(b + (h - b) * strength) for b, h in zip(base, light, strict=False)]
            out.append(f"\x1b[38;2;{rgb[0]};{rgb[1]};{rgb[2]}m{char}")
        else:
            code = 231 if strength > 0.7 else 189 if strength > 0.35 else 183
            out.append(f"\x1b[38;5;{code}m{char}")
    return "".join(out) + "\x1b[0m"


def glyph(frame: int) -> str:
    return "\u273b" if _REDUCED else GLYPHS[frame % len(GLYPHS)]


def spinner_line(p: Palette, *, verb: str, frame: int, elapsed: float, tokens: int,
                 stopping: bool, thinking: bool = False) -> str:
    from .render import compact_number, duration
    label = "Stopping" if stopping else verb
    phase = (frame * 0.9) % (len(label) + 8) - 4
    parts = [duration(elapsed)]
    if tokens:
        parts.append(f"\u2193 {compact_number(tokens)} tokens")
    if thinking and not stopping:
        parts.append("thinking")
    if not stopping:
        parts.append("esc to interrupt")
    return (p.paint(ACCENT, glyph(frame)) + " " + shimmer(label + "\u2026", phase, p)
            + p.dim(f" ({' \u00b7 '.join(parts)})"))


def pulse(p: Palette, frame: int, colour: str = ACCENT) -> str:
    """A bullet that breathes while its tool runs."""
    if not p.color or _REDUCED:
        return p.paint(colour, "\u2699")
    return p.paint(colour, "\u2699") if (frame // 5) % 2 == 0 else p.dim("\u2699")


# ------------------------------------------------------------------- frames
def box(lines: list[str], width: int, p: Palette, colour: str = MUTED, *,
        title: str = "") -> list[str]:
    """Rounded frame around styled lines, ``width`` columns wide in total."""
    inner = width - 4
    top = "\u256d" + "\u2500" * (width - 2) + "\u256e"
    if title:
        top = "\u256d\u2500 " + title + " " + "\u2500" * max(0, width - 5 - display_width(title)) + "\u256e"
    rows = [p.paint(colour, top)]
    for line in lines:
        rows.append(p.paint(colour, "\u2502") + " " + pad(line, inner) + " " + p.paint(colour, "\u2502"))
    rows.append(p.paint(colour, "\u2570" + "\u2500" * (width - 2) + "\u256f"))
    return rows


def input_box(text: str, cursor: int, width: int, p: Palette, *, placeholder: str = "",
              max_rows: int = 10, dim: bool = False) -> tuple[list[str], tuple[int, int]]:
    """The prompt box. A leading ``!`` or ``#`` becomes the prompt glyph and
    tints the frame (shell and memory modes). Returns lines and the caret."""
    mode = text[:1] if text[:1] in ("!", "#") else ""
    colour = {"!": BASH, "#": MEMORY}.get(mode, MUTED)
    shown, caret = (text[1:], max(0, cursor - 1)) if mode else (text, cursor)
    if mode and shown.startswith(" "):  # "# note" reads as "#" then the note
        shown, caret = shown[1:], max(0, caret - 1)
    prompt = mode or "\u203a"
    inner = max(8, width - 6)
    rows: list[tuple[str, int, bool]] = []  # (text, offset in ``shown``, ends its line)
    offset = 0
    for line in shown.split("\n"):
        pieces = wrap(line, inner)
        position = offset
        for index, piece in enumerate(pieces):
            rows.append((piece, position, index == len(pieces) - 1))
            position += len(piece)
        offset += len(line) + 1
    caret_row = caret_col = 0
    for index, (piece, start, last) in enumerate(rows):
        if start <= caret < start + len(piece) or (caret == start + len(piece) and last):
            caret_row, caret_col = index, display_width(piece[:caret - start])
            break
    if caret_col >= inner:  # the caret sits past a full row: give it the next one
        rows.insert(caret_row + 1, ("", 0, True))
        caret_row, caret_col = caret_row + 1, 0
    rows = [row[0] for row in rows]
    first = max(0, min(caret_row - max_rows + 1, len(rows) - max_rows))
    visible = rows[first:first + max_rows]
    glyph_style = {"!": BASH, "#": MEMORY}.get(mode, "1;" + USER)
    body = []
    for index, row in enumerate(visible):
        lead = p.paint(glyph_style, prompt) + " " if index + first == 0 else "  "
        content = p.paint("2", row) if dim else row
        if not text and index == 0 and placeholder:
            content = p.paint("2;3", fit(placeholder, inner))
        body.append(lead + content)
    lines = box(body, width, p, colour)
    return lines, (1 + caret_row - first, 4 + caret_col)


def menu(items, selected: int, width: int, p: Palette, rows: int = 8) -> list[str]:
    """Suggestions under the input box, the selected one highlighted."""
    if not items:
        return []
    start = max(0, min(selected - rows // 2, len(items) - rows))
    shown = items[start:start + rows]
    label_width = min(32, max(display_width(item.label) for item in shown) + 2)
    lines = []
    for index, item in enumerate(shown, start):
        label = fit(item.label, label_width - 1).ljust(label_width)
        detail = fit(item.detail, max(0, width - label_width - 4))
        if index == selected:
            lines.append("  " + p.paint("1;" + SELECT, label) + p.paint(SELECT, detail))
        else:
            lines.append("  " + label + p.dim(detail))
    if len(items) > rows:
        lines.append(p.dim(f"  {selected + 1}/{len(items)}"))
    return [line.rstrip() for line in lines]


def hint_line(left: str, right: str, width: int) -> str:
    gap = width - 2 - display_width(strip_ansi(left)) - display_width(strip_ansi(right))
    if gap < 2:
        return "  " + left
    return "  " + left + " " * gap + right


def shortcuts(width: int, p: Palette) -> list[str]:
    columns = 3 if width >= 100 else 2 if width >= 64 else 1
    cells = [item for group in SHORTCUTS for item in group]
    per = -(-len(cells) // columns)
    groups = [cells[i * per:(i + 1) * per] for i in range(columns)]
    column_width = (width - 4) // columns
    lines = []
    for row in range(per):
        parts = [fit(group[row], column_width - 2).ljust(column_width) if row < len(group) else ""
                 for group in groups]
        lines.append("  " + p.dim("".join(parts).rstrip()))
    return lines


def welcome(p: Palette, width: int, *, version: str, model: str, mode: str, cwd: str,
            memory: str = "", notes: int | None = None, web: bool = True, offline: bool = False,
            phase: float | None = None, tips: bool = True) -> list[str]:
    """The start-up card: Kova's ``\u25c6 Kova \u00b7 model \u00b7 mode`` header in a frame.
    ``phase`` sweeps a highlight across the name while it animates in."""
    name = p.paint("1;" + ACCENT, "Kova") if phase is None else shimmer("Kova", phase, p)
    heading = p.paint("1;" + ACCENT, "\u25c6 ") + name + p.dim("  \u00b7  model ") + p.bold(model)
    if offline:
        heading += p.paint("1;" + WARNING, "  \u00b7  offline")
    heading += p.dim("  \u00b7  mode ") + p.bold(mode)
    if not web:
        heading += p.dim("  \u00b7  web off")
    room = max(10, width - 16)
    content = [heading + p.dim(f"   v{version}"), "",
               p.dim("  /help for commands \u00b7 ? for shortcuts \u00b7 ctrl+d to leave"), "",
               p.dim("  folder  ") + fit(cwd, room)]
    if memory:
        count = "" if notes is None else f" \u00b7 {notes} {'note' if notes == 1 else 'notes'}"
        content.append(p.dim("  memory  ") + fit(memory, room - len(count)) + p.dim(count))
    size = min(width - 1, max(52, max(display_width(strip_ansi(line)) for line in content) + 4))
    lines = box(content, size, p, ACCENT)
    if offline:
        lines.append(p.paint(WARNING, " The model is unreachable \u2014 replies may fail. "
                                      "Check kova setup or /model."))
    if tips:
        lines += [
            "",
            p.dim(" Tips for getting started:"),
            "",
            p.dim(" 1. Ask Kova to search the web, work with files, or set reminders"),
            p.dim(" 2. Type / for commands, @ to attach a file, ! to run a shell command"),
            p.dim(" 3. Press shift+tab to change when Kova asks before acting"),
        ]
    return lines


# ------------------------------------------------------------------- dialogs
class Choice:
    """A framed question answered with \u2191\u2193 and Enter, number keys, or letters."""

    def __init__(self, title: str, body: list[str], options: list[tuple[str, object, str]], *,
                 colour: str = MUTED, footer: str = ""):
        self.title, self.body, self.options = title, body, options  # (label, value, hotkey)
        self.colour, self.footer = colour, footer
        self.selected = 0

    def move(self, step: int) -> None:
        self.selected = (self.selected + step) % len(self.options)

    def pick(self, key: str):
        """The value for a pressed key, or None when it answers nothing."""
        if key.isdigit() and 1 <= int(key) <= len(self.options):
            return self.options[int(key) - 1][1]
        for _label, value, hotkey in self.options:
            if hotkey and key.lower() == hotkey:
                return value
        return None

    @property
    def value(self):
        return self.options[self.selected][1]

    def render(self, width: int, p: Palette) -> list[str]:
        inner = width - 4
        lines = [p.bold(self.title), *(line for line in self.body), ""]
        for index, (label, _value, _hotkey) in enumerate(self.options):
            text = fit(f"{index + 1}. {label}", inner - 2)
            lines.append(p.paint("1;" + SELECT, "\u276f " + text) if index == self.selected
                         else "  " + text)
        if self.footer:
            lines += ["", p.dim(self.footer)]
        return box(lines, width, p, self.colour)


class Picker:
    """Choose one of many (models, conversations, modes); typing filters."""

    ROWS = 10

    def __init__(self, title: str, items: list[tuple[str, str, object]], on_pick, *,
                 current=None, subtitle: str = "", groups: dict | None = None):
        self.title, self.items, self.on_pick = title, items, on_pick  # (label, detail, value)
        self.groups = groups or {}
        self.subtitle, self.current = subtitle, current
        self.query = ""
        self.selected = next((i for i, item in enumerate(items) if item[2] == current), 0)

    @property
    def shown(self) -> list:
        if not self.query:
            return self.items
        from .editor import rank
        return rank(self.items, self.query, key=lambda item: item[0])

    def move(self, step: int) -> None:
        if self.shown:
            self.selected = (self.selected + step) % len(self.shown)

    def type(self, text: str) -> None:
        self.query += text
        self.selected = 0

    def erase(self) -> None:
        self.query = self.query[:-1]
        self.selected = 0

    @property
    def value(self):
        shown = self.shown
        return shown[self.selected][2] if shown else None

    def render(self, width: int, p: Palette) -> list[str]:
        inner = width - 4
        lines = [p.bold(self.title)]
        if self.subtitle:
            lines.append(p.dim(fit(self.subtitle, inner)))
        if self.query:
            lines.append(p.dim("Filter: ") + self.query)
        lines.append("")
        shown = self.shown
        start = max(0, min(self.selected - self.ROWS // 2, len(shown) - self.ROWS))
        label_width = min(40, max([display_width(item[0]) for item in shown] + [8]) + 2)
        previous_group = None
        for index, (label, detail, value) in enumerate(shown[start:start + self.ROWS], start):
            group = self.groups.get(value)
            if group and group != previous_group:
                lines.append(p.bold(fit(group, inner)))
                previous_group = group
            mark = " \u2714" if value == self.current and self.current is not None else ""
            text = fit(f"{index + 1}. {label}", label_width + 3).ljust(label_width + 4)
            rest = fit(detail + mark, max(0, inner - label_width - 8))
            if index == self.selected:
                lines.append(p.paint("1;" + SELECT, "\u276f " + text) + p.paint(SELECT, rest))
            else:
                lines.append("  " + text + p.dim(rest))
        if not shown:
            lines.append(p.dim("  Nothing matches"))
        lines += ["", p.dim(fit("\u2191\u2193 to move \u00b7 enter to select \u00b7 type to filter "
                                "\u00b7 esc to cancel", inner))]
        return box(lines, width, p)


def pick_verb(rng: random.Random | None = None) -> str:
    return (rng or random).choice(VERBS)


def pick_placeholder(rng: random.Random | None = None) -> str:
    return (rng or random).choice(PLACEHOLDERS)


__all__ = ['ACCENT', 'BASH', 'GLYPHS', 'MEMORY', 'MODE_COLOURS', 'MUTED', 'PLACEHOLDERS',
           'SELECT', 'SHORTCUTS', 'USER', 'VERBS', 'Choice', 'Picker', 'box', 'fit', 'glyph',
           'hint_line', 'input_box', 'menu', 'pad', 'pick_placeholder', 'pick_verb', 'pulse',
           'reduced_motion', 'shimmer', 'shortcuts', 'spinner_line', 'welcome', 'wrap']
