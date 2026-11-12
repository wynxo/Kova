"""Project file operations adapted from the user's Kaine code_tools.py.

All file paths are contained in the chat working folder, including symlinks.
"""
from __future__ import annotations

import difflib
import fnmatch
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import tempfile

from . import ToolError

NOISE_DIRECTORIES = frozenset({".git", ".hg", ".svn", "node_modules", "__pycache__",
    ".venv", "venv", "dist", "build", ".pytest_cache", ".mypy_cache", ".ruff_cache"})

READ_TOOLS = frozenset({"read_file", "list_files", "search_files"})
WRITE_TOOLS = frozenset({"edit_file", "write_file"})
FILE_TOOLS = READ_TOOLS | WRITE_TOOLS

READ_MAX_LINES = 2000
READ_MAX_BYTES = 256 * 1024
READ_MAX_LINE_CHARS = 2000
LIST_MAX_ENTRIES = 1000
SEARCH_MAX_MATCHES = 200
SEARCH_LINE_CHARS = 300
SEARCH_TIMEOUT = 20
MAX_SEARCH_FILE_BYTES = 8 * 1024 * 1024
BINARY_SNIFF_BYTES = 8192
DIFF_MAX_LINES = 60

# ------------------------------------------------------------------- paths
def _root(project: str) -> Path | None:
    if not project:
        return None
    try:
        root = Path(project).expanduser().resolve(strict=True)
    except (OSError, RuntimeError):
        return None
    return root if root.is_dir() else None


def resolve_path(path: str, project: str) -> Path:
    """Resolve a path within the working folder, including symlink targets."""
    root = Path(project).expanduser().resolve()
    candidate = Path(path or ".").expanduser()
    target = (candidate if candidate.is_absolute() else root / candidate).resolve()
    if not target.is_relative_to(root):
        raise ToolError("Path is outside the working folder")
    return target



def _within(target: Path, root: Path | None) -> bool:
    return root is not None and (target == root or root in target.parents)


def _display(target: Path, project: str) -> str:
    root = _root(project)
    if root is not None and _within(target, root) and target != root:
        return target.relative_to(root).as_posix()
    return str(target)


def _error(message: str) -> dict:
    return {"ok": False, "error": message}


def _is_binary(sample: bytes) -> bool:
    return b"\x00" in sample[:BINARY_SNIFF_BYTES]


# --------------------------------------------------------------- read_file
def read_file(project: str, path: str, start_line: int = 1, end_line: int | None = None) -> dict:
    target = resolve_path(path, project)
    shown = _display(target, project)
    if not target.exists():
        return _error(f"There is no file at {shown}.")
    if target.is_dir():
        return _error(f"{shown} is a folder; use list_files to see what is in it.")
    start = max(1, int(start_line or 1))
    out: list[str] = []
    used = 0
    total = 0
    last = start - 1
    truncated = False
    with target.open("rb") as handle:
        if _is_binary(handle.read(BINARY_SNIFF_BYTES)):
            return _error(f"{shown} looks like a binary file, so it can't be shown as text.")
        handle.seek(0)
        for number, raw in enumerate(handle, 1):
            total = number
            if number < start or truncated:
                continue
            if end_line is not None and number > end_line:
                continue
            text = raw.rstrip(b"\r\n").decode("utf-8", errors="replace")
            if len(text) > READ_MAX_LINE_CHARS:
                text = text[:READ_MAX_LINE_CHARS] + "… [line cut]"
            line = f"{number:>6}\t{text}"
            size = len(line.encode("utf-8")) + 1
            if len(out) >= READ_MAX_LINES or used + size > READ_MAX_BYTES:
                truncated = True
                continue
            out.append(line)
            used += size
            last = number
    if start > max(total, 1) or (total == 0 and start > 1):
        return _error(f"start_line {start} is past the end of {shown} ({total} lines).")
    return {"ok": True, "path": shown, "content": "\n".join(out), "start_line": start,
            "end_line": last, "total_lines": total, "truncated": truncated}


# -------------------------------------------------------------- list_files
def _matches(relative: str, pattern: str | None) -> bool:
    if not pattern:
        return True
    return fnmatch.fnmatch(relative, pattern) or fnmatch.fnmatch(relative.rsplit("/", 1)[-1], pattern)


