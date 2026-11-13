"""Streaming Markdown for Kova's terminal chat.

Model output arrives a few characters at a time. ``MarkdownStream`` prints it
as soon as whole words are known: it decides each line's block type (heading,
list item, quote, code fence, table, rule) from the line's first characters,
styles ``**bold**``, ``*italic*``, ``~~strike~~``, `` `code` `` and links
inline, and word-wraps to the terminal with hanging indents. Tables and fenced
code are held until they are complete enough to align. Nothing here touches the
terminal directly; output goes through a ``write`` callable.

Ported and adapted from Kaine's ``kaine/cli_markdown.py``; colours come from
``style`` (Kova's ``theme`` at the terminal's colour depth).
"""
from __future__ import annotations

import re
import unicodedata

from kova import theme

from .style import (
    ACCENT,
    ACCENT_SOFT,
    BASH,
    BORDER,
    CODE,
    ERROR,
    FAINT,
    HEADING,
    MEMORY,
    MUTED,
    SELECT,
    SUCCESS,
    USER,
    WARNING,
    Palette,
)

_HEADING = re.compile(r"(#{1,6})\s+")
_BULLET = re.compile(r"([-*+])\s+")
_ORDERED = re.compile(r"(\d{1,3})([.)])\s+")
_TASK = re.compile(r"\[([ xX])\]\s+")
_RULE = re.compile(r"([-*_])(\s*\1){2,}\s*$")
_TABLE_SEPARATOR = re.compile(r"^\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$")
_LINK = re.compile(r"\[([^\]\n]+)\]\(([^)\s]+)\)")
_FENCE = re.compile(r"(`{3,}|~{3,})\s*([\w+#.-]*)")


# Zero-width markers the inline scanner turns into styles.
_UNDERLINE_ON, _UNDERLINE_OFF, _DIM_ON, _DIM_OFF = "\ue000", "\ue001", "\ue002", "\ue003"
_MARKERS = {_UNDERLINE_ON: ("underline", True), _UNDERLINE_OFF: ("underline", False),
            _DIM_ON: ("dim", True), _DIM_OFF: ("dim", False)}
_BULLETS = ("\u2022", "\u25e6", "\u25aa")

_PANEL_BG = "48;2;" + ";".join(str(c) for c in (30, 29, 40))   # the code panel's tint
_RESET = re.compile(r"\x1b\[(?:0|00|39;49;00|49)?m")

_CONTROLS = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")


def clean(text: str) -> str:
    """Drop control characters (escape sequences included) from untrusted text,
    so a model or a command cannot move the cursor, retitle or reprogram the
    terminal. Newlines and tabs stay."""
    return _CONTROLS.sub("", str(text))


def display_width(text: str) -> int:
    """Terminal columns taken by plain text (wide East Asian glyphs count twice)."""
    width = 0
    for char in text:
        if unicodedata.combining(char) or char in "\u200b\u200d\ufe0f":
            continue
        width += 2 if unicodedata.east_asian_width(char) in ("W", "F") else 1
    return width


def strip_ansi(text: str) -> str:
    return re.sub(r"\x1b\[[0-9;]*m", "", text)


_STYLE_CODES = {"bold": "1", "dim": "2", "italic": "3", "underline": "4", "strike": "9",
                "code": CODE}


