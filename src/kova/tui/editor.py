"""The input line of Kova's full-screen chat: editing, history, pastes and
suggestions.

``Editor`` is a small multi-line editor with the usual shell keys (word
moves, Ctrl+U/K/W/Y, history on Up/Down at the edges). Long pastes collapse
into a ``[Pasted text #1 +42 lines]`` token that expands again on submit.
``suggest`` offers slash commands, their arguments and ``@file`` mentions for
what is being typed; ``expand_mentions`` attaches the mentioned files.

Ported and adapted from Kaine's ``kaine/cli_editor.py``.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

PASTE_LINES, PASTE_CHARS = 8, 800
HISTORY_LIMIT = 1000
MENTION = re.compile(r"(?<!\S)@([^\s]+)")
MENTION_BYTES, MENTIONS_TOTAL = 64_000, 200_000


class Editor:
    def __init__(self, history: list[str] | None = None):
        self.text = ""
        self.cursor = 0
        self.history = list(history or [])
        self.browse: int | None = None   # index into history while browsing
        self.draft = ""
        self.pastes: dict[str, str] = {}
        self.killed = ""

    # ------------------------------------------------------------ state
    def set(self, text: str) -> None:
        self.text, self.cursor = text, len(text)

    def clear(self) -> None:
        self.set("")
        self.browse = None
        self.pastes.clear()

    @property
    def row_col(self) -> tuple[int, int]:
        before = self.text[:self.cursor]
        return before.count("\n"), len(before) - (before.rfind("\n") + 1)

    def _line_bounds(self) -> tuple[int, int]:
        start = self.text.rfind("\n", 0, self.cursor) + 1
        end = self.text.find("\n", self.cursor)
        return start, len(self.text) if end == -1 else end

    # ------------------------------------------------------------ editing
    def insert(self, text: str) -> None:
        self.text = self.text[:self.cursor] + text + self.text[self.cursor:]
        self.cursor += len(text)
        self.browse = None

    def paste(self, text: str) -> None:
        text = text.replace("\t", "    ")
        lines = text.count("\n") + 1
        if lines > PASTE_LINES or len(text) > PASTE_CHARS:
            token = f"[Pasted text #{len(self.pastes) + 1} +{lines} lines]"
            self.pastes[token] = text
            self.insert(token)
        else:
            self.insert(text)

    def backspace(self) -> None:
        if self.cursor:
            token = self._token_before()
            size = len(token) if token else 1
            self.text = self.text[:self.cursor - size] + self.text[self.cursor:]
            self.cursor -= size

    def delete(self) -> None:
        self.text = self.text[:self.cursor] + self.text[self.cursor + 1:]

    def _token_before(self) -> str:
        """A paste token ending at the cursor is deleted whole."""
        return next((token for token in self.pastes if self.text[:self.cursor].endswith(token)), "")

    def left(self) -> None:
        self.cursor = max(0, self.cursor - 1)

    def right(self) -> None:
        self.cursor = min(len(self.text), self.cursor + 1)

    def home(self) -> None:
        self.cursor = self._line_bounds()[0]

    def end(self) -> None:
        self.cursor = self._line_bounds()[1]

    def word_left(self) -> None:
        match = re.search(r"\w+\W*$", self.text[:self.cursor])
        self.cursor = match.start() if match else 0

    def word_right(self) -> None:
        match = re.match(r"\W*\w+", self.text[self.cursor:])
        self.cursor = self.cursor + match.end() if match else len(self.text)

    def kill_end(self) -> None:
        end = self._line_bounds()[1]
        end = end + 1 if end == self.cursor and end < len(self.text) else end
        self.killed = self.text[self.cursor:end]
        self.text = self.text[:self.cursor] + self.text[end:]

    def kill_start(self) -> None:
        start = self._line_bounds()[0]
        self.killed = self.text[start:self.cursor]
        self.text = self.text[:start] + self.text[self.cursor:]
        self.cursor = start

    def kill_word(self) -> None:
        match = re.search(r"\S*\s*$", self.text[:self.cursor])
        start = match.start() if match and match.group() else max(0, self.cursor - 1)
        self.killed = self.text[start:self.cursor]
        self.text = self.text[:start] + self.text[self.cursor:]
        self.cursor = start

    def kill_word_right(self) -> None:
        match = re.match(r"\s*\S*", self.text[self.cursor:])
        end = self.cursor + (match.end() if match else 0)
        self.killed = self.text[self.cursor:end]
        self.text = self.text[:self.cursor] + self.text[end:]

    def yank(self) -> None:
        if self.killed:
            self.insert(self.killed)

    # ------------------------------------------------------------ lines and history
    def up(self) -> None:
        row, col = self.row_col
        if row:
            start = self.text.rfind("\n", 0, self.text.rfind("\n", 0, self.cursor)) + 1
            length = self.text.find("\n", start) - start
            self.cursor = start + min(col, length)
        elif self.history and self.browse != 0:
            if self.browse is None:
                self.draft, self.browse = self.text, len(self.history)
            self.browse -= 1
            self.set(self.history[self.browse])
            self.cursor = min(len(self.text), self.text.find("\n") if "\n" in self.text
                              else len(self.text))

    def down(self) -> None:
        row, col = self.row_col
        if row < self.text.count("\n"):
            start = self.text.find("\n", self.cursor) + 1
            end = self.text.find("\n", start)
            length = (len(self.text) if end == -1 else end) - start
            self.cursor = start + min(col, length)
        elif self.browse is not None:
            self.browse += 1
            if self.browse >= len(self.history):
                self.browse = None
                self.set(self.draft)
            else:
                self.set(self.history[self.browse])

    def submit(self) -> str:
        """Take the text (pastes expanded), remember it, and start empty."""
        typed = self.text
        full = typed
        for token, text in self.pastes.items():
            full = full.replace(token, text)
        if typed.strip() and (not self.history or self.history[-1] != typed):
            self.history.append(typed)
            del self.history[:-HISTORY_LIMIT]
        self.clear()
        return full


# ---------------------------------------------------------------- history file
def load_history(path: Path) -> list[str]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    found = []
    for line in lines[-HISTORY_LIMIT:]:
        try:
            item = json.loads(line)
        except ValueError:
            continue
        if isinstance(item, str) and item.strip():
            found.append(item)
    return found


def save_history(path: Path, history: list[str]) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        text = "".join(json.dumps(item) + "\n" for item in history[-HISTORY_LIMIT:])
        path.write_text(text, encoding="utf-8")
        os.chmod(path, 0o600)
    except OSError:
        pass


# ---------------------------------------------------------------- suggestions
@dataclass(frozen=True)
class Suggestion:
    value: str      # what replaces the typed fragment
    label: str      # what the menu shows
    detail: str = ""
    submit: bool = False  # Enter on it runs the line straight away


@dataclass(frozen=True)
class Suggestions:
    items: list
    start: int      # the typed fragment being replaced: text[start:end]
    end: int


def rank(options, fragment: str, key=lambda item: item) -> list:
    """Prefix matches first, then words that start with it, then any substring,
    then fuzzy subsequence matches."""
    fragment = fragment.lower()
    buckets: list[list] = [[], [], [], []]
    for option in options:
        name = str(key(option)).lower()
        if name.startswith(fragment):
            buckets[0].append(option)
        elif re.search(r"[/_.\- ]" + re.escape(fragment), name):
            buckets[1].append(option)
        elif fragment in name:
            buckets[2].append(option)
        elif _subsequence(fragment, name):
            buckets[3].append(option)
    return [option for bucket in buckets for option in bucket]


def _subsequence(needle: str, hay: str) -> bool:
    position = 0
    for char in needle:
        position = hay.find(char, position) + 1
        if not position:
            return False
    return True


def suggest(text: str, cursor: int, commands: dict, argument_options, files) -> Suggestions | None:
    """What to offer for the line being typed.

    ``commands`` maps a name to its description; ``argument_options(name,
    previous)`` lists ``(value, detail)`` for the next argument of a command
    after the ``previous`` ones; ``files()`` lists project paths for ``@``.
    """
    before = text[:cursor]
    if before.startswith("/") and "\n" not in before:
        if " " not in before:
            names = rank(sorted(commands), before[1:])
            items = [Suggestion("/" + name + " ", "/" + name, commands[name], True)
                     for name in names]
            return Suggestions(items, 0, cursor) if items else None
        name, *previous, current = before[1:].split(" ")
        if len(previous) > 1 or "" in previous:
            return None
        options = list(argument_options(name, previous) or [])
        matches = rank(options, current, key=lambda option: option[0])
        items = [Suggestion(value + " ", value, detail, True) for value, detail in matches]
        if items and not (len(items) == 1 and items[0].label == current):
            return Suggestions(items, cursor - len(current), cursor)
        return None
    match = re.search(r"(?:^|\s)@([^\s]*)$", before)
    if match:
        fragment = match.group(1)
        paths = rank(files(), fragment, key=lambda path: path)[:50]
        items = [Suggestion("@" + path + ("" if path.endswith("/") else " "), "@" + path)
                 for path in paths]
        return Suggestions(items, match.start(1) - 1, cursor) if items else None
    return None


class FileIndex:
    """Project paths for @ mentions: git's view when there is one, else a walk."""

    LIMIT = 5000
    SKIP = frozenset({".git", "node_modules", "__pycache__", ".venv", "venv", "build", "dist",
                      ".mypy_cache", ".pytest_cache", ".tox", "target"})

    def __init__(self, root: str):
        self.root = root
        self._paths: list[str] | None = None

    def __call__(self) -> list[str]:
        if self._paths is None:
            self._paths = self._scan()
        return self._paths

    def _scan(self) -> list[str]:
        try:
            listed = subprocess.run(
                ["git", "ls-files", "--cached", "--others", "--exclude-standard"],
                cwd=self.root, capture_output=True, text=True, timeout=3, check=True).stdout
            paths = [line for line in listed.splitlines() if line][:self.LIMIT]
            if paths:
                folders = sorted({str(Path(path).parent) + "/" for path in paths
                                  if str(Path(path).parent) != "."})
                return sorted(paths + folders, key=lambda path: (path.count("/"), path))
        except (OSError, subprocess.SubprocessError):
            pass
        found: list[str] = []
        for base, folders, names in os.walk(self.root):
            folders[:] = sorted(f for f in folders if f not in self.SKIP and not f.startswith("."))
            relative = os.path.relpath(base, self.root)
            prefix = "" if relative == "." else relative + "/"
            found += [prefix + folder + "/" for folder in folders]
            found += [prefix + name for name in sorted(names)]
            if len(found) >= self.LIMIT:
                break
        return sorted(found[:self.LIMIT], key=lambda path: (path.count("/"), path))


