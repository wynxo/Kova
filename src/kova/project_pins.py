"""Validate project-relative file references without reading files (#196)."""

from __future__ import annotations

import re
from pathlib import PurePosixPath

MAX_PINS = 20
#: One reference, in characters. Long enough for a deep but sane repository path.
MAX_PIN_CHARS = 400
_WINDOWS_DRIVE = re.compile(r"^[A-Za-z]:")
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