class Inline:
    """Inline emphasis state, carried across words (and across stream chunks)."""

    def __init__(self, palette: Palette, base: list[str] | None = None):
        self.palette = palette
        self.base = list(base or [])
        self.active: dict[str, bool] = {}

    def codes(self) -> list[str]:
        codes = list(self.base)
        for name in ("bold", "dim", "italic", "underline", "strike", "code"):
            if self.active.get(name):
                codes.append(_STYLE_CODES[name])
        return codes

    def current(self) -> str:
        return self.palette.sgr(self.codes()) if self.codes() else ""

    def reset(self) -> None:
        self.active.clear()

    def _toggle(self, name: str, value: bool | None = None) -> str:
        self.active[name] = (not self.active.get(name)) if value is None else value
        return self.palette.sgr(self.codes())

    def word(self, word: str) -> tuple[str, int]:
        """Render one whitespace-free word: (ANSI text, visible width)."""
        out, width, index = [], 0, 0
        while index < len(word):
            char = word[index]
            following = word[index + 1:index + 2]
            if char in _MARKERS:
                name, value = _MARKERS[char]
                out.append(self._toggle(name, value))
            elif char == "`":
                out.append(self._toggle("code"))
            elif self.active.get("code"):
                out.append(char)
                width += display_width(char)
            elif char == "*" and following == "*":
                out.append(self._toggle("bold"))
                index += 1
            elif char == "~" and following == "~":
                out.append(self._toggle("strike"))
                index += 1
            elif char == "*" and (self.active.get("italic") or (index == 0 and following)):
                out.append(self._toggle("italic"))
            elif char == "\\" and following and following in "*`_~[]#|\\":
                out.append(following)
                width += 1
                index += 1
            else:
                out.append(char)
                width += display_width(char)
            index += 1
        return "".join(out), width

    def static(self, text: str) -> tuple[str, int]:
        """Render a whole short text (a table cell) with its own fresh state."""
        text = _links(text)
        parts, width = [], 0
        for token in re.split(r"(\s+)", text):
            if not token:
                continue
            if token.isspace():
                parts.append(" ")
                width += 1
            else:
                rendered, size = self.word(token)
                parts.append(rendered)
                width += size
        parts.append(self.palette.sgr(self.base) if self.palette.color else "")
        self.reset()
        return "".join(parts), width


def _links(text: str) -> str:
    def replace(match):
        label, url = match.group(1), match.group(2)
        tail = "" if url == label or url.startswith("#") else f" {_DIM_ON}({url}){_DIM_OFF}"
        return f"{_UNDERLINE_ON}{label}{_UNDERLINE_OFF}{tail}"
    return _LINK.sub(replace, text)


def _highlighter(language: str, level: int = 2):
    """A Pygments line highlighter at the terminal's colour depth, or None
    when Pygments is missing or does not know the language."""
    try:
        from pygments import highlight
        from pygments.formatters import (
            Terminal256Formatter,
            TerminalFormatter,
            TerminalTrueColorFormatter,
        )
        from pygments.lexers import get_lexer_by_name
        from pygments.util import ClassNotFound
    except ImportError:
        return None
    try:
        lexer = get_lexer_by_name(language or "text", stripnl=False, ensurenl=False)
    except ClassNotFound:
        return None
    if level >= 3:
        formatter = TerminalTrueColorFormatter(style=theme.CODE_THEME)
    elif level == 2:
        formatter = Terminal256Formatter(style=theme.CODE_THEME)
    else:
        formatter = TerminalFormatter()
    return lambda line: highlight(line, lexer, formatter).rstrip("\n")


