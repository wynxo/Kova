"""The pieces of Kova's live region, each rendered to styled lines.

Everything here is pure: given state and a width it returns lines (and, for
the input box, where the caret goes). The TUI loop decides which widgets are
on screen and the ``Screen`` draws them.

The look (docs/TERMINAL-DESIGN.md): surfaces you type into sit between two
rules, the keys that work right now in the lower one; something waiting on
you (an approval, a question) has a coloured bar down its left edge; nothing
is boxed in. Words are lower case and separated by space, not by dots.
"""
from __future__ import annotations

import os
import random

from kova import theme

from .glyphs import glyphs
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
from .style import ACCENT_SOFT, BORDER, FAINT, downgrade, fg, rgb

PLACEHOLDERS = ('Try "what can you do?"',
                'Try "remind me to stretch in an hour"', 'Try "summarize @README.md"',
                'Try "what do you remember about me?"')
# (keys, what they do), in the order the ? panel lists them.
SHORTCUTS = (("ctrl+p", "command palette"), ("esc", "stop Kova"),
             ("ctrl+o", "open every step's details"), ("ctrl+t", "show the full reasoning"),
             ("pgup/pgdn", "scroll those details"), ("shift+tab", "change when Kova asks"),
             ("/", "commands"), ("@", "attach a file"), ("!", "run a shell command"),
             ("#", "remember a note"), ("esc esc", "clear the input"),
             ("shift+⏎", "new line (or \\⏎)"), ("tab", "complete"),
             ("↑ ↓", "earlier messages"), ("ctrl+l", "clear the screen"),
             ("ctrl+d", "exit (or ctrl+c twice)"))
# The half-block wordmark; the CLI's --help and setup banner still draw it.
LOGO = ("█▄▀ █▀█ █ █ ▄▀█",
        "█ █ █▄█ ▀▄▀ █▀█")

MODE_CHIPS = {"work": theme.ACCENT, "chat": theme.USER}
# When Kova asks before acting, in words.
PERMISSION_WORDS = {"manual": "asks before acting", "auto": "asks before risky steps",
                    "full": "never asks"}
MODE_HINTS = {"work": "files, commands, web, memory", "chat": "conversation, web at most"}
_REDUCED = bool(os.environ.get("KOVA_REDUCED_MOTION"))


def reduced_motion() -> bool:
    return _REDUCED


def pad(text: str, width: int) -> str:
    """Pad a styled string with spaces to ``width`` visible columns, cutting
    it (escapes kept) if it is wider."""
    size = visible(text)
    if size > width:
        return clip(text, width)
    return text + " " * (width - size)


def clip(text: str, width: int) -> str:
    """Cut a styled string to ``width`` visible columns, keeping its escapes."""
    if visible(text) <= width:
        return text
    from .terminal import _clip
    return _clip(text, width) + ("\x1b[0m" if "\x1b[" in text else "")


def visible(text: str) -> int:
    """Columns a styled string takes on screen."""
    return display_width(strip_ansi(text))


def fit(text: str, width: int) -> str:
    """Shorten plain text to ``width`` columns with an ellipsis."""
    text = " ".join(str(text).split()) if "\n" not in str(text) else str(text)
    if display_width(text) <= width:
        return text
    tail = glyphs().ellipsis
    out, used = "", 0
    limit = max(0, width - display_width(tail))
    for char in text:
        step = display_width(char)
        if used + step > limit:
            break
        out, used = out + char, used + step
    return out + tail


def fit_path(path: str, width: int) -> str:
    """Shorten a path from the middle, keeping its start and the last folder."""
    if display_width(path) <= width or width < 8:
        return fit(path, width)
    head, _, tail = path.rpartition("/")
    tail = "/" + tail
    keep = width - display_width(tail) - 1
    if keep < 4:
        return fit(path, width)
    head = fit(head, keep + display_width(glyphs().ellipsis))
    return head[:-len(glyphs().ellipsis)] + glyphs().ellipsis + tail


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


def keys(p: Palette, pairs, gap: str = "   ", room: int | None = None) -> str:
    """Key hints: each key in bold, what it does in muted text, spaced apart.
    With ``room``, hints drop from the end until the rest fits."""
    pairs = list(pairs)
    while True:
        text = gap.join(p.paint("1;" + ACCENT_SOFT, key) + " " + p.faint(what) for key, what in pairs)
        if room is None or visible(text) <= room or len(pairs) <= 1:
            return text if room is None or visible(text) <= room else ""
        pairs.pop()


