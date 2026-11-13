"""Kova's Markdown memory: notes with YAML frontmatter under a root folder."""

from __future__ import annotations

import hashlib
import os
import re
import tempfile
import threading
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import yaml

# Tolerates a UTF-8 BOM, CRLF line ends, and a note that ends right after its frontmatter.
_FRONTMATTER = re.compile(r"\A\ufeff?---\r?\n(.*?)\r?\n---[ \t]*(?:\r?\n|\Z)", re.DOTALL)
_SAFE_SLUG = re.compile(r"\A\w[\w .-]*\Z")

#: Default cap on the whole memory store, in characters. Small enough to stay
#: cheap in the prompt, large enough for a useful set of facts (~2k tokens).
DEFAULT_MEMORY_BUDGET = 8000


class MemoryStoreError(ValueError):
    """A memory write the store refuses; the message is safe to show the model."""


class MemoryExistsError(MemoryStoreError):
    """An ``add`` would overwrite an existing note."""


class MemoryBudgetError(MemoryStoreError):
    """A write would push the store over its character budget."""

    def __init__(self, overflow: int, used: int, budget: int) -> None:
        self.overflow = overflow
        self.used = used
        self.budget = budget
        super().__init__(
            f"Memory over budget by {overflow} characters: {used} used, {budget} allowed. "
            "Remove or shorten an existing note, then retry."
        )


def note_chars(title: str, body: str, tags: list[str] | None = None) -> int:
    """What one note costs against the budget: title, tags and body text."""
    return len(title) + sum(len(tag) for tag in tags or []) + len(body)


def _safe(slug: str) -> bool:
    """A slug may never contain path separators or start with a dot."""
    return bool(_SAFE_SLUG.match(slug)) and ".." not in slug


def slugify(title: str) -> str:
    """Turn a title into a file slug: lowercase, dashed, max 60 chars."""
    slug = re.sub(r"[^\w]+|_", "-", title.lower()).strip("-")
    slug = slug[:60].rstrip("-")
    if slug:
        return slug
    # Titles with no letters or digits (e.g. only emoji) get a stable, distinct
    # slug, so two such notes never overwrite each other.
    digest = hashlib.sha1(title.encode("utf-8")).hexdigest()[:8] if title.strip() else ""
    return f"note-{digest}" if digest else "note"


@dataclass
class Note:
    slug: str
    title: str
    body: str
    tags: list[str]
    updated: str
    path: Path


def _md_files(root: Path) -> list[Path]:
    """All .md files under root, recursive, skipping hidden dirs (.obsidian etc.)."""
    if not root.is_dir():
        return []
    files = [
        p
        for p in root.rglob("*.md")
        if not any(part.startswith(".") for part in p.relative_to(root).parts[:-1])
    ]
    return sorted(files)


def _parse(path: Path) -> Note:
    """Read one note: YAML frontmatter if present, else the file stem as title."""
    text = path.read_text()
    meta: dict = {}
    body = text
    match = _FRONTMATTER.match(text)
    if match:
        try:
            loaded = yaml.safe_load(match.group(1))
        except yaml.YAMLError:
            loaded = None
        if isinstance(loaded, dict):
            meta = loaded
            body = text[match.end() :]
    raw_tags = meta.get("tags")
    if isinstance(raw_tags, list):
        tags = [str(t) for t in raw_tags]
    elif isinstance(raw_tags, str) and raw_tags:
        tags = [raw_tags]
    else:
        tags = []
    return Note(
        slug=path.stem,
        title=str(meta.get("title") or path.stem),
        body=body,
        tags=tags,
        updated=str(meta.get("updated") or _mtime_iso(path)),
        path=path,
    )


def _mtime_iso(path: Path) -> str:
    return datetime.fromtimestamp(path.stat().st_mtime).astimezone().isoformat()


# Re-entrant: patch reads and then writes under the same guard.
_WRITE_LOCK = threading.RLock()


def _atomic_write(path: Path, text: str) -> None:
    """Write via a temp file + rename, so a crash or race never leaves half a note."""
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".kova-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(text)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


