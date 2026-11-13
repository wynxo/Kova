"""Raw terminal I/O for Kova's full-screen chat.

``KeyReader`` turns the bytes a terminal sends into keys: text, editing and
arrow keys, bracketed pastes, and a lone Esc told apart from an escape
sequence. ``Screen`` keeps a *live region* — spinner, input box, menus,
dialogs — redrawn under the scrolling transcript. Transcript text is written
above it and scrolls into the terminal's scrollback like any other output;
the live region is erased and redrawn around each write, one synchronized
frame at a time, so nothing flickers or leaves remnants.

Ported and adapted from Kaine's ``kaine/cli_terminal.py``.
"""
from __future__ import annotations

import codecs
import os
import re
import select
import shutil
import sys
from dataclasses import dataclass

from .markdown import display_width, strip_ansi

PASTE_START, PASTE_END = "\x1b[200~", "\x1b[201~"
SYNC_ON, SYNC_OFF = "\x1b[?2026h", "\x1b[?2026l"   # ignored by terminals without it
HIDE_CURSOR, SHOW_CURSOR = "\x1b[?25l", "\x1b[?25h"

SEQUENCES = {
    "\x1b[A": "up", "\x1b[B": "down", "\x1b[C": "right", "\x1b[D": "left",
    "\x1bOA": "up", "\x1bOB": "down", "\x1bOC": "right", "\x1bOD": "left",
    "\x1b[H": "home", "\x1b[F": "end", "\x1bOH": "home", "\x1bOF": "end",
    "\x1b[1~": "home", "\x1b[4~": "end", "\x1b[7~": "home", "\x1b[8~": "end",
    "\x1b[3~": "delete", "\x1b[5~": "pageup", "\x1b[6~": "pagedown", "\x1b[Z": "shift-tab",
    "\x1b[1;5C": "word-right", "\x1b[1;5D": "word-left", "\x1b[1;3C": "word-right",
    "\x1b[1;3D": "word-left", "\x1bf": "word-right", "\x1bb": "word-left",
    "\x1b\x7f": "kill-word", "\x1b\x08": "kill-word", "\x1bd": "kill-word-right",
    # Shift+Enter or Alt+Enter where the terminal reports them apart from Enter.
    "\x1b[13;2u": "newline", "\x1b[27;2;13~": "newline", "\x1b\r": "newline", "\x1b\n": "newline",
}
CONTROL = {
    "\r": "enter", "\n": "newline", "\t": "tab", "\x7f": "backspace", "\x08": "backspace",
    "\x01": "home", "\x05": "end", "\x02": "left", "\x06": "right", "\x0b": "kill-end",
    "\x15": "kill-start", "\x17": "kill-word", "\x19": "yank", "\x0c": "clear-screen",
    "\x14": "ctrl-t", "\x0f": "ctrl-o", "\x04": "ctrl-d", "\x03": "ctrl-c", "\x10": "ctrl-p",
    "\x0e": "ctrl-n", "\x1a": "suspend",
}
_SGR = re.compile(r"\x1b\[([0-9;]*)m")
_CSI = re.compile(r"\x1b\[[0-9;?<>=]*[ -/]*[@-~]")
_SS3 = re.compile(r"\x1bO.")
_LONGEST = max(len(key) for key in SEQUENCES)


@dataclass(frozen=True)
class Key:
    name: str       # "text", "paste", "enter", "esc", "up", "ctrl-c", ...
    text: str = ""  # the characters, for "text" and "paste"


