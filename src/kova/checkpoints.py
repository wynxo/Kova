"""Local, git-independent checkpoints of what a turn changed.

Kova's file tools are the only writers inside a working folder, so a checkpoint
covers exactly the files one turn touched: the bytes from just before the first
write, and the bytes after the turn. Both sides live in a content-addressed,
zlib-compressed blob store, so unchanged files and repeated edits cost nothing
to record. A checkpoint is therefore a point in time (the state *before* its
turn), and restoring one puts back every path a turn from that point on changed
and removes the files those turns created.

Nothing here reads or writes the user's git repository: blobs live under
``KOVA_HOME/checkpoints``, never inside the working folder, and a restore
touches only the paths recorded in the checkpoints being reverted.
"""

from __future__ import annotations

import difflib
import hashlib
import os
import tempfile
import zlib
from pathlib import Path

#: A file larger than this is not checkpointed; it is reported as skipped.
MAX_FILE_BYTES = 8 * 1024 * 1024
#: Preview text and diffs are cut here before they leave the server.
MAX_PREVIEW_CHARS = 200_000


def read_bytes(path: Path) -> bytes | None:
    """The file's bytes, or None when it is missing or not a regular file."""
    try:
        if not path.is_file():
            return None
        return path.read_bytes()
    except OSError:
        return None


def decode(data: bytes | None) -> str | None:
    """Text for a preview; binary and undecodable bytes are shown as replacement."""
    return None if data is None else data.decode("utf-8", "replace")


class BlobStore:
    """Content-addressed, compressed file contents under one directory."""

    def __init__(self, root: Path):
        self.root = Path(root)

    def _path(self, digest: str) -> Path:
        return self.root / digest[:2] / digest

    def has(self, digest: str) -> bool:
        return self._path(digest).is_file()

    def put(self, data: bytes) -> str:
        """Store bytes once, returning their digest; an existing blob is reused."""
        digest = hashlib.sha256(data).hexdigest()
        path = self._path(digest)
        if not path.is_file():
            path.parent.mkdir(parents=True, exist_ok=True)
            handle, temporary = tempfile.mkstemp(prefix=".blob-", dir=path.parent)
            try:
                with os.fdopen(handle, "wb") as stream:
                    stream.write(zlib.compress(data, 6))
                os.replace(temporary, path)
            finally:
                if os.path.exists(temporary):
                    os.unlink(temporary)
        return digest

    def get(self, digest: str) -> bytes:
        return zlib.decompress(self._path(digest).read_bytes())

    def text(self, digest: str | None) -> str | None:
        """Decoded contents of one blob, or None when there is no such side."""
        if digest is None:
            return None
        try:
            return decode(self.get(digest))
        except (OSError, zlib.error):
            return None


class ChangeTracker:
    """The files one turn wrote, with their contents from just before that turn.

    ``note`` is called with each write tool's target before the write lands; the
    first call for a path wins, so the recorded side is the state at turn start
    even when a later round of the same turn edits the file again.
    """

    def __init__(self, blobs: BlobStore, root: Path):
        self.blobs = blobs
        self.root = Path(root).resolve()
        self.before: dict[str, str | None] = {}
        self.skipped: list[str] = []

    def note(self, path: Path) -> None:
        """Remember a file's current contents, once, before a tool overwrites it."""
        try:
            target = Path(path).resolve()
            relative = str(target.relative_to(self.root))
        except (OSError, ValueError):
            return  # outside the working folder: not ours to checkpoint
        if relative in self.before or relative in self.skipped:
            return
        data = read_bytes(target)
        if data is None:
            self.before[relative] = None  # the turn creates this file
        elif len(data) > MAX_FILE_BYTES:
            self.skipped.append(relative)
        else:
            self.before[relative] = self.blobs.put(data)

    @property
    def touched(self) -> bool:
        return bool(self.before) or bool(self.skipped)

    def finish(self) -> tuple[dict[str, dict], list[str]]:
        """The changed files of this turn, and the paths too large to record."""
        files: dict[str, dict] = {}
        for relative, before in self.before.items():
            after = self._capture(self.root / relative)
            if after == before:
                continue
            before_text = self.blobs.text(before)
            after_text = self.blobs.text(after)
            additions, deletions = line_counts(before_text or "", after_text or "")
            files[relative] = {"before": before, "after": after,
                               "status": status_of(before, after),
                               "additions": additions, "deletions": deletions}
        return files, list(self.skipped)

    def _capture(self, target: Path) -> str | None:
        data = read_bytes(target)
        if data is None:
            return None
        if len(data) > MAX_FILE_BYTES:
            return None
        return self.blobs.put(data)


