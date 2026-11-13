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

from kova import theme

from .markdown import (
    ACCENT,
    BASH,
    MEMORY,
    MUTED,
    SELECT,
    USER,
    WARNING,
    Palette,
    display_width,
    strip_ansi,
)
from .style import ACCENT_SOFT, BORDER, FAINT, SUCCESS, blend, fg, rgb

SPINNER = "\u280b\u2819\u2839\u2838\u283c\u2834\u2826\u2827\u2807\u280f"   # braille dots
GLYPHS = SPINNER
VERBS = ("Thinking", "Pondering", "Mulling", "Working", "Brewing", "Crafting", "Tinkering",
         "Reckoning", "Percolating", "Conjuring", "Synthesizing", "Untangling", "Sketching",
         "Considering", "Noodling", "Cooking", "Musing", "Assembling", "Weaving", "Distilling")
PLACEHOLDERS = ('Try "what can you do?"', 'Try "remind me to stretch in an hour"',
                'Try "summarize @README.md"', 'Try "search the web for today\'s news"',
                'Try "what do you remember about me?"')
# (keys, what they do), in the order the ? panel lists them.
SHORTCUTS = (("ctrl+p", "command palette"), ("pgup/pgdn", "scroll expanded details"), ("ctrl+t", "toggle full reasoning"), ("ctrl+o", "toggle tool details"), ("!", "run a shell command"), ("/", "commands"), ("@", "attach a file"),
             ("#", "remember a note"), ("esc", "interrupt Kova"), ("esc esc", "clear the input"),
             ("shift+\u23ce", "new line (or \\\u23ce)"), ("shift+tab", "cycle permissions"),
             ("tab", "complete"), ("\u2191 \u2193", "earlier messages"),
             ("ctrl+l", "clear the screen"), ("ctrl+d", "exit (or ctrl+c twice)"))
LOGO = ("\u2588\u2584\u2580 \u2588\u2580\u2588 \u2588 \u2588 \u2584\u2580\u2588",
        "\u2588 \u2588 \u2588\u2584\u2588 \u2580\u2584\u2580 \u2588\u2580\u2588")

MODE_COLOURS = {"manual": fg(theme.MEMORY), "auto": fg(theme.WARNING), "full": fg(theme.ERROR)}
MODE_CHIPS = {"work": theme.ACCENT, "chat": theme.USER}
_REDUCED = bool(os.environ.get("KOVA_REDUCED_MOTION"))


def reduced_motion() -> bool:
    return _REDUCED


def pad(text: str, width: int) -> str:
    """Pad a styled string with spaces to ``width`` visible columns, cutting
    it (escapes kept) if it is wider."""
    size = visible(text)
    if size > width:
        from .terminal import _clip
        return _clip(text, width) + ("\x1b[0m" if "\x1b[" in text else "")
    return text + " " * (width - size)


def visible(text: str) -> int:
    """Columns a styled string takes on screen."""
    return display_width(strip_ansi(text))


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


def fit_path(path: str, width: int) -> str:
    """Shorten a path from the middle, keeping its start and the last folder."""
    if display_width(path) <= width or width < 8:
        return fit(path, width)
    head, _, tail = path.rpartition("/")
    tail = "/" + tail
    keep = width - display_width(tail) - 1
    if keep < 4:
        return fit(path, width)
    return fit(head, keep + 1)[:-1] + "\u2026" + tail


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
def shimmer(text: str, phase: float, p: Palette, base=None, light=(240, 236, 255)) -> str:
    """Text with a soft highlight sweeping across it; ``phase`` is the highlight's
    position in characters (it may run past either end)."""
    if not p.color:
        return text
    if _REDUCED or p.level == 1:
        return p.paint(ACCENT_SOFT, text)
    base = base or rgb(theme.ACCENT_SOFT)
    out = []
    for index, char in enumerate(text):
        strength = max(0.0, 1.0 - abs(index - phase) / 2.5)
        if p.level >= 3:
            colour = [round(b + (h - b) * strength) for b, h in zip(base, light, strict=False)]
            out.append(f"\x1b[38;2;{colour[0]};{colour[1]};{colour[2]}m{char}")
        else:
            code = 231 if strength > 0.7 else 189 if strength > 0.35 else 147
            out.append(f"\x1b[38;5;{code}m{char}")
    return "".join(out) + "\x1b[0m"


def glyph(frame: int) -> str:
    return "\u25cf" if _REDUCED else SPINNER[frame % len(SPINNER)]


