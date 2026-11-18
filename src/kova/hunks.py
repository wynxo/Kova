"""Per-hunk review of one file in a change set, and undoing one hunk (issue #190).

A change set (see :mod:`kova.checkpoints`) stores a file's bytes from before
and after a turn. The hunks here are the blocks that differ between those two
sides. Each hunk is then compared against the file *as it is now*:

* ``applied``: the turn's lines are still in the file, untouched. Undo is offered.
* ``undone``: the region now reads as it did before the turn.
* ``changed``: something else edited that region since; undoing it here could
  throw that edit away, so it is not offered.
* ``missing``: the file is gone.

Undo reverts exactly one applied hunk in the current file and nothing else. It
refuses when the file's hash differs from the one the viewer was shown, when the
path leaves the chat's working folder, and when any part of the path is a
symbolic link. Every undo is recorded as a new change set, so it can be reviewed
and restored like any edit Kova makes.
"""

from __future__ import annotations

import difflib
import hashlib
import os
import stat
import threading
import zlib
from pathlib import Path

from kova.checkpoints import MAX_FILE_BYTES, MAX_PREVIEW_CHARS, BlobStore, _write, line_counts

# One undo at a time: read, check and write must not interleave.
_lock = threading.Lock()


class HunkError(Exception):
    """A refusal with the HTTP status that describes it."""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


def file_hash(data: bytes | None) -> str | None:
    return None if data is None else hashlib.sha256(data).hexdigest()


def _display(line: str) -> str:
    """A line without its line ending, for showing."""
    return (line.splitlines() or [""])[0]


def _lines(text: str) -> list[str]:
    """Lines with their endings kept, so joining them gives back the exact text."""
    return text.splitlines(keepends=True)


def _matcher(a: list[str], b: list[str]) -> difflib.SequenceMatcher:
    return difflib.SequenceMatcher(None, a, b, autojunk=False)


# -- the working folder -------------------------------------------------------

def contained(root: Path, relative: str) -> Path:
    """The file ``relative`` names under ``root``, or a refusal.

    The path must be relative, must not climb out with ``..``, and no part of
    it may be a symbolic link: a link could point anywhere on the computer.
    """
    if not relative or "\x00" in relative:
        raise HunkError(400, "No file was named.")
    rel = Path(relative)
    if rel.is_absolute() or rel.anchor or any(part in ("..", "") for part in rel.parts):
        raise HunkError(403, "That file is outside the chat's working folder.")
    base = Path(root).resolve()
    current = base
    for part in rel.parts:
        current = current / part
        if current.is_symlink():
            raise HunkError(403, f"{relative} goes through a symbolic link, so Kova will not change it.")
    try:
        if not current.resolve().is_relative_to(base):
            raise HunkError(403, "That file is outside the chat's working folder.")
    except OSError as exc:
        raise HunkError(409, f"{relative} cannot be read: {exc}") from exc
    return current


def read_current(target: Path) -> bytes | None:
    """The file's bytes without following a link at the last step; None when it is gone."""
    try:
        info = os.lstat(target)
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(info.st_mode):
        raise HunkError(403, "That path is not a regular file.")
    if info.st_size > MAX_FILE_BYTES:
        raise HunkError(413, "That file is too large to undo one change in.")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
    try:
        handle = os.open(target, flags)
    except OSError as exc:
        raise HunkError(403, f"That file cannot be opened: {exc.strerror or exc}") from exc
    with os.fdopen(handle, "rb") as stream:
        return stream.read(MAX_FILE_BYTES + 1)


# -- hunks ---------------------------------------------------------------------

def _text(blobs: BlobStore, digest: str | None) -> str | None:
    """Strict UTF-8 text of one side; a binary side raises."""
    if digest is None:
        return ""
    data = blobs.get(digest)
    return data.decode("utf-8")


def _touched(ops: list[tuple], start: int, end: int) -> bool:
    """Whether any change between two versions reaches ``[start, end]`` (inclusive, so neighbours count)."""
    return any(tag != "equal" and i1 <= end and i2 >= start for tag, i1, i2, _, _ in ops)


def _offset(ops: list[tuple], start: int, end: int, same: bool) -> int | None:
    """How far ``[start, end)`` of one version moved in the next, when it sits in one unchanged run."""
    if same:
        return 0
    for tag, i1, i2, j1, _ in ops:
        if tag == "equal" and i1 <= start and end <= i2:
            return j1 - i1
    return None


def _reads_as_before(ops: list[tuple], after: list[str], current: list[str], hunk: dict) -> bool:
    """Whether the region the later edits touched now reads as before the turn.

    The touching edits span ``[low, high)`` of ``after``; that span maps to one
    run of ``current``. It is undone when that run equals the span with the
    hunk's added lines swapped back for its removed ones.
    """
    j1, j2 = hunk["new"]
    touching = [op for op in ops if op[0] != "equal" and op[1] <= j2 and op[2] >= j1]
    first, last = touching[0], touching[-1]
    low, high = min(first[1], j1), max(last[2], j2)
    start = first[3] - (first[1] - low)
    end = last[4] + (high - last[2])
    return current[start:end] == after[low:j1] + hunk["removed"] + after[j2:high]


def _hunk_id(removed: list[str], added: list[str], seen: dict[str, int]) -> str:
    """An id from the hunk's content, not its line numbers; repeats are numbered."""
    body = "\x00".join(removed) + "\x01" + "\x00".join(added)
    ordinal = seen.get(body, 0)
    seen[body] = ordinal + 1
    return hashlib.sha256(f"{body}\x02{ordinal}".encode("utf-8", "surrogatepass")).hexdigest()[:16]