# ------------------------------------------------------------------- effects
def shimmer(text: str, phase: float, p: Palette, base=None, light=None) -> str:
    """Text with a soft highlight sweeping across it; ``phase`` is the highlight's
    position in characters (it may run past either end)."""
    if not p.color:
        return text
    if _REDUCED or p.level == 1:
        return p.paint(ACCENT_SOFT, text)
    base = base or rgb(p.terminal.roles["ACCENT_SOFT"])
    light = light or rgb(p.terminal.roles["ACCENT"])
    out = []
    for index, char in enumerate(text):
        strength = max(0.0, 1.0 - abs(index - phase) / 2.5)
        colour = [round(b + (h - b) * strength) for b, h in zip(base, light, strict=False)]
        out.append(f"\x1b[{downgrade(f'38;2;{colour[0]};{colour[1]};{colour[2]}', p.level)}m{char}")
    return "".join(out) + "\x1b[0m"


def glyph(frame: int) -> str:
    """The live node at ``frame``; still when motion is reduced."""
    g = glyphs()
    return g.spin(0) if _REDUCED else g.spin(frame)


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


def rule(width: int, p: Palette, colour: str = FAINT, *, title: str = "", right: str = "") -> str:
    """A horizontal rule ``width`` columns wide, with an optional title at the
    left and text (key hints) at the right. Hints give way first: the rule
    itself never wraps."""
    line = glyphs().rule
    if right:
        right = clip(right, max(0, width - 12))
    left = (line * 2 + " " + title + " ") if title else ""
    tail = (" " + right + " " + line) if right else ""
    fill = max(2, width - visible(left) - visible(tail))
    if title:
        left = p.paint(colour, line * 2 + " ") + title + " "
    tail_text = (" " + right + " " + p.paint(colour, line)) if right else ""
    return left + p.paint(colour, line * fill) + tail_text


def panel(lines: list[str], width: int, p: Palette, colour: str = FAINT, *, title: str = "",
          footer: str = "") -> list[str]:
    """Lines between two rules: the title in the upper one, keys in the lower."""
    return [rule(width, p, colour, title=title), *lines, rule(width, p, colour, right=footer)]


def gate(lines: list[str], width: int, p: Palette, colour: str = WARNING, *, title: str = "",
         footer: str = "") -> list[str]:
    """Something waiting on you: a coloured bar down the left, keys underneath."""
    g = glyphs()
    bar = p.paint("1;" + colour, g.gate)
    rows = [p.paint("1;" + colour, g.ask) + " " + p.bold(fit(strip_ansi(title), width - 3))] if title else []
    rows += [bar + (" " + pad(line, width - 2) if line else "") for line in lines]
    if footer:
        rows.append(rule(width, p, colour, right=footer))
    return [row.rstrip() for row in rows]


def input_box(text: str, cursor: int, width: int, p: Palette, *, placeholder: str = "",
              max_rows: int = 10, dim: bool = False, footer: str = "",
              busy: bool = False) -> tuple[list[str], tuple[int, int]]:
    """The prompt, between two rules. A leading ``!`` or ``#`` becomes the
    prompt mark and tints the rules (shell and memory modes); otherwise the
    mark is the user's own bar. Returns lines and the caret."""
    g = glyphs()
    mode = text[:1] if text[:1] in ("!", "#") else ""
    colour = {"!": BASH, "#": MEMORY}.get(mode) or FAINT
    shown, caret = (text[1:], max(0, cursor - 1)) if mode else (text, cursor)
    if mode and shown.startswith(" "):  # "# note" reads as "#" then the note
        shown, caret = shown[1:], max(0, caret - 1)
    prompt = mode or g.user
    inner = max(8, width - 3)
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
    mark_style = {"!": "1;" + BASH, "#": "1;" + MEMORY}.get(mode) or \
        ("1;" + (FAINT if busy or not text else USER))
    body = []
    for index, row in enumerate(visible_rows):
        lead = p.paint(mark_style, prompt) + " " if index + first == 0 else "  "
        content = p.dim(row) if dim else row
        if not text and index == 0 and placeholder:
            content = p.paint(f"3;{FAINT}" if p.level >= 2 else "2;3", fit(placeholder, inner))
        body.append(lead + content)
    lines = panel(body, width, p, colour, footer=footer if footer and visible(footer) < width - 8 else "")
    return lines, (1 + caret_row - first, 2 + caret_col)