def list_files(project: str, path: str = ".", pattern: str | None = None) -> dict:
    base = resolve_path(path or ".", project)
    if not base.is_dir():
        return _error(f"{_display(base, project)} is not a folder.")
    entries: list[str] = []
    truncated = False
    for current, folders, files in os.walk(base):
        folders[:] = sorted(name for name in folders if name not in NOISE_DIRECTORIES)
        here = Path(current)
        names = [(name + "/", True) for name in folders] + [(name, False) for name in sorted(files)]
        for name, is_folder in sorted(names):
            relative = (here / name.rstrip("/")).relative_to(base).as_posix()
            if is_folder:
                if pattern:
                    continue
                relative += "/"
            elif name in NOISE_DIRECTORIES or not _matches(relative, pattern):
                continue
            if len(entries) >= LIST_MAX_ENTRIES:
                truncated = True
                break
            entries.append(relative)
        if truncated:
            break
    return {"ok": True, "path": _display(base, project), "entries": sorted(entries),
            "truncated": truncated}


# ------------------------------------------------------------ search_files
def _match_line(path: str, number: int | str, text: str) -> str:
    return f"{path}:{number}: {text[:SEARCH_LINE_CHARS]}"


def _search_rg(rg: str, base: Path, query: str, regex: bool, glob: str | None) -> dict:
    command = [rg, "--line-number", "--no-heading", "--color", "never",
               "--no-follow", "--null", "--max-filesize", str(MAX_SEARCH_FILE_BYTES)]
    if not regex:
        command.append("--fixed-strings")
    if glob:
        command += ["--glob", glob]
    for directory in sorted(NOISE_DIRECTORIES):
        command += ["--glob", f"!{directory}/**"]
    command += ["--", query, "."]
    try:
        process = subprocess.run(command, cwd=base, stdin=subprocess.DEVNULL,
                                 capture_output=True, timeout=SEARCH_TIMEOUT)
    except subprocess.TimeoutExpired:
        return _error("The search took too long; narrow it with path or glob.")
    if process.returncode not in (0, 1):
        message = process.stderr.decode("utf-8", errors="replace").strip().splitlines()
        return _error("Search failed: " + (message[-1] if message else "ripgrep error"))
    matches: list[str] = []
    truncated = False
    for raw in process.stdout.decode("utf-8", errors="replace").splitlines():
        name, _, rest = raw.partition("\x00")
        number, _, text = rest.partition(":")
        if len(matches) >= SEARCH_MAX_MATCHES:
            truncated = True
            break
        matches.append(_match_line(name.removeprefix("./"), number, text))
    return {"ok": True, "matches": matches, "truncated": truncated, "engine": "rg"}


def _search_python(base: Path, query: str, regex: bool, glob: str | None) -> dict:
    try:
        pattern = re.compile(query if regex else re.escape(query))
    except re.error as exc:
        return _error(f"Invalid regular expression: {exc}.")
    matches: list[str] = []
    truncated = False
    for current, folders, files in os.walk(base):
        folders[:] = sorted(name for name in folders if name not in NOISE_DIRECTORIES)
        for name in sorted(files):
            target = Path(current) / name
            relative = target.relative_to(base).as_posix()
            if not _matches(relative, glob):
                continue
            try:
                # Never follow a project symlink into a private file. The
                # descriptor-level check closes the last-component symlink
                # race between inspecting the path and opening it.
                if target.is_symlink():
                    continue
                descriptor = os.open(
                    target, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
                    | getattr(os, "O_NONBLOCK", 0)
                )
                with os.fdopen(descriptor, "rb") as handle:
                    if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
                        continue
                    data = handle.read(MAX_SEARCH_FILE_BYTES + 1)
            except OSError:
                continue
            if len(data) > MAX_SEARCH_FILE_BYTES:
                truncated = True
                continue
            if _is_binary(data):
                continue
            for number, line in enumerate(data.decode("utf-8", errors="replace").splitlines(), 1):
                if pattern.search(line):
                    if len(matches) >= SEARCH_MAX_MATCHES:
                        return {"ok": True, "matches": matches, "truncated": True,
                                "engine": "python"}
                    matches.append(_match_line(relative, number, line))
    return {"ok": True, "matches": matches, "truncated": truncated, "engine": "python"}