def spinner_glyph(p: Palette, frame: int) -> str:
    """The spinner, its colour drifting slowly along Kova's gradient."""
    if _REDUCED or p.level < 2:
        return p.paint(ACCENT, glyph(frame))
    cycle = (frame % 40) / 20
    position = cycle if cycle <= 1 else 2 - cycle
    return p.paint("1;" + fg(blend(theme.GRADIENT, position)), glyph(frame))


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
    return (spinner_glyph(p, frame) + " " + shimmer(label + "\u2026", phase, p)
            + "  " + p.faint(" \u00b7 ".join(parts)))


def pulse(p: Palette, frame: int, colour: str = ACCENT) -> str:
    """The spinner beside a tool that is running."""
    return p.paint(colour, glyph(frame))


# ------------------------------------------------------------------- frames
def _edge(text: str, p: Palette, colour: str, gradient: bool) -> str:
    return p.gradient(text) if gradient and p.level >= 2 else p.paint(colour, text)


def box(lines: list[str], width: int, p: Palette, colour: str = BORDER, *,
        title: str = "", footer: str = "", gradient: bool = False) -> list[str]:
    """A rounded frame around styled lines, ``width`` columns wide in total.

    ``title`` sits in the top edge, ``footer`` at the right of the bottom one;
    ``gradient`` runs Kova's colours along the frame instead of ``colour``.
    """
    inner = width - 4
    if title:
        title = " " + title + " "
        rest = max(0, width - 3 - visible(title))
        top = _edge("\u256d\u2500", p, colour, gradient) + title + _edge(
            "\u2500" * rest + "\u256e", p, colour, gradient)
        if gradient and p.level >= 2:  # keep the colour flowing across the title
            top = (p.gradient("\u256d\u2500", (theme.GRADIENT[0], theme.GRADIENT[0])) + title
                   + p.gradient("\u2500" * rest + "\u256e", theme.GRADIENT[1:]))
    else:
        top = _edge("\u256d" + "\u2500" * (width - 2) + "\u256e", p, colour, gradient)
    if footer:
        footer = " " + footer + " "
        rest = max(0, width - 3 - visible(footer))
        bottom = (_edge("\u2570" + "\u2500" * rest, p, colour, gradient) + footer
                  + _edge("\u2500\u256f", p, colour, gradient))
    else:
        bottom = _edge("\u2570" + "\u2500" * (width - 2) + "\u256f", p, colour, gradient)
    left = p.paint(fg(theme.GRADIENT[0]), "\u2502") if gradient and p.level >= 2 \
        else p.paint(colour, "\u2502")
    right = p.paint(fg(theme.GRADIENT[-1]), "\u2502") if gradient and p.level >= 2 \
        else p.paint(colour, "\u2502")
    rows = [top]
    for line in lines:
        rows.append(left + " " + pad(line, inner) + " " + right)
    rows.append(bottom)
    return rows


def input_box(text: str, cursor: int, width: int, p: Palette, *, placeholder: str = "",
              max_rows: int = 10, dim: bool = False, footer: str = "",
              busy: bool = False) -> tuple[list[str], tuple[int, int]]:
    """The prompt box. A leading ``!`` or ``#`` becomes the prompt glyph and
    tints the frame (shell and memory modes); otherwise the frame lights up in
    Kova's violet while there is text in it. Returns lines and the caret."""
    mode = text[:1] if text[:1] in ("!", "#") else ""
    colour = {"!": BASH, "#": MEMORY}.get(mode) or (ACCENT if text and not busy else FAINT)
    shown, caret = (text[1:], max(0, cursor - 1)) if mode else (text, cursor)
    if mode and shown.startswith(" "):  # "# note" reads as "#" then the note
        shown, caret = shown[1:], max(0, caret - 1)
    prompt = mode or "\u276f"
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
    visible_rows = rows[first:first + max_rows]
    glyph_style = {"!": "1;" + BASH, "#": "1;" + MEMORY}.get(mode) or \
        ("1;" + (FAINT if busy else ACCENT))
    body = []
    for index, row in enumerate(visible_rows):
        lead = p.paint(glyph_style, prompt) + " " if index + first == 0 else "  "
        content = p.dim(row) if dim else row
        if not text and index == 0 and placeholder:
            content = p.paint(f"3;{FAINT}" if p.level >= 2 else "2;3", fit(placeholder, inner))
        body.append(lead + content)
    lines = box(body, width, p, colour, footer=fit(footer, max(0, width - 8)) if footer else "")
    return lines, (1 + caret_row - first, 4 + caret_col)