class KeyReader:
    """Keys from a file descriptor, parsed incrementally across reads."""

    ESC_WAIT = 0.03  # how long a lone Esc waits to become a sequence

    def __init__(self, fd: int):
        self.fd = fd
        self.pending = ""
        self.decoder = codecs.getincrementaldecoder("utf-8")("replace")

    def read(self, timeout: float) -> list[Key]:
        self._fill(timeout)
        keys = self.parse(final=False)
        if self.pending and not self.pending.startswith(PASTE_START):
            # An Esc, or a sequence cut between reads: give it a moment to
            # complete, then take what is there at face value.
            if self._fill(self.ESC_WAIT):
                keys += self.parse(final=False)
            if self.pending and not self.pending.startswith(PASTE_START):
                keys += self.parse(final=True)
        return keys

    def _fill(self, timeout: float) -> bool:
        ready, _, _ = select.select([self.fd], [], [], max(0.0, timeout))
        if not ready:
            return False
        data = os.read(self.fd, 65536)
        if not data:
            return False
        self.pending += self.decoder.decode(data)
        return True

    def parse(self, final: bool) -> list[Key]:
        text, keys, index = self.pending, [], 0
        while index < len(text):
            char = text[index]
            if text.startswith(PASTE_START, index):
                end = text.find(PASTE_END, index + len(PASTE_START))
                if end == -1:
                    break  # the rest of the paste is still arriving
                body = text[index + len(PASTE_START):end]
                keys.append(Key("paste", body.replace("\r\n", "\n").replace("\r", "\n")))
                index = end + len(PASTE_END)
            elif char == "\x1b":
                step, key = self._escape(text, index, final)
                if step == 0:
                    break
                if key is not None:
                    keys.append(key)
                index += step
            elif char in CONTROL:
                keys.append(Key(CONTROL[char]))
                index += 1
            elif ord(char) < 32 or 0x7f <= ord(char) < 0xa0:
                index += 1
            else:
                start = index
                while index < len(text) and ord(text[index]) >= 32 and text[index] != "\x7f" \
                        and not 0x80 <= ord(text[index]) < 0xa0:
                    index += 1
                keys.append(Key("text", text[start:index]))
        self.pending = text[index:]
        return keys

    @staticmethod
    def _escape(text: str, index: int, final: bool) -> tuple[int, Key | None]:
        """(characters consumed, key); (0, None) to wait for more input."""
        for size in range(min(_LONGEST, len(text) - index), 1, -1):
            name = SEQUENCES.get(text[index:index + size])
            if name:
                return size, Key(name)
        for pattern in (_CSI, _SS3):
            match = pattern.match(text, index)
            if match:
                return match.end() - index, None  # a key we have no use for
        rest = text[index + 1:]
        if not rest:
            return (1, Key("esc")) if final else (0, None)
        if rest[0] == "\x1b":
            return 1, Key("esc")  # Esc pressed twice in a row
        if rest[0] in "[O":
            return (1, Key("esc")) if final else (0, None)  # a sequence cut in half
        return 2, None  # an Alt+key combination we have no use for


class RawMode:
    """Keys arrive one at a time, unechoed, with Ctrl+C and Ctrl+Z as keys.

    Output processing stays on, so "\\n" still starts a new line.
    """

    def __init__(self, fd: int, out):
        self.fd, self.out, self.saved = fd, out, None

    def __enter__(self):
        import termios
        self.saved = termios.tcgetattr(self.fd)
        mode = termios.tcgetattr(self.fd)
        mode[0] &= ~(termios.BRKINT | termios.ICRNL | termios.INPCK | termios.ISTRIP | termios.IXON)
        mode[3] &= ~(termios.ECHO | termios.ICANON | termios.IEXTEN | termios.ISIG)
        mode[6][termios.VMIN], mode[6][termios.VTIME] = 1, 0
        termios.tcsetattr(self.fd, termios.TCSANOW, mode)
        self.out.write("\x1b[?2004h")  # bracketed paste
        self.out.flush()
        return self

    def __exit__(self, *_exc):
        import termios
        self.out.write("\x1b[?2004l" + SHOW_CURSOR)
        self.out.flush()
        if self.saved is not None:
            termios.tcsetattr(self.fd, termios.TCSANOW, self.saved)


def size() -> tuple[int, int]:
    columns, rows = shutil.get_terminal_size((100, 30))
    return max(20, columns), max(8, rows)