def status_of(before: str | None, after: str | None) -> str:
    """How a file changed between two blob digests."""
    if before is None:
        return "added"
    if after is None:
        return "deleted"
    return "modified"


def line_counts(before: str, after: str) -> tuple[int, int]:
    """Added and removed lines between two texts, like ``git diff --numstat``."""
    additions = deletions = 0
    for line in difflib.unified_diff(before.splitlines(), after.splitlines(), n=0, lineterm=""):
        if line.startswith("+") and not line.startswith("+++"):
            additions += 1
        elif line.startswith("-") and not line.startswith("---"):
            deletions += 1
    return additions, deletions


def unified_diff(path: str, before: str, after: str) -> str:
    """A unified diff of one file, labelled ``a/`` and ``b/`` like git's."""
    return "\n".join(difflib.unified_diff(before.splitlines(), after.splitlines(),
                                          fromfile=f"a/{path}", tofile=f"b/{path}", lineterm=""))


def _cut(text: str | None) -> tuple[str | None, bool]:
    if text is None or len(text) <= MAX_PREVIEW_CHARS:
        return text, False
    return text[:MAX_PREVIEW_CHARS], True


def preview(blobs: BlobStore, path: str, entry: dict) -> dict:
    """One changed file's diff and both sides, for a review panel."""
    before = blobs.text(entry.get("before"))
    after = blobs.text(entry.get("after"))
    before, before_cut = _cut(before)
    after, after_cut = _cut(after)
    diff, diff_cut = _cut(unified_diff(path, before or "", after or ""))
    return {"path": path,
            "status": entry.get("status") or status_of(entry.get("before"), entry.get("after")),
            "additions": entry.get("additions", 0), "deletions": entry.get("deletions", 0),
            "before": before, "after": after, "diff": diff,
            "truncated": before_cut or after_cut or diff_cut}


def revert_plan(rows: list[dict], checkpoint_id: int) -> list[tuple[str, str | None]]:
    """The state each path had before ``checkpoint_id``, from that turn onward.

    A path's earliest touch at or after the checkpoint holds its pre-turn
    contents, which is exactly what restoring to that checkpoint must put back.
    """
    plan: dict[str, str | None] = {}
    for row in sorted(rows, key=lambda item: item["id"]):
        if row["id"] < checkpoint_id:
            continue
        for path, entry in (row.get("files") or {}).items():
            plan.setdefault(path, entry.get("before"))
    return list(plan.items())


def _write(target: Path, data: bytes) -> None:
    """Replace a file atomically, keeping its permissions when it already exists."""
    mode = target.stat().st_mode & 0o7777 if target.exists() else None
    target.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(prefix=".kova-restore-", dir=target.parent)
    try:
        with os.fdopen(handle, "wb") as stream:
            stream.write(data)
        if mode is not None:
            os.chmod(temporary, mode)
        os.replace(temporary, target)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def apply_restore(root: Path, plan: list[tuple[str, str | None]], blobs: BlobStore) -> dict:
    """Put the working folder back: write recorded contents, remove created files.

    Only paths recorded by the reverted checkpoints are touched; every other
    file in the folder, and the user's git state, is left exactly as it is.
    """
    base = Path(root).resolve()
    restored: list[str] = []
    deleted: list[str] = []
    failed: list[dict] = []
    for relative, digest in plan:
        target = base / relative
        try:
            if digest is None:
                if target.is_file() or target.is_symlink():
                    target.unlink()
                    deleted.append(relative)
            else:
                _write(target, blobs.get(digest))
                restored.append(relative)
        except (OSError, zlib.error) as exc:
            failed.append({"path": relative, "error": str(exc)})
    return {"restored": restored, "deleted": deleted, "failed": failed}


def summarize(files: dict[str, dict]) -> dict:
    """Totals for a change set, for a one-line summary in a list or card."""
    counts = {"added": 0, "modified": 0, "deleted": 0}
    additions = deletions = 0
    for entry in files.values():
        counts[entry.get("status", "modified")] = counts.get(entry.get("status", "modified"), 0) + 1
        additions += entry.get("additions", 0)
        deletions += entry.get("deletions", 0)
    return {"files": len(files), "additions": additions, "deletions": deletions, **counts}