def search_files(project: str, query: str, path: str = ".", regex: bool = False,
                 glob: str | None = None) -> dict:
    base = resolve_path(path or ".", project)
    if not base.is_dir():
        return _error(f"{_display(base, project)} is not a folder.")
    if regex:
        try:
            re.compile(query)
        except re.error as exc:
            return _error(f"Invalid regular expression: {exc}.")
    rg = shutil.which("rg")
    if rg:
        return _search_rg(rg, base, query, regex, glob)
    return _search_python(base, query, regex, glob)


# ------------------------------------------------------------------ writes
def _umask() -> int:
    try:
        with open("/proc/self/status", encoding="ascii") as status:
            for line in status:
                if line.startswith("Umask:"):
                    return int(line.split()[1], 8)
    except (OSError, ValueError, IndexError):
        pass
    return 0o022


class _EditConflict(OSError):
    """A file changed between the agent's read and its atomic replacement."""


def _atomic_write(target: Path, data: bytes, *,
                  expected: bytes | None = None, expect_missing: bool = False) -> None:
    """Atomically replace a file, optionally checking its previously read bytes."""
    mode = (target.stat().st_mode & 0o7777) if target.exists() else (0o666 & ~_umask())
    handle = tempfile.NamedTemporaryFile(dir=target.parent, prefix=f".{target.name}.",
                                         suffix=".kova-tmp", delete=False)
    temporary = Path(handle.name)
    try:
        with handle:
            handle.write(data)
        os.chmod(temporary, mode)
        if expected is not None:
            try:
                current = target.read_bytes()
            except FileNotFoundError as exc:
                raise _EditConflict("The file disappeared during editing") from exc
            if current != expected:
                raise _EditConflict("The file changed on disk during editing")
        elif expect_missing and target.exists():
            raise _EditConflict("The file appeared on disk during writing")
        os.replace(temporary, target)
    finally:
        if temporary.exists():
            try:
                temporary.unlink()
            except OSError:
                pass


def _diff(before: str, after: str, shown: str) -> str:
    lines = list(difflib.unified_diff(before.splitlines(), after.splitlines(),
                                      f"a/{shown}", f"b/{shown}", lineterm=""))
    if len(lines) > DIFF_MAX_LINES:
        lines = lines[:DIFF_MAX_LINES] + ["… diff cut"]
    return "\n".join(lines)


def edit_file(project: str, path: str, old_text: str, new_text: str,
              replace_all: bool = False) -> dict:
    target = resolve_path(path, project)
    shown = _display(target, project)
    if not old_text:
        return _error("old_text is empty; quote the exact text to replace, or use write_file.")
    if not target.is_file():
        return _error(f"There is no file at {shown}." if not target.exists()
                      else f"{shown} is a folder, not a file.")
    data = target.read_bytes()
    if _is_binary(data):
        return _error(f"{shown} looks like a binary file; it can't be edited as text.")
    try:
        before = data.decode("utf-8")
    except UnicodeDecodeError:
        return _error(f"{shown} is not valid UTF-8 text, so editing it could corrupt it.")
    old, new = old_text, new_text
    count = before.count(old)
    if count == 0 and "\r\n" in before and "\n" in old and "\r\n" not in old:
        old, new = old.replace("\n", "\r\n"), new.replace("\n", "\r\n")
        count = before.count(old)
    if count == 0:
        return _error(f"old_text was not found in {shown}. Read the file again and copy "
                      "the text exactly.")
    if count > 1 and not replace_all:
        return _error(f"old_text appears {count} times in {shown}. Add surrounding lines so "
                      "it matches once, or set replace_all.")
    after = before.replace(old, new) if replace_all else before.replace(old, new, 1)
    try:
        _atomic_write(target, after.encode("utf-8"), expected=data)
    except _EditConflict:
        return _error(f"{shown} changed on disk while editing. Read it again before retrying.")
    except OSError as exc:
        return _error(f"Could not save {shown}: {exc.strerror or exc}.")
    return {"ok": True, "path": shown, "replacements": count if replace_all else 1,
            "diff": _diff(before, after, shown), "changed": [shown]}


def write_file(project: str, path: str, content: str) -> dict:
    target = resolve_path(path, project)
    shown = _display(target, project)
    if target.is_dir():
        return _error(f"{shown} is a folder, not a file.")
    created = not target.exists()
    try:
        expected = target.read_bytes() if not created else None
        target.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write(target, str(content).encode("utf-8"), expected=expected, expect_missing=created)
    except OSError as exc:
        return _error(f"Could not write {shown}: {exc.strerror or exc}.")
    return {"ok": True, "path": shown, "created": created, "changed": [shown]}


