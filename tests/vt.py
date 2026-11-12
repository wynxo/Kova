"""A small VT100 screen for tests: enough of a terminal to check what
Kova's terminal chat's output looks like once a real terminal has interpreted it.

Handles printable text (wide characters take two cells), CR, LF with
scrolling into a scrollback list, autowrap with the pending-wrap state,
cursor movement (CUU/CUD/CUF/CUB/CUP/CHA), erase in display and line, and
ignores SGR, private modes and OSC strings.
"""
from __future__ import annotations

import re

from kova.tui.markdown import display_width

_ESCAPE = re.compile(r"\x1b\[([0-9;?]*)([ -/]*)([@-~])|\x1b\][^\x07\x1b]*(\x07|\x1b\\)|\x1b[()][0-9A-Za-z]|\x1b[=>78]")


_INCOMPLETE = re.compile(r"\x1b(\[[0-9;?]*[ -/]*|\][^\x07\x1b]*|[()]?)?\Z")


class VT:
    def __init__(self, columns: int = 80, rows: int = 24):
        self.columns, self.rows = columns, rows
        self.grid = [[" "] * columns for _ in range(rows)]
        self.scrollback: list[str] = []
        self.x = self.y = 0
        self.pending_wrap = False
        self.saved = (0, 0)
        self.partial = ""  # an escape sequence cut off at the end of the last feed

    # ------------------------------------------------------------ reading
    def line(self, row: int) -> str:
        return "".join(cell for cell in self.grid[row] if cell is not None).rstrip()

    def lines(self) -> list[str]:
        return [self.line(row) for row in range(self.rows)]

    def text(self) -> str:
        return "\n".join(self.lines()).rstrip("\n")

    def history(self) -> list[str]:
        """Scrollback followed by the screen: everything ever shown, in order."""
        return [*self.scrollback, *self.lines()]

    # ------------------------------------------------------------ writing
    def feed(self, data: str) -> None:
        data, self.partial = self.partial + data, ""
        index = 0
        while index < len(data):
            match = _ESCAPE.match(data, index) if data[index] == "\x1b" else None
            if data[index] == "\x1b" and not match and _INCOMPLETE.match(data, index):
                self.partial = data[index:]  # wait for the rest of the sequence
                return
            if match:
                if match.group(3):
                    self._csi(match.group(1), match.group(3))
                elif match.group(0) == "\x1b7":
                    self.saved = (self.x, self.y)
                elif match.group(0) == "\x1b8":
                    self.x, self.y = self.saved
                index = match.end()
                continue
            char = data[index]
            index += 1
            if char == "\r":
                self.x, self.pending_wrap = 0, False
            elif char == "\n":
                self._linefeed()
                self.x, self.pending_wrap = 0, False  # the pty turns \n into \r\n
            elif char == "\b":
                self.x = max(0, self.x - 1)
            elif char == "\x1b" or ord(char) < 32:
                continue
            else:
                self._put(char)

    def _put(self, char: str) -> None:
        width = display_width(char)
        if width == 0:
            return
        if self.pending_wrap or self.x + width > self.columns:
            self.x, self.pending_wrap = 0, False
            self._linefeed()
        self.grid[self.y][self.x] = char
        if width == 2 and self.x + 1 < self.columns:
            self.grid[self.y][self.x + 1] = None
        self.x += width
        if self.x >= self.columns:
            self.x, self.pending_wrap = self.columns - 1, True

    def _linefeed(self) -> None:
        if self.y == self.rows - 1:
            self.scrollback.append(self.line(0))
            self.grid.pop(0)
            self.grid.append([" "] * self.columns)
        else:
            self.y += 1

    def _csi(self, params: str, final: str) -> None:
        if params.startswith("?"):
            return
        numbers = [int(part) if part else 0 for part in params.split(";")] if params else []
        count = max(1, numbers[0]) if numbers else 1
        self.pending_wrap = final in "m" and self.pending_wrap
        if final == "A":
            self.y = max(0, self.y - count)
        elif final == "B":
            self.y = min(self.rows - 1, self.y + count)
        elif final == "C":
            self.x = min(self.columns - 1, self.x + count)
        elif final == "D":
            self.x = max(0, self.x - count)
        elif final == "G":
            self.x = min(self.columns - 1, count - 1)
        elif final == "H":
            row = numbers[0] if numbers else 1
            col = numbers[1] if len(numbers) > 1 else 1
            self.y, self.x = min(self.rows, max(1, row)) - 1, min(self.columns, max(1, col)) - 1
        elif final == "J":
            mode = numbers[0] if numbers else 0
            if mode == 0:
                self.grid[self.y][self.x:] = [" "] * (self.columns - self.x)
                for row in range(self.y + 1, self.rows):
                    self.grid[row] = [" "] * self.columns
            elif mode in (2, 3):
                self.grid = [[" "] * self.columns for _ in range(self.rows)]
        elif final == "K":
            mode = numbers[0] if numbers else 0
            if mode == 0:
                self.grid[self.y][self.x:] = [" "] * (self.columns - self.x)
            elif mode == 2:
                self.grid[self.y] = [" "] * self.columns