def menu(items, selected: int, width: int, p: Palette, rows: int = 8) -> list[str]:
    """Suggestions under the input box, the selected one marked and lit."""
    if not items:
        return []
    g = glyphs()
    start = max(0, min(selected - rows // 2, len(items) - rows))
    shown = items[start:start + rows]
    label_width = min(32, max(display_width(item.label) for item in shown) + 2)
    lines = []
    for index, item in enumerate(shown, start):
        label = fit(item.label, label_width - 1).ljust(label_width)
        detail = fit(item.detail, max(0, width - label_width - 6))
        if index == selected:
            lines.append("  " + p.paint("1;" + ACCENT, g.pointer + " ") + p.paint("1;" + SELECT, label)
                         + p.paint(SELECT, detail))
        else:
            lines.append("    " + label + p.dim(detail))
    if len(items) > rows:
        lines.append(p.faint(f"    {selected + 1} of {len(items)}"))
    return [line.rstrip() for line in lines]


def hint_line(left: str, right: str, width: int) -> str:
    gap = width - 2 - visible(left) - visible(right)
    if gap < 2:
        return "  " + left
    return "  " + left + " " * gap + right


def status_bar(p: Palette, width: int, *, mode: str, model: str, title: str = "",
               permission: str | None = None, web: bool = True, plan: str = "",
               stats: str = "", offline: bool = False, cwd: str = "", state: str = "") -> str:
    """The bottom line, in plain words: mode, model, when Kova asks, the chat;
    the numbers and the folder on the right. Parts drop off from the least
    useful end as the terminal narrows, so it never wraps."""

    def model_part(room: int) -> str:
        name = p.bold(fit(model or "no model", max(1, room)))
        return name + (" " + p.yellow("offline") if offline else "")
    mode_part = p.paint(MUTED, mode) if mode else ""
    parts: list[tuple[int, str]] = [(0, model_part(60))]
    if state == "blocked-on-approval":
        parts.append((0, p.yellow("waiting for you")))
    elif state and state not in ("done", "working"):
        parts.append((1, p.dim(state.replace("-", " "))))
    if permission:
        words = PERMISSION_WORDS.get(permission, permission)
        paint = {"auto": p.yellow, "full": p.red}.get(permission, p.dim)
        parts.append((3, paint(words)))
    if not web:
        parts.append((4, p.dim("web off")))
    if plan:
        parts.append((1, p.dim("plan " + plan)))
    if title:
        parts.append((5, p.faint(fit(title, 28))))
    right_parts = [part for part in (stats, cwd) if part]
    separator = "   "

    def render(keep: list[tuple[int, str]], right: list[str]) -> str:
        left = " " + (mode_part + separator if mode_part else "") \
            + separator.join(text for _rank, text in keep)
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


def shortcuts(width: int, p: Palette, keys_map=None) -> list[str]:
    bindings = [(keys_map.format_help(key) if keys_map else key, text) for key, text in SHORTCUTS]
    columns = 3 if width >= 110 else 2 if width >= 64 else 1
    per = -(-len(bindings) // columns)
    groups = [bindings[i * per:(i + 1) * per] for i in range(columns)]
    column_width = (width - 2) // columns
    key_width = max(display_width(key) for key, _ in bindings) + 2
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


def tile(p: Palette, name: str, colour: str = theme.ACCENT) -> str:
    """A speaker's tile: their initial on their colour (the app mark, in a terminal)."""
    initial = (str(name).strip()[:1] or "?").upper()
    return p.pill(initial, colour, solid=True) if p.color else initial


def welcome(p: Palette, width: int, *, version: str, model: str, mode: str, cwd: str,
            memory: str = "", notes: int | None = None, web: bool = True, offline: bool = False,
            phase: float | None = None, title: str = "",
            permission: str | None = None, assistant_name: str = "Kova") -> list[str]:
    """The start: who you are talking to, what it can reach, and how to steer
    it. Left-aligned, no frame; ``phase`` is accepted for compatibility and
    ignored, because nothing animates on load."""
    size = max(30, min(width, 80))
    label_width = 9
    room = max(8, size - label_width - 6)
    name = p.bold(fit(assistant_name, max(4, size - 20)))
    version_text = p.faint(f"v{version}")
    head = tile(p, assistant_name) + "  " + name
    gap = size - visible(head) - visible(version_text)
    lines = [head + " " * max(2, gap) + version_text]
    intro = "Every step shows up here as it happens. Esc stops, ctrl+o opens details."
    from .render import wrap_words
    lines += ["    " + p.dim(row) for row in wrap_words(intro, size - 4)]
    lines.append("")

    def row(label: str, value: str) -> str:
        return "    " + p.faint(label.ljust(label_width)) + value

    state = p.yellow("offline") if offline else p.green("ready")
    lines.append(row("model", p.bold(fit(model, max(6, room - 8))) + "  " + state))
    hint = MODE_HINTS.get(mode, "")
    if not web and hint:
        hint = hint.replace("web, ", "").replace(", web at most", "") + ", web off"
    lines.append(row("mode", p.paint("1;" + fg(MODE_CHIPS.get(mode, theme.ACCENT)), mode)
                     + (p.dim("  " + fit(hint, room - len(mode) - 2)) if hint else "")))
    lines.append(row("folder", fit_path(cwd, room)))
    if permission:
        when = {"manual": "before acting", "auto": "before risky steps", "full": "never"}
        lines.append(row("asks", p.dim(when.get(permission, permission))
                         + p.faint("   shift+tab changes it")))
    if memory or notes is not None:
        count = "" if notes is None else f"{notes} {'note' if notes == 1 else 'notes'}"
        where = fit_path(memory, max(4, room - len(count) - 5)) if memory else ""
        lines.append(row("memory", count + (p.faint(("  in " if count else "") + where) if where else "")))
    if offline:
        lines += ["", "    " + p.yellow(fit("The model is not answering. Start it, or pick another "
                                              "with /model.", size - 4))]
    return lines


# ------------------------------------------------------------------- dialogs
class Choice:
    """A question answered with one key, with ↑↓ and Enter, or a number.

    Drawn as a gate: a bar down the left in ``colour``, the body, then one row
    per answer with its key in front of it.
    """

    def __init__(self, title: str, body: list[str], options: list[tuple[str, object, str]], *,
                 colour: str = ACCENT, footer: str = ""):
        self.title, self.body, self.options = title, body, options  # (label, value, hotkey)
        self.colour, self.footer = colour, footer
        self.selected = 0

    def move(self, step: int) -> None:
        self.selected = (self.selected + step) % len(self.options)

    def pick(self, key: str):
        """The value for a pressed key, or None when it answers nothing."""
        if key.isdecimal() and 1 <= int(key) <= len(self.options):
            return self.options[int(key) - 1][1]
        for _label, value, hotkey in self.options:
            if hotkey and key.lower() == hotkey:
                return value
        return None

    @property
    def value(self):
        return self.options[self.selected][1]

    def render(self, width: int, p: Palette) -> list[str]:
        g = glyphs()
        inner = width - 4
        colour = WARNING if self.colour == ACCENT else self.colour
        lines = [*self.body]
        if lines:
            lines.append("")
        for index, (label, _value, hotkey) in enumerate(self.options):
            key = hotkey or str(index + 1)
            text = fit(label, inner - 6)
            if index == self.selected:
                lines.append(p.paint("1;" + colour, g.pointer) + " " + p.paint("1;" + colour, key)
                             + "  " + p.bold(text))
            else:
                lines.append("  " + p.paint("1;" + ACCENT_SOFT, key) + "  " + p.dim(text))
        return gate(lines, width, p, colour, title=self.title, footer=self.footer)


class Picker:
    """Choose one of many (models, conversations, modes); typing filters."""

    ROWS = 10

    def __init__(self, title: str, items: list[tuple[str, str, object]], on_pick, *,
                 current=None, subtitle: str = "", groups: dict | None = None,
                 actions: dict | None = None):
        self.title, self.items, self.on_pick = title, items, on_pick  # (label, detail, value)
        self.groups = groups or {}
        # Extra actions on the highlighted row, as {key name: (label, callback)};
        # ``ctrl-s`` forks in the session picker. Typing still filters, so only
        # key names the editor never sees are offered.
        self.actions = actions or {}
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

    def _filter_line(self, p: Palette, inner: int, prompt: str = "filter  ") -> str:
        g = glyphs()
        if self.query:
            room = max(1, inner - display_width(prompt) - 2)
            typed = self.query if display_width(self.query) <= room else self.query[-room:]
            return p.faint(prompt) + typed + p.paint(ACCENT, g.caret)
        return p.faint(fit(prompt + "type to narrow the list", inner))

    def _row(self, p: Palette, index: int, label: str, detail: str, value, label_width: int,
             inner: int, *, numbered: bool) -> str:
        g = glyphs()
        current = value == self.current and self.current is not None
        tick = p.green("  current") if current else ""
        number = f"{index + 1}".rjust(2) + " " if numbered else ""
        title = fit(label, max(1, label_width - 1))
        if index == self.selected:
            row = p.paint("1;" + ACCENT, g.pointer + " ") + p.faint(number) + p.paint("1;" + SELECT, title)
        else:
            row = "  " + p.faint(number) + title
        room = inner - 2 - len(number) - label_width - visible(tick)
        if detail and room > 3:
            row += " " * max(2, label_width - display_width(title)) + p.dim(fit(detail, room))
        return row + tick

    def render(self, width: int, p: Palette) -> list[str]:
        inner = width - 2
        lines = []
        if self.subtitle:
            lines.append("  " + p.dim(fit(self.subtitle, inner - 2)))
        lines.append("  " + self._filter_line(p, inner - 2))
        lines.append("")
        shown = self.shown
        start = max(0, min(self.selected - self.ROWS // 2, len(shown) - self.ROWS))
        label_width = min(40, max([display_width(item[0]) for item in shown] + [8]) + 2)
        previous_group = None
        for index, (label, detail, value) in enumerate(shown[start:start + self.ROWS], start):
            group = self.groups.get(value)
            if group and group != previous_group:
                lines.append("  " + p.paint(MUTED, fit(group, inner - 2)))
                previous_group = group
            lines.append(self._row(p, index, label, detail, value, label_width, inner, numbered=True))
        if not shown:
            lines.append("  " + p.dim(f'Nothing matches "{fit(self.query, 30)}". '
                                      "Backspace widens the search."))
        g = glyphs()
        pairs = [(g.updown, "move"), ("enter", "choose")]
        pairs += [(hotkey.replace("ctrl-", "ctrl+"), label) for hotkey, (label, _cb) in self.actions.items()]
        pairs.append(("esc", "cancel"))
        footer = keys(p, pairs)
        if visible(footer) > width - 8:
            footer = keys(p, pairs[:2] + pairs[-1:])
        if visible(footer) > width - 8:
            footer = ""
        return panel(lines, width, p, FAINT, title=p.bold(fit(self.title, width - 8)), footer=footer)


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
                 subtitle: str = "Commands, models and recent chats"):
        super().__init__("Command palette", items, on_pick, subtitle=subtitle, groups=groups)

    @property
    def shown(self) -> list:
        if not self.query:
            return self.items
        from .editor import rank
        return rank(self.items, self.query, key=lambda item: f"{item[0]} {item[1]}")

    def render(self, width: int, p: Palette) -> list[str]:
        inner = max(8, width - 2)
        compact = width < 44
        lines = []
        if self.subtitle and not compact:
            lines.append("  " + p.dim(fit(self.subtitle, inner - 2)))
        lines.append("  " + self._filter_line(p, inner - 2, "search  "))
        lines.append("")
        shown = self.shown
        rows = self.ROWS if width >= 40 else max(4, self.ROWS - 3)
        start = max(0, min(self.selected - rows // 2, len(shown) - rows))
        label_width = inner - 2 if compact else min(
            26, max([display_width(item[0]) for item in shown] + [8]) + 2)
        previous = None
        for index, (label, detail, value) in enumerate(shown[start:start + rows], start):
            group = self.groups.get(value)
            if group and group != previous and not compact and not self.query:
                lines.append("  " + p.paint(MUTED, fit(group, inner - 2)))
            previous = group
            lines.append(self._row(p, index, label, "" if compact else detail, value,
                                   label_width, inner, numbered=False))
        if not shown:
            lines.append("  " + p.dim(fit(f'Nothing matches "{self.query}". Backspace widens it.', inner - 2)))
        g = glyphs()
        footer = keys(p, [(g.updown, "move"), ("enter", "choose"), ("esc", "cancel")]) if width >= 52 \
            else keys(p, [("esc", "cancel")]) if width >= 30 else ""
        return panel(lines, width, p, FAINT, title=p.bold(fit("Command palette", width - 8)),
                     footer=footer)


def pick_placeholder(rng: random.Random | None = None) -> str:
    return (rng or random).choice(PLACEHOLDERS)


__all__ = ['ACCENT', 'BASH', 'LOGO', 'MEMORY', 'MODE_CHIPS', 'MODE_HINTS',
           'MUTED', 'PERMISSION_WORDS', 'PLACEHOLDERS', 'SELECT', 'SHORTCUTS', 'USER', 'Choice',
           'CommandPalette', 'Picker', 'box', 'clip', 'fit', 'fit_path', 'gate', 'glyph', 'hint_line',
           'input_box', 'keys', 'logo', 'menu', 'pad', 'panel', 'pick_placeholder',
           'reduced_motion', 'rule', 'shimmer', 'shortcuts', 'status_bar', 'tile', 'visible',
           'welcome', 'wrap']
