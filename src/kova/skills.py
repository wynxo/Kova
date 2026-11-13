"""Kova's skills: procedural memory the model writes and reuses.

A skill is one Markdown file with YAML frontmatter: a single-line
``description`` that always sits in the system prompt, and a body of full
instructions loaded only when the model asks for that skill. That is the same
shape Claude Code's ``SKILL.md`` and project instructions use, and it keeps a
long procedure nearly free until it is actually needed.

Kova writes the skills it learns into ``<root>/Kova``; anything the user drops
into ``<root>`` is read but never rewritten or deleted.
"""

from __future__ import annotations

import os
import re
import tempfile
import threading
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import yaml

from kova.memory import slugify

# Same front matter shape as memory notes: --- delimited YAML, BOM/CRLF tolerant.
_FRONTMATTER = re.compile(r"\A\ufeff?---\r?\n(.*?)\r?\n---[ \t]*(?:\r?\n|\Z)", re.DOTALL)

#: Kova's own skills live in this subfolder of the skills root.
WRITE_SUBDIR = "Kova"
#: One line, always in the prompt: long enough to route on, short enough to stay cheap.
MAX_DESCRIPTION_CHARS = 240
#: The whole skills block in one prompt, in characters.
MAX_PROMPT_CHARS = 4000
#: A skill name, and so its slug, never grows past this.
MAX_NAME_CHARS = 60


class SkillError(ValueError):
    """A skill write or load the store refuses; safe to show the model."""


@dataclass
class Skill:
    slug: str
    name: str
    description: str
    body: str
    updated: str
    path: Path

    @property
    def text(self) -> str:
        """The full instructions, as the model sees them when it loads the skill."""
        return f"# {self.name}\n{self.description}\n\n{self.body.strip()}".strip()


def _md_files(root: Path) -> list[Path]:
    """All .md files under root, recursive, skipping hidden dirs."""
    if not root.is_dir():
        return []
    files = [
        p
        for p in root.rglob("*.md")
        if not any(part.startswith(".") for part in p.relative_to(root).parts[:-1])
    ]
    return sorted(files)


def _headline(text: str) -> str:
    """The first real line of a body, used when a file has no description."""
    for line in text.splitlines():
        line = " ".join(line.split()).lstrip("#-* ").strip()
        if line:
            return line[:MAX_DESCRIPTION_CHARS]
    return ""


def _parse(path: Path) -> Skill:
    """Read one skill: YAML frontmatter when present, else a body-derived summary."""
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
            body = text[match.end():]
    headline = _headline(body)
    name = str(meta.get("name") or "").strip() or headline or path.stem
    description = " ".join(str(meta.get("description") or "").split()) or headline
    return Skill(
        slug=path.stem,
        name=name,
        description=description,
        body=body,
        updated=str(meta.get("updated") or _mtime_iso(path)),
        path=path,
    )


def _mtime_iso(path: Path) -> str:
    return datetime.fromtimestamp(path.stat().st_mtime).astimezone().isoformat()


_WRITE_LOCK = threading.RLock()


def _atomic_write(path: Path, text: str) -> None:
    """Write via a temp file + rename, so a crash never leaves half a skill."""
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".kova-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(text)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


class Skills:
    """Markdown skills under ``root``; Kova writes only inside ``write_subdir``."""

    def __init__(self, root: Path, write_subdir: str | None = WRITE_SUBDIR) -> None:
        self.root = Path(root)
        self.write_subdir = write_subdir

    @classmethod
    def for_home(cls, home: Path) -> Skills:
        """The skills folder of a Kova home, with Kova's own writes in a subfolder."""
        return cls(Path(home) / "skills", WRITE_SUBDIR)

    @property
    def write_dir(self) -> Path:
        return self.root / self.write_subdir if self.write_subdir else self.root

    def list(self) -> list[Skill]:
        """Every skill under root, recursive, hidden dirs skipped."""
        skills = []
        for path in _md_files(self.root):
            try:
                skills.append(_parse(path))
            except (OSError, UnicodeDecodeError):
                continue  # one broken file must not break every prompt
        return skills

    def get(self, name: str) -> Skill | None:
        """Find a skill by slug or name: the write dir first, then the rest of root."""
        slug = slugify(name)
        if not slug:
            return None
        direct = self.write_dir / f"{slug}.md"
        if direct.is_file():
            return _parse(direct)
        wanted = name.strip().casefold()
        for path in _md_files(self.root):
            if path.stem == slug:
                return _parse(path)
            try:
                skill = _parse(path)
            except (OSError, UnicodeDecodeError):
                continue
            if skill.name.casefold() == wanted:
                return skill
        return None

    def save(self, name: str, description: str, body: str) -> Skill:
        """Write one skill; the same name replaces the skill Kova wrote before."""
        name = " ".join((name or "").split())
        if not name:
            raise SkillError("A skill needs a name.")
        if len(name) > MAX_NAME_CHARS:
            raise SkillError(f"Skill names are at most {MAX_NAME_CHARS} characters; this one is {len(name)}.")
        description = " ".join((description or "").split())
        if not description:
            raise SkillError("A skill needs a one-line description: it is what you see before loading the skill.")
        if len(description) > MAX_DESCRIPTION_CHARS:
            raise SkillError(
                f"Keep the description to one line of at most {MAX_DESCRIPTION_CHARS} characters "
                f"(this one is {len(description)}); put the detail in the body."
            )
        body = (body or "").strip()
        if not body:
            raise SkillError("A skill needs a body: the full instructions you want to follow later.")
        path = self.write_dir / f"{slugify(name)}.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.is_symlink() or not path.parent.resolve().is_relative_to(self.root.resolve()):
            raise SkillError(f"Refusing to write a skill through a link: {path}")
        updated = datetime.now().astimezone().isoformat()
        meta = {"name": name, "description": description, "updated": updated}
        with _WRITE_LOCK:
            _atomic_write(path, "---\n" + yaml.safe_dump(meta, sort_keys=False) + "---\n" + body + "\n")
        return Skill(slug=path.stem, name=name, description=description, body=body,
                     updated=updated, path=path)

    def remove(self, name: str) -> bool:
        """Delete a skill; only ever one Kova itself wrote."""
        slug = slugify(name)
        path = self.write_dir / f"{slug}.md"
        if not slug or path.is_symlink() or not path.parent.resolve().is_relative_to(self.root.resolve()):
            return False
        if path.is_file():
            path.unlink()
            return True
        return False

    def prompt_block(self, limit: int = MAX_PROMPT_CHARS) -> str:
        """The always-visible part: every skill's name and description, never a body.

        The header always appears in a work-mode prompt, so the model knows it can
        write down a procedure it worked out and load one later.
        """
        head = ("Your skills (procedural memory you wrote yourself). Only these one-line summaries "
                "sit in the prompt: call skill_load with a skill's name to get its full instructions "
                "before following it, and skill_write to save a procedure you worked out.")
        skills = [skill for skill in self.list() if skill.description]
        if not skills:
            return f"{head}\n(none yet)"
        lines = []
        used = 0
        for index, skill in enumerate(skills):
            line = f"- {skill.name}: {skill.description}"
            if used + len(line) > limit:
                lines.append(f"({len(skills) - index} more skills are not shown; use skill_list to see them.)")
                break
            lines.append(line)
            used += len(line) + 1
        return head + "\n" + "\n".join(lines)
