"""Kova's Markdown memory: notes with YAML frontmatter under a root folder."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import yaml

_FRONTMATTER = re.compile(r"\A---\n(.*?)\n---\n", re.DOTALL)
_SAFE_SLUG = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9 _.-]*\Z")


def _safe(slug: str) -> bool:
    """A slug may never contain path separators or start with a dot."""
    return bool(_SAFE_SLUG.match(slug)) and ".." not in slug


def slugify(title: str) -> str:
    """Turn a title into a file slug: lowercase, dashed, max 60 chars."""
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
    slug = slug[:60].rstrip("-")
    return slug or "note"


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
        meta = {"title": title, "tags": tags, "updated": updated}
        path.write_text("---\n" + yaml.safe_dump(meta, sort_keys=False) + "---\n" + body)
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
        if path.is_file():
            path.unlink()
            return True
        return False

    def list(self) -> list[Note]:
        """Every note under root, recursive, hidden dirs skipped."""
        return [_parse(p) for p in _md_files(self.root)]

    def search(self, query: str, limit: int = 5) -> list[Note]:
        """Best-matching notes: title hit x3, tag hit x2, body hit x1 per query word."""
        words = [w for w in query.lower().split() if len(w) >= 2]
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
