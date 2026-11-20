"""Pinned project files: explicit references, never automatic content (#196).

A project may list files that matter to it, relative to its working folder.
The list is guidance for the model and a label in the sidebar; Kova never reads
those files for this feature. Their contents are not copied into the prompt,
the database or a backup, and pinning one grants no access a turn did not
already have: the file tools still stay inside the chat's working folder.
"""

from __future__ import annotations

import re
from pathlib import PurePosixPath

#: Enough references to describe a project without turning the prompt into a
#: file listing. The composer's ``@`` list has its own, larger limit.
MAX_PINS = 20
#: One reference, in characters. Long enough for a deep but sane repository path.
MAX_PIN_CHARS = 400
#: UTF-8 bytes the references may take in one system prompt.
PROMPT_BUDGET = 2_000

_WINDOWS_DRIVE = re.compile(r"^[A-Za-z]:")
_INTRO = (
    "Pinned project files (explicit references only; their contents are not "
    "included). Open one with read_file when it matters; file tools still stay "
    "inside the chat's working folder."
)


def _one(value: object) -> str:
    """One relative path inside a project folder, normalised, or ``ValueError``.

    This is lexical only: it never touches the filesystem, so pinning a name
    cannot read it, reveal whether it exists, or follow a symlink.
    """
    if not isinstance(value, str):
        raise ValueError("a pinned file must be a path string")
    text = value.strip()
    if not text:
        return ""
    if len(text) > MAX_PIN_CHARS:
        raise ValueError(f"a pinned file path is longer than {MAX_PIN_CHARS} characters")
    if any(ord(char) < 32 or ord(char) == 127 for char in text):
        raise ValueError("a pinned file path cannot contain control characters")
    # A path copied from Windows is normalised, but never treated as absolute.
    normalized = text.replace("\\", "/")
    if (normalized.startswith("/") or normalized.startswith("~")
            or _WINDOWS_DRIVE.match(normalized)):
        raise ValueError("pin files by a path inside the project folder, not an absolute path")
    if normalized.endswith("/"):
        raise ValueError("pin files, not folders")
    parts = PurePosixPath(normalized).parts
    if not parts or any(part in (".", "..") for part in parts):
        raise ValueError("a pinned file path must stay inside the project folder")
    return PurePosixPath(*parts).as_posix()


def pins(values: object, cwd: str | None) -> list[str]:
    """The pinned paths for a project, validated against its working folder.

    A project needs a folder before it can pin files, because every reference
    is relative to that folder. Empty entries are ignored, duplicates collapse,
    and order is kept so the list reads the way the user wrote it.
    """
    if not isinstance(values, (list, tuple)):
        raise ValueError("pinned files must be a list of paths")
    kept: list[str] = []
    for value in values:
        path = _one(value)
        if path and path not in kept:
            kept.append(path)
    if not kept:
        return []
    if not isinstance(cwd, str) or not cwd.strip():
        raise ValueError("Set a working folder before pinning files.")
    if len(kept) > MAX_PINS:
        raise ValueError(f"A project can pin at most {MAX_PINS} files.")
    return kept


def salvage(values: object, cwd: str | None) -> list[str]:
    """The valid pins from a hand-edited list, dropping the rest.

    Backup restore follows the same rule as chats and project looks: a damaged
    entry narrows the result instead of failing the whole restore.
    """
    if not isinstance(values, (list, tuple)):
        return []
    kept: list[str] = []
    for value in values:
        try:
            for path in pins([value], cwd):
                if path not in kept:
                    kept.append(path)
        except ValueError:
            continue
    return kept[:MAX_PINS]


def render(files: list[str], cwd: str | None, budget: int = PROMPT_BUDGET) -> str:
    """The prompt block for a project's pins, or ``""`` when there is none.

    Only names and their base folder reach the model. The block is cut to
    ``budget`` UTF-8 bytes and says how many references were left out.
    """
    if not files or not cwd:
        return ""
    header = f"{_INTRO}\nProject folder: {cwd}\n"
    room = budget - len(header.encode("utf-8"))
    if room <= 0:
        return ""
    lines: list[str] = []
    used = 0
    for index, path in enumerate(files):
        line = f"- {path}"
        cost = len(line.encode("utf-8")) + 1  # the "\n" before the next line
        if used + cost > room:
            left = len(files) - index
            note = f"… {left} more pinned file{'s' if left != 1 else ''} not shown"
            cost = len(note.encode("utf-8"))
            # Make room for the note rather than ending on a silent cut: a
            # reference list that hides entries should say so.
            while lines and used + cost > room:
                used -= len(lines.pop().encode("utf-8")) + 1
            if used + cost <= room:
                lines.append(note)
            break
        lines.append(line)
        used += cost
    return header + "\n".join(lines)


__all__ = ["MAX_PIN_CHARS", "MAX_PINS", "PROMPT_BUDGET", "pins", "render", "salvage"]