def _analyse(before: list[str], after: list[str], current: list[str] | None) -> list[dict]:
    """Each hunk between ``before`` and ``after``, with its state against ``current``."""
    ops = [op for op in _matcher(before, after).get_opcodes() if op[0] != "equal"]
    later = _matcher(after, current).get_opcodes() if current is not None else []
    same = current == after
    seen: dict[str, int] = {}
    hunks = []
    for _, i1, i2, j1, j2 in ops:
        removed, added = before[i1:i2], after[j1:j2]
        hunk = {"id": _hunk_id(removed, added, seen), "old": (i1, i2), "new": (j1, j2),
                "removed": removed, "added": added, "offset": None}
        if current is None:
            hunk["state"] = "missing"
        elif same or not _touched(later, j1, j2):
            hunk["offset"] = _offset(later, j1, j2, same)
            hunk["state"] = "applied" if hunk["offset"] is not None else "changed"
        else:
            hunk["state"] = "undone" if _reads_as_before(later, after, current, hunk) else "changed"
        hunks.append(hunk)
    return hunks


def _header(hunk: dict) -> str:
    (i1, i2), (j1, j2) = hunk["old"], hunk["new"]
    old_start = i1 + 1 if i2 > i1 else i1
    new_start = j1 + 1 if j2 > j1 else j1
    return f"@@ -{old_start},{i2 - i1} +{new_start},{j2 - j1} @@"


def _sides(blobs: BlobStore, entry: dict, current_bytes: bytes | None):
    """The three versions as line lists, or a reason they cannot be shown."""
    try:
        before = _text(blobs, entry.get("before"))
        after = _text(blobs, entry.get("after"))
        current = None if current_bytes is None else current_bytes.decode("utf-8")
    except UnicodeDecodeError:
        return None, "This file is not text, so it has no line-by-line view."
    except (OSError, zlib.error):
        return None, "This change set's saved copy of the file is missing."
    if max(len(before), len(after), len(current or "")) > MAX_PREVIEW_CHARS:
        return None, "This file is too large for the line-by-line view."
    return (_lines(before), _lines(after), None if current is None else _lines(current)), None


def view(blobs: BlobStore, root: Path, path: str, entry: dict) -> dict:
    """Everything the diff panel shows for one file of one change set.

    ``segments`` alternate between unchanged runs (``same``, with their lines so
    the viewer can expand them) and hunks, in file order.
    """
    target = contained(root, path)
    current_bytes = read_current(target)
    result = {"path": path, "status": entry.get("status"),
              "additions": entry.get("additions", 0), "deletions": entry.get("deletions", 0),
              "current_hash": file_hash(current_bytes), "supported": True, "reason": None,
              "segments": []}
    sides, reason = _sides(blobs, entry, current_bytes)
    if sides is None:
        return {**result, "supported": False, "reason": reason}
    before, after, current = sides
    hunks = _analyse(before, after, current)
    old = new = 0
    for hunk in hunks:
        (i1, i2), (j1, j2) = hunk["old"], hunk["new"]
        if j1 > new:
            result["segments"].append({"type": "same", "old": old + 1, "new": new + 1,
                                       "lines": [_display(line) for line in after[new:j1]]})
        result["segments"].append({
            "type": "hunk", "id": hunk["id"], "header": _header(hunk), "state": hunk["state"],
            "old": i1 + 1, "new": j1 + 1,
            "removed": [_display(line) for line in hunk["removed"]],
            "added": [_display(line) for line in hunk["added"]]})
        old, new = i2, j2
    if new < len(after):
        result["segments"].append({"type": "same", "old": old + 1, "new": new + 1,
                                   "lines": [_display(line) for line in after[new:]]})
    return result


def undo(blobs: BlobStore, root: Path, path: str, entry: dict, hunk_id: str,
         expected_hash: str) -> dict:
    """Revert one applied hunk in the working file.

    Returns the new file's ``before``/``after`` digests and line counts, shaped
    like a change-set entry so the caller can record it.
    """
    with _lock:
        target = contained(root, path)
        current_bytes = read_current(target)
        if current_bytes is None:
            raise HunkError(409, f"{path} no longer exists, so there is nothing to undo.")
        if file_hash(current_bytes) != expected_hash:
            raise HunkError(409, f"{path} changed since you opened this diff. "
                                 "Reopen it to see the current changes.")
        sides, reason = _sides(blobs, entry, current_bytes)
        if sides is None:
            raise HunkError(409, reason)
        before, after, current = sides
        hunk = next((item for item in _analyse(before, after, current) if item["id"] == hunk_id), None)
        if hunk is None:
            raise HunkError(404, "That change is not part of this file's change set.")
        if hunk["state"] != "applied":
            raise HunkError(409, "That change was already undone or edited since, "
                                 "so it cannot be undone on its own.")
        start, end = (index + hunk["offset"] for index in hunk["new"])
        updated = "".join(current[:start] + hunk["removed"] + current[end:]).encode("utf-8")
        # The file must not move under us between the check and the write.
        if file_hash(read_current(contained(root, path))) != expected_hash:
            raise HunkError(409, f"{path} changed while undoing. Reopen it and try again.")
        _write(target, updated)
    old_text, new_text = current_bytes.decode("utf-8"), updated.decode("utf-8")
    additions, deletions = line_counts(old_text, new_text)
    return {"before": blobs.put(current_bytes), "after": blobs.put(updated), "status": "modified",
            "additions": additions, "deletions": deletions, "hash": file_hash(updated)}
