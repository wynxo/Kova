"""Kova's Markdown memory: notes with YAML frontmatter under a root folder.

Notes carry provenance in their frontmatter: ``origin`` is ``user`` when the
fact was stated by the user (typed in the CLI, or written by a post-turn review
that could quote them) and ``model`` when the model volunteered or inferred it.
``source`` names the chat a fact came from and ``created`` when it was first
recorded, so every remembered fact can be inspected and traced back.

Recall is one small *index* note (``index.md``) plus topic notes loaded on
demand. :meth:`Memory.search` has two modes: ``keyword`` (the historic title ×3
/ tag ×2 / body ×1 word scoring) and ``hybrid``, which adds a deterministic
lexical signal. Kova has no embeddings provider and never calls one: the second
signal is an IDF-weighted token-overlap cosine over the same tokenizer, so
recall stays local, offline and reproducible. Rank order is
``keyword + HYBRID_WEIGHT × cosine``, newest ``updated`` breaking ties.
"""

from __future__ import annotations

import hashlib
import math
import os
import re
import tempfile
import threading
from collections import Counter
from collections.abc import Callable
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

#: Where a memory came from: stated by the user, or volunteered/inferred by the model.
ORIGINS = ("user", "model")

#: The always-loaded index note's slug, and how much of it may reach the prompt.
INDEX_SLUG = "index"
INDEX_MAX_LINES = 200