def menu(items, selected: int, width: int, p: Palette, rows: int = 8) -> list[str]:
    """Suggestions under the input box, the selected one marked and lit."""
    if not items:
        return []
    start = max(0, min(selected - rows // 2, len(items) - rows))
    shown = items[start:start + rows]
    label_width = min(32, max(display_width(item.label) for item in shown) + 2)
    lines = []
    for index, item in enumerate(shown, start):
        label = fit(item.label, label_width - 1).ljust(label_width)
        detail = fit(item.detail, max(0, width - label_width - 6))
        if index == selected:
            lines.append("  " + p.paint("1;" + ACCENT, "\u276f ") + p.paint("1;" + SELECT, label)
                         + p.paint(SELECT, detail))
        else:
            lines.append("    " + label + p.dim(detail))
    if len(items) > rows:
        lines.append(p.faint(f"    {selected + 1}/{len(items)}"))
    return [line.rstrip() for line in lines]


def hint_line(left: str, right: str, width: int) -> str:
    gap = width - 2 - visible(left) - visible(right)
    if gap < 2:
        return "  " + left
    return "  " + left + " " * gap + right


def status_bar(p: Palette, width: int, *, mode: str, model: str, title: str = "",
               permission: str | None = None, web: bool = True, plan: str = "",
               stats: str = "", offline: bool = False, cwd: str = "") -> str:
    """The bottom line: a mode chip, the model, the chat, and the last turn's
    numbers on the right. Parts drop off from the least useful end as the
    terminal narrows, so it never wraps."""
    chip = p.pill(mode.upper(), MODE_CHIPS.get(mode, theme.ACCENT), solid=True) if p.color \
        else f"[{mode}]"
    dot = p.paint(WARNING if offline else SUCCESS, "\u25cf")

    def model_part(room: int) -> str:
        return (dot + " " + p.bold(fit(model or "no model", max(1, room)))
                + (p.yellow(" offline") if offline else ""))
    parts: list[tuple[int, str]] = [(0, model_part(60))]
    if title:
        parts.append((2, p.dim(fit(title, 28))))
    if permission:
        colour = MODE_COLOURS.get(permission, MUTED)
        parts.append((3, p.paint(colour, "\u23f5\u23f5 " + permission)
                      if permission != "manual" else p.dim(permission)))
    if not web:
        parts.append((4, p.dim("web off")))
    if plan:
        parts.append((1, p.dim("plan " + plan)))
    right_parts = [part for part in (stats, cwd) if part]
    separator = p.faint(" \u00b7 ")

    def render(keep: list[tuple[int, str]], right: list[str]) -> str:
        left = " " + chip + " " + separator.join(text for _rank, text in keep)
        tail = separator.join(p.faint(part) for part in right)
        gap = width - visible(left) - visible(tail) - 1
        return left + " " * max(1, gap) + tail if tail else left

    keep = list(parts)
    right = list(right_parts)
    while True:
        line = render(keep, right)
        if visible(line) <= width - 1:
            return line
        if right:
            right.pop()
        elif len(keep) > 1:
            keep.remove(max(keep[1:], key=lambda part: part[0]))
        else:
            room = width - 1 - visible(render([(0, model_part(1))], [])) + 1
            return render([(0, model_part(room))], [])


def shortcuts(width: int, p: Palette) -> list[str]:
    columns = 3 if width >= 100 else 2 if width >= 64 else 1
    per = -(-len(SHORTCUTS) // columns)
    groups = [SHORTCUTS[i * per:(i + 1) * per] for i in range(columns)]
    column_width = (width - 4) // columns
    key_width = max(display_width(key) for key, _ in SHORTCUTS) + 2
    lines = []
    for row in range(per):
        cells = []
        for group in groups:
            if row >= len(group):
                continue
            key, text = group[row]
            cell = p.paint("1;" + ACCENT_SOFT, key.ljust(key_width)) + p.dim(
                fit(text, column_width - key_width - 2))
            cells.append(pad(cell, column_width))
        lines.append(("  " + "".join(cells)).rstrip())
    return lines


def logo(p: Palette) -> list[str]:
    """The Kova wordmark in half-block letters, shaded along the gradient."""
    return [p.gradient(row, bold=True) for row in LOGO]


def welcome(p: Palette, width: int, *, version: str, model: str, mode: str, cwd: str,
            memory: str = "", notes: int | None = None, web: bool = True, offline: bool = False,
            phase: float | None = None, tips: bool = True, title: str = "",
            permission: str | None = None) -> list[str]:
    """The start-up card: the wordmark, then model, mode, folder and memory,
    in a frame drawn in Kova's gradient. ``phase`` sweeps a highlight across
    the tagline while the card animates in."""
    size = max(30, min(width, 72))
    inner = size - 4
    tagline = "your private AI"
    if phase is not None:
        tagline = shimmer(tagline, phase, p)
    else:
        tagline = p.dim(tagline)
    tagline += p.faint(f"  v{version}")
    content = [""]
    if inner >= 40:
        mark = logo(p)
        content += ["  " + mark[0], "  " + mark[1] + "   " + tagline]
    else:
        content.append("  " + p.paint("1;" + ACCENT, "\u25c6") + " " + p.gradient("Kova", bold=True)
                       + ("  " + tagline if inner >= 36 else ""))
    content.append("")
    label_width = 9
    room = max(8, inner - label_width - 4)

    def row(label: str, value: str) -> str:
        return "  " + p.faint(label.ljust(label_width)) + value

    state = p.yellow("\u25cf offline") if offline else p.green("\u25cf") + p.dim(" ready")
    name = fit(model, max(6, room - 12))
    content.append(row("model", p.bold(name) + " " * max(2, room - display_width(name)
                                                           - visible(state)) + state))
    detail = [MODE_HINTS.get(mode, "")] if MODE_HINTS.get(mode) else []
    if not web:
        detail.insert(0, "web off")
    hint = fit(" \u00b7 ".join(detail), room - len(mode) - 2) if room - len(mode) > 8 else ""
    content.append(row("mode", p.paint("1;" + fg(MODE_CHIPS.get(mode, theme.ACCENT)), mode)
                       + (p.dim("  " + hint) if hint else "")))
    content.append(row("folder", fit_path(cwd, room)))
    if memory or notes is not None:
        count = "" if notes is None else f"{notes} {'note' if notes == 1 else 'notes'}"
        where = fit_path(memory, max(6, room - len(count) - 3)) if memory else ""
        content.append(row("memory", count + (p.faint("  " + where) if where else "")))
    content.append("")
    lines = box(content, size, p, ACCENT, gradient=True)
    if offline:
        lines += ["", "  " + p.yellow("The model is unreachable \u2014 replies may fail. "
                                      "Run kova setup or /model.")]
    if tips:
        def key(glyph: str, text: str) -> str:
            return p.paint("1;" + ACCENT_SOFT, glyph) + " " + p.dim(text)
        keys = [key("/", "commands"), key("@", "attach a file"), key("!", "shell"),
                key("#", "remember"), key("?", "shortcuts")]
        lines += ["", "  " + p.bold("Ask anything.") + p.dim(" Kova can search the web, work "
                                                            "with files and set reminders.")
                  if width >= 80 else "  " + p.bold("Ask anything."), ""]
        row_text, rows_out = "  ", []
        for item in keys:
            if visible(row_text) + visible(item) + 3 > width and row_text.strip():
                rows_out.append(row_text.rstrip())
                row_text = "  "
            row_text += item + "   "
        rows_out.append(row_text.rstrip())
        lines += rows_out
    return lines


MODE_HINTS = {"work": "web, files, memory, reminders", "chat": "conversation, web at most"}


# ------------------------------------------------------------------- dialogs
class Choice:
    """A framed question answered with \u2191\u2193 and Enter, number keys, or letters."""

    def __init__(self, title: str, body: list[str], options: list[tuple[str, object, str]], *,
                 colour: str = ACCENT, footer: str = ""):
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
        lines = [*(line for line in self.body), ""]
        for index, (label, _value, _hotkey) in enumerate(self.options):
            text = fit(f"{index + 1}. {label}", inner - 2)
            lines.append(p.paint("1;" + self.colour, "\u276f ") + p.paint("1;" + SELECT, text)
                         if index == self.selected else "  " + p.dim(text))
        lines.append("")
        return box(lines, width, p, self.colour, title=p.bold(fit(self.title, width - 8)),
                   footer=p.faint(fit(self.footer, width - 10)) if self.footer else "")


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
        lines = []
        if self.subtitle:
            lines.append(p.dim(fit(self.subtitle, inner)))
        lines.append(p.faint("Filter: ") + self.query + p.paint(ACCENT, "\u258f")
                     if self.query else p.faint("Type to filter"))
        lines.append("")
        shown = self.shown
        start = max(0, min(self.selected - self.ROWS // 2, len(shown) - self.ROWS))
        label_width = min(40, max([display_width(item[0]) for item in shown] + [8]) + 2)
        previous_group = None
        for index, (label, detail, value) in enumerate(shown[start:start + self.ROWS], start):
            group = self.groups.get(value)
            if group and group != previous_group:
                lines.append(p.paint("1;" + MUTED, fit(group, inner)))
                previous_group = group
            current = value == self.current and self.current is not None
            text = fit(f"{index + 1}. {label}", label_width + 3).ljust(label_width + 4)
            rest = fit(detail, max(0, inner - label_width - 10))
            tick = p.green(" \u2714") if current else ""
            if index == self.selected:
                lines.append(p.paint("1;" + ACCENT, "\u276f ") + p.paint("1;" + SELECT, text)
                             + p.paint(SELECT, rest) + tick)
            else:
                lines.append("  " + text + p.dim(rest) + tick)
        if not shown:
            lines.append(p.dim("  Nothing matches"))
        lines.append("")
        footer = "\u2191\u2193 move \u00b7 enter select \u00b7 esc cancel"
        return box(lines, width, p, ACCENT, title=p.bold(fit(self.title, width - 8)),
                   footer=p.faint(fit(footer, width - 10)))


class CommandPalette(Picker):
    """``Ctrl+P``: one searchable list of slash commands, models and chats.

    It is a ``Picker`` with a wider net: the filter matches each entry's label
    and its detail too (a model's id, a chat's mode), and rows are grouped when
    nothing is typed. Narrow terminals drop the details and the subtitle rather
    than clipping them.
    """

    ROWS = 9

    def __init__(self, items: list[tuple[str, str, object]], on_pick, *,
                 groups: dict | None = None,
                 subtitle: str = "Slash commands, models and recent chats"):
        super().__init__("Command palette", items, on_pick, subtitle=subtitle, groups=groups)

    @property
    def shown(self) -> list:
        if not self.query:
            return self.items
        from .editor import rank
        return rank(self.items, self.query, key=lambda item: f"{item[0]} {item[1]}")

    def render(self, width: int, p: Palette) -> list[str]:
        inner = max(8, width - 4)
        compact = width < 44
        lines = []
        if self.subtitle and not compact:
            lines.append(p.dim(fit(self.subtitle, inner)))
        prompt = "Search: "
        if self.query:
            room = max(1, inner - display_width(prompt) - 2)
            typed = self.query if display_width(self.query) <= room else self.query[-room:]
            lines.append(p.faint(prompt) + typed + p.paint(ACCENT, "\u258f"))
        else:
            lines.append(p.faint(fit(prompt + "type to filter", inner)))
        lines.append("")
        shown = self.shown
        rows = self.ROWS if width >= 40 else max(4, self.ROWS - 3)
        start = max(0, min(self.selected - rows // 2, len(shown) - rows))
        label_width = inner if compact else min(
            26, max([display_width(item[0]) for item in shown] + [8]) + 2)
        previous = None
        for index, (label, detail, value) in enumerate(shown[start:start + rows], start):
            group = self.groups.get(value)
            if group and group != previous and not compact and not self.query:
                lines.append(p.paint("1;" + MUTED, fit(group.upper(), inner)))
            previous = group
            current = value == self.current and self.current is not None
            tick = p.green(" \u2714") if current else ""
            title = fit(label, max(1, label_width - 2))
            row = (p.paint("1;" + ACCENT, "\u276f ") + p.paint("1;" + SELECT, title)
                   if index == self.selected else "  " + title)
            if not compact and detail:
                room = inner - label_width - 4 - visible(tick)
                if room > 3:
                    row += " " * max(2, label_width - visible(title)) + p.dim(fit(detail, room))
            lines.append(row + tick)
        if not shown:
            lines.append(p.dim("  Nothing matches"))
        lines.append("")
        footer = "\u2191\u2193 move \u00b7 enter select \u00b7 esc cancel" if width >= 52 \
            else "\u2191\u2193 \u00b7 enter \u00b7 esc" if width >= 30 else "esc"
        return box(lines, width, p, ACCENT, title=p.bold(fit("Command palette", width - 8)),
                   footer=p.faint(fit(footer, width - 10)))


def pick_verb(rng: random.Random | None = None) -> str:
    return (rng or random).choice(VERBS)


def pick_placeholder(rng: random.Random | None = None) -> str:
    return (rng or random).choice(PLACEHOLDERS)


__all__ = ['ACCENT', 'BASH', 'GLYPHS', 'LOGO', 'MEMORY', 'MODE_CHIPS', 'MODE_COLOURS', 'MUTED',
           'PLACEHOLDERS', 'SELECT', 'SHORTCUTS', 'SPINNER', 'USER', 'VERBS', 'Choice',
           'CommandPalette', 'Picker',
           'box', 'fit', 'fit_path', 'glyph', 'hint_line', 'input_box', 'logo', 'menu', 'pad',
           'pick_placeholder', 'pick_verb', 'pulse', 'reduced_motion', 'shimmer', 'shortcuts',
           'spinner_glyph', 'spinner_line', 'status_bar', 'visible', 'welcome', 'wrap']
