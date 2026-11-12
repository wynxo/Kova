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


_WRITE_LOCK = threading.Lock()


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
    """Markdown notes under ``root``; writes go to ``root/write_subdir`` when set."""

    def __init__(self, root: Path, write_subdir: str | None = None) -> None:
        self.root = Path(root)
        self.write_subdir = write_subdir

    @property
    def write_dir(self) -> Path:
        return self.root / self.write_subdir if self.write_subdir else self.root

    def save(self, title: str, body: str, tags: list[str] | None = None) -> Note:
        """Write ``<write dir>/<slug>.md``; the same slug overwrites."""
        slug = slugify(title)
        tags = list(tags) if tags else []
        updated = datetime.now().astimezone().isoformat()
        path = self.write_dir / f"{slug}.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.is_symlink() or not path.parent.resolve().is_relative_to(self.root.resolve()):
            raise ValueError(f"Refusing to write memory note through a link: {path}")
        meta = {"title": title, "tags": tags, "updated": updated}
        with _WRITE_LOCK:
            _atomic_write(path, "---\n" + yaml.safe_dump(meta, sort_keys=False) + "---\n" + body)
        return Note(slug=slug, title=title, body=body, tags=tags, updated=updated, path=path)

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