class Memory:
    """Markdown notes under ``root``; writes go to ``root/write_subdir`` when set.

    ``budget`` caps the whole store in characters (title + tags + body). A
    write that would exceed it raises :class:`MemoryBudgetError` with the exact
    overflow; nothing is ever silently truncated. ``None`` or ``0`` disables
    the cap, which backup restores use.
    """

    def __init__(self, root: Path, write_subdir: str | None = None,
                 budget: int | None = DEFAULT_MEMORY_BUDGET) -> None:
        self.root = Path(root)
        self.write_subdir = write_subdir
        self.budget = budget or 0

    @property
    def write_dir(self) -> Path:
        return self.root / self.write_subdir if self.write_subdir else self.root

    def usage(self) -> int:
        """Characters currently used by every note, against :attr:`budget`."""
        return sum(note_chars(note.title, note.body, note.tags) for note in self.list())

    def stats(self) -> dict:
        """Usage and capacity, for the prompt header and the list tool."""
        notes = self.list()
        used = sum(note_chars(note.title, note.body, note.tags) for note in notes)
        budget = self.budget
        return {
            "used": used,
            "budget": budget,
            "percent": round(used * 100 / budget) if budget else 0,
            "notes": len(notes),
        }

    def _enforce_budget(self, used: int, old_chars: int, new_chars: int) -> None:
        """Refuse a write whose result would exceed the budget."""
        total = used - old_chars + new_chars
        if self.budget and total > self.budget:
            raise MemoryBudgetError(total - self.budget, used, self.budget)

    def _write_note(self, path: Path, title: str, body: str, tags: list[str],
                    *, refuse_existing: bool) -> Note:
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.is_symlink() or not path.parent.resolve().is_relative_to(self.root.resolve()):
            raise MemoryStoreError(f"Refusing to write memory note through a link: {path}")
        with _WRITE_LOCK:
            if refuse_existing and path.is_file():
                raise MemoryExistsError(
                    f'A memory note named "{path.stem}" already exists. '
                    "Use memory_patch for a targeted change instead of replacing it."
                )
            try:
                existing = _parse(path) if path.is_file() else None
            except (OSError, UnicodeDecodeError):
                existing = None  # a broken note is replaced, not counted twice
            self._enforce_budget(
                self.usage(),
                note_chars(existing.title, existing.body, existing.tags) if existing else 0,
                note_chars(title, body, tags),
            )
            updated = datetime.now().astimezone().isoformat()
            meta = {"title": title, "tags": tags, "updated": updated}
            _atomic_write(path, "---\n" + yaml.safe_dump(meta, sort_keys=False) + "---\n" + body)
        return Note(slug=path.stem, title=title, body=body, tags=tags, updated=updated, path=path)

    def save(self, title: str, body: str, tags: list[str] | None = None) -> Note:
        """Write ``<write dir>/<slug>.md``; the same slug overwrites.

        This is the CLI/API "add" operation and keeps its historic upsert
        behaviour. Model-facing writes use :meth:`add`/:meth:`patch`, which
        never replace a whole note by accident.
        """
        return self._write_note(self.write_dir / f"{slugify(title)}.md", title, body,
                                list(tags) if tags else [], refuse_existing=False)

    def add(self, title: str, body: str, tags: list[str] | None = None) -> Note:
        """Create a new note; refuse to replace one that already exists."""
        return self._write_note(self.write_dir / f"{slugify(title)}.md", title, body,
                                list(tags) if tags else [], refuse_existing=True)

    def patch(self, slug: str, old_text: str, new_text: str) -> Note:
        """Replace one span inside one note; ``old_text`` must match exactly once.

        Patching is deliberately narrow: it never replaces a whole entry, and it
        only touches notes Kova itself wrote (inside the write dir).
        """
        if not old_text:
            raise MemoryStoreError("old_text must not be empty; use memory_remove to delete a note.")
        if not _safe(slug):
            raise MemoryStoreError(f'No note named "{slug}".')
        path = self.write_dir / f"{slug}.md"
        if not path.is_file():
            if self.get(slug) is not None:
                raise MemoryStoreError(
                    f'Note "{slug}" is outside Kova\'s write folder, so it cannot be patched.'
                )
            raise MemoryStoreError(f'No note named "{slug}". Run memory_list to see the slugs.')
        note = _parse(path)
        count = note.body.count(old_text)
        if count == 0:
            raise MemoryStoreError(
                f'old_text was not found in "{slug}". Copy the text exactly, or add a new note.'
            )
        if count > 1:
            raise MemoryStoreError(
                f'old_text matches {count} times in "{slug}"; include more surrounding text '
                "so it matches exactly once."
            )
        return self._write_note(path, note.title, note.body.replace(old_text, new_text, 1),
                                note.tags, refuse_existing=False)

    def remove(self, slug: str) -> bool:
        """Delete one note from the write dir; see :meth:`delete`."""
        return self.delete(slug)

    def get(self, slug: str) -> Note | None:
        """Find a note: the write dir first, then the rest of root."""
        if not _safe(slug):
            return None
        direct = self.write_dir / f"{slug}.md"
        if direct.is_file():
            return _parse(direct)
        for candidate in _md_files(self.root):
            if candidate.stem == slug:
                return _parse(candidate)
        return None

    def delete(self, slug: str) -> bool:
        """Delete a note; only ever inside the write dir."""
        if not _safe(slug):
            return False
        path = self.write_dir / f"{slug}.md"
        if path.is_symlink() or not path.parent.resolve().is_relative_to(self.root.resolve()):
            return False
        if path.is_file():
            path.unlink()
            return True
        return False

    def list(self) -> list[Note]:
        """Every note under root, recursive, hidden dirs skipped."""
        notes = []
        for p in _md_files(self.root):
            try:
                notes.append(_parse(p))
            except (OSError, UnicodeDecodeError):
                continue  # one broken note must not break every chat
        return notes

    def search(self, query: str, limit: int = 5) -> list[Note]:
        """Best-matching notes: title hit x3, tag hit x2, body hit x1 per query word."""
        # Words only, so punctuation ("dog's", "Rex!") never hides a match.
        words = [w for w in re.findall(r"\w+", query.lower()) if len(w) >= 2]
        if not words:
            return []
        scored: list[tuple[int, Note]] = []
        for note in self.list():
            title = note.title.lower()
            tag_list = [t.lower() for t in note.tags]
            body = note.body.lower()
            score = 0
            for word in words:
                score += 3 * title.count(word)
                score += 2 * sum(t.count(word) for t in tag_list)
                score += body.count(word)
            if score:
                scored.append((score, note))
        scored.sort(key=lambda sn: (sn[0], sn[1].updated), reverse=True)
        return [note for _, note in scored[:limit]]
