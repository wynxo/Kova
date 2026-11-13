"""Git-independent, local checkpoints for reviewing one agent turn.

Snapshots are staged outside the working folder and use reflinks where the
filesystem supports them. Only originals for changed/deleted files are kept
after the turn; files created by the turn are represented by their paths.
"""

from __future__ import annotations

import difflib
import filecmp
import json
import os
import shutil
import stat
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from uuid import uuid4

_FICLONE = 0x40049409
_MAX_DIFF = 120_000


@dataclass
class Snapshot:
    root: Path
    staging: Path
    checkpoint_root: Path
    files: dict[str, tuple[int, int]]


def _inside(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _clone(source: Path, target: Path) -> None:
    """Copy one file using copy-on-write when the filesystem supports it."""
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        import fcntl
        with source.open("rb") as src, target.open("wb") as dst:
            fcntl.ioctl(dst.fileno(), _FICLONE, src.fileno())
        shutil.copystat(source, target, follow_symlinks=False)
    except (ImportError, OSError):
        target.unlink(missing_ok=True)
        shutil.copy2(source, target, follow_symlinks=False)


def _walk(root: Path, excluded: Path) -> list[Path]:
    found: list[Path] = []
    for base, dirs, names in os.walk(root, followlinks=False):
        here = Path(base)
        dirs[:] = [name for name in dirs if name != ".git" and
                   not _inside((here / name).resolve(strict=False), excluded)]
        for name in names:
            path = here / name
            if _inside(path.resolve(strict=False), excluded):
                continue
            try:
                info = path.lstat()
            except OSError:
                continue
            if stat.S_ISREG(info.st_mode):
                found.append(path)
    return found


class Checkpoints:
    """Create, inspect and restore local per-turn checkpoints."""

    def __init__(self, home: Path):
        self.home = Path(home).resolve()
        self.root = self.home / "checkpoints"
        self.root.mkdir(parents=True, exist_ok=True)

    def begin(self, working_folder: Path) -> Snapshot:
        root = Path(working_folder).resolve(strict=True)
        staging = Path(tempfile.mkdtemp(prefix=".turn-", dir=self.root))
        files: dict[str, tuple[int, int]] = {}
        try:
            for source in _walk(root, self.home):
                relative = source.relative_to(root).as_posix()
                info = source.stat()
                _clone(source, staging / "tree" / relative)
                files[relative] = (info.st_size, info.st_mtime_ns)
        except BaseException:
            shutil.rmtree(staging, ignore_errors=True)
            raise
        return Snapshot(root, staging, self.home, files)

    def finish(self, snapshot: Snapshot, chat_id: str) -> dict | None:
        """Persist a changed-file checkpoint and unified diff, if anything changed."""
        try:
            current = {path.relative_to(snapshot.root).as_posix(): path
                       for path in _walk(snapshot.root, snapshot.checkpoint_root)}
            old = snapshot.files
            changed = sorted(path for path in current.keys() & old.keys()
                             if not filecmp.cmp(current[path], snapshot.staging / "tree" / path,
                                                shallow=False))
            created = sorted(current.keys() - old.keys())
            deleted = sorted(old.keys() - current.keys())
            if not changed and not created and not deleted:
                return None

            checkpoint_id = uuid4().hex
            folder = self.root / checkpoint_id
            backup = folder / "before"
            backup.mkdir(parents=True)
            entries = []
            diff: list[str] = []
            for relative in sorted(set(changed) | set(created) | set(deleted)):
                previous = snapshot.staging / "tree" / relative
                present = current.get(relative)
                status = "created" if relative in created else "deleted" if relative in deleted else "modified"
                entries.append({"path": relative, "status": status})
                if status != "created":
                    _clone(previous, backup / relative)
                try:
                    old_text = previous.read_text(encoding="utf-8") if status != "created" else ""
                    new_text = present.read_text(encoding="utf-8") if present is not None else ""
                    before_name = "/dev/null" if status == "created" else "a/" + relative
                    after_name = "/dev/null" if status == "deleted" else "b/" + relative
                    diff.extend(difflib.unified_diff(old_text.splitlines(keepends=True),
                                                     new_text.splitlines(keepends=True),
                                                     fromfile=before_name, tofile=after_name, n=3))
                except UnicodeDecodeError:
                    diff.extend([f"--- a/{relative}\n", f"+++ b/{relative}\n",
                                 "[binary file changed]\n"])
                if sum(map(len, diff)) > _MAX_DIFF:
                    diff.append("\n[diff truncated; checkpoint contains the complete changed files]\n")
                    break
            record = {"id": checkpoint_id, "chat_id": chat_id, "cwd": str(snapshot.root),
                      "created": datetime.now(timezone.utc).isoformat(), "files": entries}
            (folder / "checkpoint.json").write_text(json.dumps(record, indent=2), encoding="utf-8")
            return {**record, "diff": "".join(diff)}
        finally:
            shutil.rmtree(snapshot.staging, ignore_errors=True)

    @staticmethod
    def discard(snapshot: Snapshot | None) -> None:
        """Remove a staging snapshot when a turn is cancelled or aborted."""
        if snapshot is not None:
            shutil.rmtree(snapshot.staging, ignore_errors=True)

    def restore(self, checkpoint_id: str, working_folder: Path, *, chat_id: str) -> list[str]:
        """Restore files touched by this checkpoint without consulting or changing git."""
        if not checkpoint_id.isalnum() or len(checkpoint_id) != 32:
            raise ValueError("invalid checkpoint id")
        folder = self.root / checkpoint_id
        record_path = folder / "checkpoint.json"
        try:
            record = json.loads(record_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise FileNotFoundError("checkpoint not found") from exc
        root = Path(working_folder).resolve(strict=True)
        if record.get("id") != checkpoint_id or record.get("chat_id") != chat_id or record.get("cwd") != str(root):
            raise FileNotFoundError("checkpoint not found for this chat and folder")
        restored = []
        for entry in record.get("files", []):
            relative = PurePosixPath(entry.get("path", ""))
            if relative.is_absolute() or not relative.parts or any(part in ("", ".", "..", ".git") for part in relative.parts):
                raise ValueError("checkpoint contains an unsafe path")
            target = root.joinpath(*relative.parts)
            if not _inside(target.resolve(strict=False), root):
                raise ValueError("checkpoint path leaves the working folder")
            if entry.get("status") == "created":
                if target.is_file() or target.is_symlink():
                    target.unlink()
                parent = target.parent
                while parent != root:
                    try:
                        parent.rmdir()
                    except OSError:
                        break
                    parent = parent.parent
            else:
                source = folder / "before" / Path(*relative.parts)
                if not source.is_file():
                    raise FileNotFoundError(f"checkpoint content missing for {relative.as_posix()}")
                target.parent.mkdir(parents=True, exist_ok=True)
                temp = target.with_name(f".{target.name}.{uuid4().hex}.restore")
                shutil.copy2(source, temp)
                os.replace(temp, target)
            restored.append(relative.as_posix())
        return restored

    def list(self, chat_id: str) -> list[dict]:
        results = []
        for path in self.root.glob("*/checkpoint.json"):
            try:
                record = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if record.get("chat_id") == chat_id:
                results.append(record)
        return sorted(results, key=lambda item: item.get("created", ""), reverse=True)