#: How much the lexical signal may move a hybrid ranking, relative to keyword score.
HYBRID_WEIGHT = 0.5


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
    #: ``user`` (stated by the user) or ``model`` (volunteered/inferred by the model).
    origin: str = "user"
    #: The chat this fact came from, when it was learned in one.
    source: str = ""
    #: When the fact was first recorded (``updated`` moves on every rewrite).
    created: str = ""


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
    # Unknown or missing origins fall back to "user": never claim a fact was
    # the model's own invention when it plainly was not.
    origin = str(meta.get("origin") or "user").strip().lower()
    if origin not in ORIGINS:
        origin = "user"
    raw_source = meta.get("source")
    raw_created = meta.get("created")
    return Note(
        slug=path.stem,
        title=str(meta.get("title") or path.stem),
        body=body,
        tags=tags,
        updated=str(meta.get("updated") or _mtime_iso(path)),
        path=path,
        origin=origin,
        source=str(raw_source) if raw_source else "",
        created=str(raw_created) if raw_created else "",
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


_WORD = re.compile(r"\w+", re.UNICODE)


def _tokens(text: str) -> list[str]:
    """Lowercase words of two or more characters: the unit both search signals count."""
    return [w for w in _WORD.findall(text.lower()) if len(w) >= 2]


def _idf(appearances: Counter, documents: int) -> Callable[[str], float]:
    """Smoothed inverse document frequency, always positive for a known token."""
    total = max(1, documents)

    def weight(token: str) -> float:
        return math.log((total + 1) / (appearances.get(token, 0) + 1)) + 1.0

    return weight


def _lexical(query_words: list[str], documents: list[list[str]],
             appearances: Counter) -> list[float]:
    """IDF-weighted token-overlap cosine between the query and each document.

    Kova ships no embeddings provider and must work offline, so the second
    ranking signal is deterministic and local: rare shared words count more
    than common ones, and long notes are divided by their own norm so a huge
    note cannot win on volume alone.
    """
    weight = _idf(appearances, len(documents))
    query = Counter(query_words)
    query_norm = math.sqrt(sum((count * weight(token)) ** 2 for token, count in query.items())) or 1.0
    scores: list[float] = []
    for tokens in documents:
        counts = Counter(tokens)
        document_norm = math.sqrt(
            sum((count * weight(token)) ** 2 for token, count in counts.items())) or 1.0
        shared = sum(query[token] * counts[token] * weight(token) ** 2
                     for token in query if token in counts)
        scores.append(shared / (query_norm * document_norm))
    return scores


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
                    *, refuse_existing: bool, origin: str = "user", source: str = "",
                    created: str = "") -> Note:
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
            meta = {"title": title, "tags": tags, "updated": updated,
                    "created": created or updated,
                    "origin": origin if origin in ORIGINS else "user"}
            if source:
                meta["source"] = source
            _atomic_write(path, "---\n" + yaml.safe_dump(meta, sort_keys=False) + "---\n" + body)
        return Note(slug=path.stem, title=title, body=body, tags=tags, updated=updated, path=path,
                    origin=origin if origin in ORIGINS else "user", source=source,
                    created=created or updated)

    def save(self, title: str, body: str, tags: list[str] | None = None, *,
             origin: str = "user", source: str = "") -> Note:
        """Write ``<write dir>/<slug>.md``; the same slug overwrites.

        This is the CLI/API "add" operation and keeps its historic upsert
        behaviour, so a person editing their own note can rewrite it wholesale.
        Model-facing writes use :meth:`add`/:meth:`patch`, which never replace a
        whole note by accident. ``origin`` records who the fact came from.
        """
        path = self.write_dir / f"{slugify(title)}.md"
        created = ""
        if path.is_file():
            try:
                created = _parse(path).created  # an edit keeps the original date
            except (OSError, UnicodeDecodeError):
                created = ""
        return self._write_note(path, title, body, list(tags) if tags else [],
                                refuse_existing=False, origin=origin, source=source, created=created)

    def add(self, title: str, body: str, tags: list[str] | None = None, *,
            origin: str = "model", source: str = "") -> Note:
        """Create a new note; refuse to replace one that already exists."""
        return self._write_note(self.write_dir / f"{slugify(title)}.md", title, body,
                                list(tags) if tags else [], refuse_existing=True,
                                origin=origin, source=source)

    def update(self, slug: str, *, title: str | None = None, body: str | None = None,
               tags: list[str] | None = None) -> Note:
        """Edit one note in place, keeping its slug, provenance and creation date.

        This is the memory browser's edit: a person deliberately rewriting their
        own note. Notes outside Kova's write folder are never touched.
        """
        if not _safe(slug):
            raise MemoryStoreError(f'No note named "{slug}".')
        path = self.write_dir / f"{slug}.md"
        if not path.is_file():
            if self.get(slug) is not None:
                raise MemoryStoreError(
                    f'Note "{slug}" is outside Kova\'s write folder, so it cannot be edited.'
                )
            raise MemoryStoreError(f'No note named "{slug}". Run memory_list to see the slugs.')
        note = _parse(path)
        return self._write_note(
            path,
            note.title if title is None else title,
            note.body if body is None else body,
            note.tags if tags is None else list(tags),
            refuse_existing=False,
            origin=note.origin,
            source=note.source,
            created=note.created,
        )

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
                                note.tags, refuse_existing=False, origin=note.origin,
                                source=note.source, created=note.created)

    def remove(self, slug: str) -> bool:
        """Delete one note from the write dir; see :meth:`delete`."""
        return self.delete(slug)

    def forget(self, slug: str) -> bool:
        """Delete one note; the user-facing name for :meth:`delete`."""
        return self.delete(slug)

    def restore(self, slug: str, text: str) -> bool:
        """Put one note's whole previous file text back, for undoing a change.

        Only Kova's own write folder is ever touched, and the text is written
        exactly as recorded — an undo never re-renders frontmatter.
        """
        if not _safe(slug):
            return False
        path = self.write_dir / f"{slug}.md"
        if path.is_symlink() or not path.parent.resolve().is_relative_to(self.root.resolve()):
            return False
        path.parent.mkdir(parents=True, exist_ok=True)
        with _WRITE_LOCK:
            _atomic_write(path, text)
        return True

    def index(self) -> Note | None:
        """The always-loaded index note, if the user keeps one."""
        return self.get(INDEX_SLUG)

    def index_text(self, max_lines: int = INDEX_MAX_LINES) -> str:
        """The index note's body, capped to ``max_lines`` so it stays cheap.

        A cap of zero (or less) disables the index; over-long indexes are cut
        with a marker the model can act on, never silently dropped.
        """
        if max_lines <= 0:
            return ""
        note = self.index()
        if note is None:
            return ""
        lines = note.body.splitlines()
        if len(lines) <= max_lines:
            return note.body.strip()
        kept = "\n".join(lines[:max_lines]).rstrip()
        return f"{kept}\n… index truncated ({len(lines) - max_lines} more lines; open index for the rest)"

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

    def search(self, query: str, limit: int = 5, *, mode: str = "keyword") -> list[Note]:
        """Best-matching notes, as a list of at most ``limit`` notes.

        Every mode starts from the historic keyword score: title hit ×3, tag hit
        ×2, body hit ×1 for each query word. ``mode="keyword"`` ranks on that
        score alone (newest ``updated`` first among equals). ``mode="hybrid"``
        adds the deterministic lexical signal from :func:`_lexical`, so two
        notes that score alike on words are ordered by how much *rare* wording
        they share with the query. Only notes with a keyword hit are returned:
        the lexical signal reorders recall, it never invents a candidate, so a
        query that matches nothing still returns nothing.
        """
        # Words only, so punctuation ("dog's", "Rex!") never hides a match.
        words = [w for w in re.findall(r"\w+", query.lower()) if len(w) >= 2]
        if not words:
            return []
        notes = self.list()
        hits: list[tuple[int, Note]] = []
        for note in notes:
            title = note.title.lower()
            tag_list = [t.lower() for t in note.tags]
            body = note.body.lower()
            score = 0
            for word in words:
                score += 3 * title.count(word)
                score += 2 * sum(t.count(word) for t in tag_list)
                score += body.count(word)
            if score:
                hits.append((score, note))
        if mode != "hybrid":
            hits.sort(key=lambda sn: (sn[0], sn[1].updated), reverse=True)
            return [note for _, note in hits[:limit]]
        documents = [_tokens(f"{note.title} {' '.join(note.tags)} {note.body}")
                     for _, note in hits]
        appearances: Counter = Counter(token for tokens in documents for token in set(tokens))
        lexical = _lexical(words, documents, appearances)
        ranked = [(score + HYBRID_WEIGHT * similarity, note)
                  for (score, note), similarity in zip(hits, lexical)]
        ranked.sort(key=lambda sn: (sn[0], sn[1].updated), reverse=True)
        return [note for _, note in ranked[:limit]]