class MarkdownStream:
    """Feed model text in any chunk sizes; styled, wrapped lines come out."""

    def __init__(self, write, width: int = 100, palette: Palette | None = None, *,
                 margin: str = "  ", first: str = "", base: list[str] | None = None):
        self.write = write
        self.width = max(24, int(width))
        self.palette = palette or Palette(False)
        self.margin = margin
        self.first = first            # prefix of the very first line (e.g. a bullet)
        self.base = list(base or [])
        self.inline = Inline(self.palette, self.base)
        self.buffer = ""
        self.at_line_start = True
        self.column = 0
        self.indent = margin
        self.fence: tuple[str, object] | None = None
        self.table: list[str] = []
        self.blank_pending = False
        self.space_pending = False
        self.started = False

    # ------------------------------------------------------------ public
    def feed(self, text: str) -> None:
        self.buffer += clean(str(text or "").replace("\r\n", "\n").replace("\t", "    "))
        self._drain(final=False)

    def finish(self) -> None:
        self._drain(final=True)
        self._flush_table()
        if self.column:
            self._newline()
        if self.fence is not None:
            self._close_fence()
        self.inline.reset()

    # ------------------------------------------------------------ output
    def _emit(self, text: str) -> None:
        if text:
            self.write(text)

    def _start_line(self, prefix: str, indent: str) -> None:
        """Begin an output line: paragraph spacing, the prefix, the hanging indent."""
        if self.blank_pending and self.started:
            self._emit("\n")
        self.blank_pending = False
        if not self.started and self.first:
            prefix = self.first + prefix[display_width(self.first):]
        self.started = True
        self._emit(prefix + self.inline.current())
        self.column = display_width(strip_ansi(prefix))
        self.indent = indent

    def _newline(self) -> None:
        self._emit(self.palette.sgr([]) + "\n")
        self.column = 0

    # ------------------------------------------------------------- lines
    def _drain(self, final: bool) -> None:
        while self.buffer:
            if self.at_line_start:
                if not self._begin_line(final):
                    return
                continue
            newline = self.buffer.find("\n")
            if newline == -1:
                cut = _hold_point(self.buffer)
                if final:
                    cut = len(self.buffer)
                if cut <= 0:
                    return
                self._words(self.buffer[:cut])
                self.buffer = self.buffer[cut:]
                if not final:
                    return
            else:
                self._words(self.buffer[:newline])
                self.buffer = self.buffer[newline + 1:]
                self._end_line()

    def _end_line(self) -> None:
        if self.column:
            self._newline()
        self.inline.reset()
        self.inline.base = list(self.base)
        self.space_pending = False
        self.at_line_start = True

    def _take_line(self) -> str:
        newline = self.buffer.find("\n")
        line = self.buffer if newline == -1 else self.buffer[:newline]
        self.buffer = "" if newline == -1 else self.buffer[newline + 1:]
        return line

    def _begin_line(self, final: bool) -> bool:
        newline = self.buffer.find("\n")
        line = self.buffer if newline == -1 else self.buffer[:newline]
        complete = newline != -1 or final
        stripped = line.lstrip(" ")
        depth = (len(line) - len(stripped)) // 2

        if self.fence is not None:
            if not complete:
                return False
            self._code_line(self._take_line())
            return True
        if not complete and _undecided(stripped):
            return False
        if self.table and not stripped.startswith("|"):
            self._flush_table()
        fence = _FENCE.match(stripped)
        if fence:
            self._take_line()
            self._open_fence(fence.group(1), fence.group(2))
            return True
        if stripped.startswith("|"):
            self.table.append(self._take_line().strip())
            return True
        if not stripped.strip():
            if complete:
                self._take_line()
                self.blank_pending = True
                self.inline.reset()
                return True
            return False
        if complete and _RULE.match(stripped):
            self._take_line()
            self._start_line(self.margin, self.margin)
            self._emit(self.palette.faint("\u2500" * min(40, self.width - len(self.margin))))
            self._end_line()
            return True

        body_start = len(line) - len(stripped)
        heading = _HEADING.match(stripped)
        bullet = _BULLET.match(stripped)
        ordered = _ORDERED.match(stripped)
        if heading:
            level = len(heading.group(1))
            self.blank_pending = self.blank_pending or self.started
            self.inline.base = HEADING.split(";") if level <= 2 else ["1"]
            self._start_line(self.margin, self.margin)
            self.buffer = self.buffer[body_start + heading.end():]
        elif bullet:
            rest = stripped[bullet.end():]
            task = _TASK.match(rest)
            mark = ("\u2611" if task.group(1).strip() else "\u2610") if task else \
                _BULLETS[min(depth, 2)]
            prefix = self.margin + "  " * depth
            if task:
                marker = self.palette.green(mark) if task.group(1).strip() else self.palette.dim(mark)
            else:
                marker = self.palette.paint(ACCENT, mark)
            self._start_line(prefix + marker + " ", prefix + "  ")
            self.buffer = self.buffer[body_start + bullet.end() + (task.end() if task else 0):]
        elif ordered:
            prefix = self.margin + "  " * depth
            number = f"{ordered.group(1)}."
            self._start_line(prefix + self.palette.paint(ACCENT_SOFT, number) + " ",
                             prefix + " " * (len(number) + 1))
            self.buffer = self.buffer[body_start + ordered.end():]
        elif stripped.startswith(">"):
            bar = self.palette.paint(ACCENT_SOFT, "\u2502") + " "
            self.inline.base = [*self.base, "3"]
            self._start_line(self.margin + bar, self.margin + bar)
            self.buffer = self.buffer[body_start + 1:].lstrip(" ")
        else:
            self._start_line(self.margin, self.margin)
            self.buffer = self.buffer[body_start:]
        self.at_line_start = False
        return True

    def _words(self, text: str) -> None:
        indent_width = display_width(strip_ansi(self.indent))
        for token in re.split(r"(\s+)", _links(text)):
            if not token:
                continue
            if token.isspace():
                # Spaces are written only before a word on the same row, so a
                # row never ends in a space that would make the terminal wrap.
                self.space_pending = self.column > indent_width
                continue
            style = self.inline.current()  # the word's style before it toggles any
            rendered, size = self.inline.word(token)
            gap = 1 if self.space_pending else 0
            if self.column + gap + size > self.width and self.column > indent_width:
                self._emit(self.palette.sgr([]) + "\n" + self.indent + style)
                self.column, gap = indent_width, 0
            self._emit(" " * gap + rendered)
            self.column += gap + size
            self.space_pending = False

    # -------------------------------------------------------------- code
    def _open_fence(self, marker: str, language: str) -> None:
        if self.column:
            self._newline()
        self.blank_pending = self.blank_pending or self.started
        highlighter = _highlighter(language, self.palette.level) if self.palette.color else None
        self.fence = (marker, highlighter)
        if self.blank_pending and self.started:
            self._emit("\n")
        self.blank_pending = False
        self.started = True
        if self._panel:
            label = self.palette.paint(f"{MUTED};{_PANEL_BG}", f"  {language}" if language else "")
            self._emit(self.margin + self._panel_row(label, len(language) + 2 if language else 0))
        elif language:
            self._emit(self.margin + self.palette.dim(language) + "\n")

    @property
    def _panel(self) -> bool:
        """Code sits on a tinted panel when the terminal has colours to spare."""
        return self.palette.level >= 2

    def _panel_row(self, styled: str, size: int) -> str:
        """One row of the code panel: ``styled`` text padded across on the tint."""
        room = self.width - len(self.margin)
        fill = self.palette.paint(_PANEL_BG, " " * max(0, room - size))
        return styled + fill + self.palette.sgr([]) + "\n"

    def _close_fence(self) -> None:
        if self._panel:
            self._emit(self.margin + self._panel_row("", 0))
        self.fence = None

    def _code_line(self, line: str) -> None:
        marker, highlighter = self.fence
        if line.strip().startswith(marker[0] * 3) and not line.strip().strip(marker[0]):
            self._close_fence()
            self.blank_pending = True
            self.at_line_start = True
            return
        room = max(10, self.width - len(self.margin) - 4)
        tint = self.palette.sgr([_PANEL_BG])
        for piece in _chunks(line, room):
            text = highlighter(piece) if highlighter else self.palette.code(piece)
            if self._panel:
                text = _RESET.sub(lambda match: match.group(0) + tint, text)
                self._emit(self.margin + self._panel_row(tint + "  " + text, 2 + display_width(piece)))
            else:
                self._emit(self.margin + "  " + text + self.palette.sgr([]) + "\n")
        self.column = 0

    # ------------------------------------------------------------- table
    def _flush_table(self) -> None:
        rows = [row for row in self.table if not _TABLE_SEPARATOR.match(row)]
        self.table = []
        if not rows:
            return
        cells = [[cell.strip() for cell in row.strip().strip("|").split("|")] for row in rows]
        columns = max(len(row) for row in cells)
        cells = [row + [""] * (columns - len(row)) for row in cells]
        renderer = Inline(self.palette)
        rendered = [[renderer.static(cell) for cell in row] for row in cells]
        widths = [max(size for _, size in (row[i] for row in rendered)) for i in range(columns)]
        available = self.width - len(self.margin) - (3 * columns + 1)
        while sum(widths) > available and max(widths) > 6:
            widest = widths.index(max(widths))
            widths[widest] -= 1
        if self.blank_pending and self.started:
            self._emit("\n")
        self.blank_pending = True
        self.started = True
        def dim(text):
            return self.palette.faint(text)

        def border(left, middle, right):
            return self.margin + dim(left + middle.join("\u2500" * (w + 2) for w in widths) + right)

        self._emit(border("\u256d", "\u252c", "\u256e") + "\n")
        for index, row in enumerate(rendered):
            parts = []
            for (text, size), width, raw in zip(row, widths, cells[index], strict=False):
                if size > width:
                    text, size = renderer.static(_clip(raw, width))
                if index == 0:
                    text = self.palette.paint("1;" + ACCENT_SOFT, strip_ansi(text)) \
                        if self.palette.color else text
                parts.append(" " + text + " " * (width - size) + " ")
            self._emit(self.margin + dim("\u2502") + dim("\u2502").join(parts) + dim("\u2502") + "\n")
            if index == 0 and len(rendered) > 1:
                self._emit(border("\u251c", "\u253c", "\u2524") + "\n")
        self._emit(border("\u2570", "\u2534", "\u256f") + "\n")
        self.column = 0