def expand_mentions(text: str, root: str) -> tuple[str, list[tuple[str, int]]]:
    """Attach the files a message mentions with @path.

    Returns the text to send and ``(path, lines)`` for each file attached.
    Paths outside the project folder and binary files are left as written.
    """
    base = Path(root).resolve()
    attached, blocks, total = [], [], 0
    for match in MENTION.finditer(text):
        relative = match.group(1).rstrip(".,;:!?)")
        try:
            path = (base / relative).resolve()
            path.relative_to(base)
        except (OSError, ValueError):
            continue
        if not path.is_file() or relative in {name for name, _ in attached}:
            continue
        try:
            data = path.read_bytes()[:MENTION_BYTES]
        except OSError:
            continue
        if b"\0" in data or total + len(data) > MENTIONS_TOTAL:
            continue
        content = data.decode("utf-8", "replace")
        total += len(data)
        attached.append((relative, content.count("\n") + (0 if content.endswith("\n") else 1)))
        blocks.append(f'<file path="{relative}">\n{content.rstrip()}\n</file>')
    if not blocks:
        return text, []
    return text + "\n\n" + "\n\n".join(blocks), attached


__all__ = ['Editor', 'FileIndex', 'Suggestion', 'Suggestions', 'expand_mentions',
           'load_history', 'rank', 'save_history', 'suggest']