class Screen:
    """A scrolling transcript with a live region redrawn below it.

    The transcript's insertion point is (row R, column C); the live region
    starts on row R when C is 0, otherwise on the row after. After drawing,
    the terminal cursor rests in the live region (at the input caret) or
    back at the insertion point, and ``rest`` records how to return.
    """

    def __init__(self, out=None, *, size_fn=size):
        self.out = out or sys.stdout
        self.size = size_fn
        self.column = 0
        self.live: list[str] = []
        self.caret: tuple[int, int] | None = None
        self.drawn = False
        self.rest = 0            # rows between the insertion row and the resting cursor
        self.frame: list[str] = []
        self.dirty = False
        self.style = ""            # the transcript's text style at the insertion point
        self.written = False       # anything in the transcript since the last clear
        self.last_empty = False    # the last finished transcript line was blank

    # ------------------------------------------------------------ transcript
    def write(self, text: str) -> None:
        if not text:
            return
        self._erase()
        columns = self.size()[0]
        parts = str(text).replace("\r", "").split("\n")
        for index, part in enumerate(parts):
            if index:
                self.frame.append("\n")
                self.last_empty = self.column == 0
                self.column = 0
            if not part:
                continue
            self._track_style(part)
            self.frame.append(part)
            self.column += display_width(strip_ansi(part))
            if self.column >= columns:
                # The terminal wrapped (or is about to): make it explicit so the
                # insertion point is always a real, reachable cell.
                self.column %= columns
                if self.column == 0:
                    self.frame.append("\r\n")
        self.written = True
        self.dirty = True

    def _track_style(self, text: str) -> None:
        """Follow SGR codes so a redraw of the live region (which resets the
        style) can hand the transcript back its bold or colour mid-word."""
        for match in _SGR.finditer(text):
            params = match.group(1)
            if params in ("", "0") or params.startswith("0;"):
                self.style = "" if params in ("", "0") else match.group(0)
            else:
                self.style += match.group(0)

    @property
    def ends_blank(self) -> bool:
        """The transcript ends in an empty line (or there is none yet)."""
        return not self.written or (self.column == 0 and self.last_empty)

    def isatty(self) -> bool:
        return True

    def flush(self) -> None:
        """Writes are batched into the next ``render``, one frame per loop pass."""

    # ------------------------------------------------------------ live region
    def set_live(self, lines: list[str], caret: tuple[int, int] | None = None) -> None:
        rows = self.size()[1]
        limit = max(1, rows - 1)
        if len(lines) > limit:
            # Keep the bottom (the input box) and drop the top of the region.
            drop = len(lines) - limit
            lines = lines[drop:]
            caret = (caret[0] - drop, caret[1]) if caret and caret[0] >= drop else None
        if lines != self.live or caret != self.caret:
            self.live, self.caret = list(lines), caret
            self.dirty = True

    def clear(self) -> None:
        """Clear the visible screen (the transcript stays in scrollback)."""
        rows = self.size()[1]
        self._erase()
        self.frame.append("\n" * rows + "\x1b[H\x1b[2J")
        self.column = 0
        self.written = False
        self.dirty = True

    def render(self) -> None:
        if not self.dirty and not self.frame:
            return
        self._erase()
        chunks = [SYNC_ON, HIDE_CURSOR, *self.frame, self._draw(), SYNC_OFF]
        if self.caret is not None:
            chunks.append(SHOW_CURSOR)
        self.out.write("".join(chunks))
        self.out.flush()
        self.frame = []
        self.dirty = False

    def close(self) -> None:
        """Remove the live region and leave the cursor on a fresh line."""
        self.live, self.caret = [], None
        self._erase()
        if self.column:
            self.frame.append("\n")
            self.column = 0
        self.out.write("".join([*self.frame, SHOW_CURSOR]))
        self.out.flush()
        self.frame = []

    def _erase(self) -> None:
        if not self.drawn:
            return
        move = f"\x1b[{self.rest}A" if self.rest else ""
        move += "\r" + (f"\x1b[{self.column}C" if self.column else "")
        self.frame.append(move + "\x1b[J" + self.style)
        self.drawn = False
        self.dirty = True

    def _draw(self) -> str:
        if not self.live:
            self.rest = 0
            self.drawn = False
            return ""
        columns = self.size()[0]
        out = ["\r\n" if self.column else ""]
        top = 1 if self.column else 0
        for index, line in enumerate(self.live):
            if index:
                out.append("\r\n")
            out.append(_clip(line, columns - 1) + "\x1b[0m")
        last = len(self.live) - 1
        if self.caret is not None:
            row, col = self.caret
            row = max(0, min(row, last))
            up = last - row
            out.append((f"\x1b[{up}A" if up else "") + "\r" + (f"\x1b[{col}C" if col else ""))
            self.rest = top + row
        else:
            up = top + last
            out.append((f"\x1b[{up}A" if up else "") + "\r"
                       + (f"\x1b[{self.column}C" if self.column else ""))
            self.rest = 0
        self.drawn = True
        return "".join(out)


def _clip(line: str, width: int) -> str:
    """Cut a styled line to ``width`` visible columns, keeping its escapes."""
    if display_width(strip_ansi(line)) <= width:
        return line
    out, used = [], 0
    for token in re.split(r"(\x1b\[[0-9;?]*[A-Za-z])", line):
        if token.startswith("\x1b["):
            out.append(token)
            continue
        for char in token:
            step = display_width(char)
            if used + step > width:
                return "".join(out)
            out.append(char)
            used += step
    return "".join(out)


__all__ = ['CONTROL', 'SEQUENCES', 'Key', 'KeyReader', 'RawMode', 'Screen', 'size']