def _chunks(line: str, width: int) -> list[str]:
    """A long code line cut into rows of ``width`` columns (never reflowed)."""
    rows, row, used = [], "", 0
    for char in line:
        step = display_width(char)
        if used + step > width:
            rows.append(row)
            row, used = "", 0
        row, used = row + char, used + step
    return [*rows, row]


def _clip(text: str, width: int) -> str:
    plain = re.sub(r"[*`~]", "", text)
    out, size = "", 0
    for char in plain:
        step = display_width(char)
        if size + step > width - 1:
            return out + "\u2026"
        out += char
        size += step
    return out


def _undecided(stripped: str) -> bool:
    """True while a line's first characters could still start a block construct."""
    if not stripped:
        return True
    if stripped.startswith("|"):
        return True
    if stripped[0] in "`~":
        return len(stripped) < 3 or stripped[:3] in ("```", "~~~")
    if set(stripped) <= set("-*_ ") and not _BULLET.match(stripped + "x"):
        return True
    if stripped[0] in "-*+" and len(stripped) < 3:
        return True
    bullet = _BULLET.match(stripped)
    if bullet:  # "- [" may still become a task-list checkbox
        rest = stripped[bullet.end():]
        return bool(rest) and any(box.startswith(rest) for box in ("[ ] ", "[x] ", "[X] "))
    if stripped.startswith("#") and not _HEADING.match(stripped) and set(stripped) <= {"#"}:
        return len(stripped) <= 6
    if stripped[0].isdigit() and not _ORDERED.match(stripped):
        return bool(re.fullmatch(r"\d{1,3}[.)]?", stripped))
    return False


def _hold_point(text: str) -> int:
    """How much of an unfinished line can be printed: whole words, whole links."""
    cut = max(text.rfind(" "), text.rfind("\t"))
    if cut == -1:
        return len(text) if display_width(text) > 48 else 0
    cut += 1
    opening = text.rfind("[", 0, cut)
    if opening != -1 and not _LINK.search(text, opening):
        closing = text.find("]", opening)
        # Hold back only what can still become a link: "[label" or "[label](url".
        if closing == -1 or text[closing + 1:closing + 2] in ("", "("):
            cut = opening
    return cut


__all__ = ['ACCENT', 'ACCENT_SOFT', 'BASH', 'BORDER', 'CODE', 'ERROR', 'FAINT', 'HEADING', 'MEMORY',
           'MUTED',
           'SELECT', 'SUCCESS', 'USER', 'WARNING', 'Inline', 'MarkdownStream', 'Palette',
           'clean', 'display_width', 'strip_ansi']
