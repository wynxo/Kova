"""Read-only project paths for the browser's ``@`` mentions (issue #197).

The terminal chat lists project paths with ``tui.editor.FileIndex``; the web
composer asks the server instead. Both want the same answer: filenames only,
never file contents, relative to one folder and confined to it. git's view is
used when the folder is a repository; otherwise a bounded walk skips the
folders that only hold generated files.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

#: Folders whose contents are noise in a file picker. Same set as the TUI.
SKIP = frozenset({
    ".git", ".hg", ".svn", "node_modules", "__pycache__", ".venv", "venv",
    "build", "dist", ".mypy_cache", ".pytest_cache", ".tox", "target",
})
#: Enough to cover a large repository without turning one keystroke into a scan.
LIMIT = 4000


def paths(root: str | Path, limit: int = LIMIT) -> list[str]:
    """Every file and folder under ``root``, as relative paths, sorted shallow first."""
    base = Path(root)
    try:
        base = base.resolve(strict=True)
    except (OSError, RuntimeError):
        return []
    if not base.is_dir():
        return []
    listed = _from_git(base)
    if listed:
        return _ordered(listed, limit)
    return _ordered(_walk(base, limit), limit)


def search(root: str | Path, query: str = "", limit: int = 20) -> list[str]:
    """Paths matching ``query`` anywhere in the name, best match first.

    The query is a case-insensitive substring, not a glob: the composer sends
    whatever follows ``@`` and should never turn a stray ``*`` into a pattern.
    """
    needle = str(query or "").strip().lower()
    found = paths(root)
    if not needle:
        return found[:limit]
    matches = [path for path in found if needle in path.lower()]
    basename = Path(needle).name
    matches.sort(key=lambda path: (
        0 if path.lower().startswith(needle) else 1 if Path(path).name.lower().startswith(basename) else 2,
        path.count("/"), len(path), path,
    ))
    return matches[:limit]


def _from_git(base: Path) -> list[str]:
    """git's tracked and untracked-unignored files, when ``base`` is a repo."""
    try:
        # fsmonitor would run a program named in the folder's own .git/config;
        # listing files must never execute anything.
        result = subprocess.run(
            ["git", "-c", "core.fsmonitor=false", "ls-files", "-z", "--cached", "--others",
             "--exclude-standard"],
            cwd=base, stdin=subprocess.DEVNULL, capture_output=True, timeout=3, check=True,
            # git writes paths as UTF-8; text=True alone would decode them with the
            # Windows code page and turn "café" into "cafÃ©".
            text=True, encoding="utf-8", errors="replace",
        )
    except (OSError, subprocess.SubprocessError):
        return []
    # -z: names arrive verbatim, not C-quoted ("caf\303\251.txt") like plain output.
    files = [name for name in result.stdout.split("\0") if name]
    folders = {"/".join(parts[:end]) + "/" for parts in (name.split("/") for name in files)
               for end in range(1, len(parts))}
    return files + sorted(folders)


def _walk(base: Path, limit: int) -> list[str]:
    found: list[str] = []
    for current, folders, names in os.walk(base):
        folders[:] = sorted(name for name in folders
                            if name not in SKIP and not name.startswith("."))
        relative = os.path.relpath(current, base)
        prefix = "" if relative == "." else relative.replace(os.sep, "/") + "/"
        found += [prefix + folder + "/" for folder in folders]
        found += [prefix + name for name in sorted(names) if name not in SKIP]
        if len(found) >= limit:
            break
    return found[:limit]


def _ordered(found: list[str], limit: int) -> list[str]:
    """Shallow paths first, then alphabetical: ``src/`` before ``src/a/b.py``."""
    return sorted(dict.fromkeys(found), key=lambda path: (path.count("/"), path))[:limit]


__all__ = ["LIMIT", "SKIP", "paths", "search"]
