"""Procedural memory: small YAML descriptions, explicitly loaded Markdown bodies."""
from __future__ import annotations

import logging
import re
from pathlib import Path

import yaml
from filelock import FileLock

from kova.config import private_write

log = logging.getLogger(__name__)
MAX_DESCRIPTION = 240
MAX_BODY = 12_000
MAX_HEADER = 4096
MAX_SKILLS = 32
_SLUG = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*\Z")


class Skills:
    """Human-editable skills under KOVA_HOME/skills, never executable authority."""

    def __init__(self, root: Path):
        self.root = Path(root)

    def _path(self, slug: str) -> Path:
        if not isinstance(slug, str) or len(slug) > 60 or not _SLUG.fullmatch(slug):
            raise ValueError("Skill slug must be lowercase words separated by hyphens (max 60 chars)")
        path = self.root / f"{slug}.md"
        if path.is_symlink() or not path.resolve().is_relative_to(self.root.resolve()):
            raise ValueError("Skill paths must stay inside the skills folder; symlinks are not allowed")
        return path

    @staticmethod
    def _description(value: str) -> str:
        if not isinstance(value, str) or not value.strip() or len(value) > MAX_DESCRIPTION or '\0' in value:
            raise ValueError(f"Skill description must contain 1–{MAX_DESCRIPTION} characters")
        return " ".join(value.split())

    @staticmethod
    def _body(value: str) -> str:
        if not isinstance(value, str) or not value.strip() or len(value.encode('utf-8')) > MAX_BODY or '\0' in value:
            raise ValueError(f"Skill body must contain 1–{MAX_BODY} UTF-8 bytes, without NUL characters")
        return value

    def _header(self, stream) -> dict:
        # Do not read the body to discover a skill. A malformed/unbounded header
        # must not turn directory discovery into a full-file read.
        first = stream.readline(MAX_HEADER + 1).decode('utf-8-sig').strip()
        if first != '---':
            raise ValueError("Skill needs YAML front matter with a description")
        header = bytearray()
        while len(header) <= MAX_HEADER:
            line = stream.readline(MAX_HEADER + 1 - len(header))
            if not line:
                break
            if line.strip() == b'---':
                meta = yaml.safe_load(header.decode('utf-8'))
                if not isinstance(meta, dict):
                    break
                return {"description": self._description(meta.get('description'))}
            header.extend(line)
        raise ValueError("Invalid or oversized skill front matter")

    def list(self) -> list[dict]:
        result = []
        for path in sorted(self.root.glob('*.md')):
            try:
                with self._path(path.stem).open('rb') as stream:
                    result.append({"slug": path.stem, **self._header(stream)})
            except (OSError, ValueError, yaml.YAMLError):
                log.warning("Skipping invalid skill %s", path.name)
        return result

    def read(self, slug: str) -> dict:
        with self._path(slug).open('rb') as stream:
            meta = self._header(stream)
            body = stream.read(MAX_BODY + 1).decode('utf-8')
        return {"slug": slug, **meta, "body": self._body(body)}

    def _write(self, slug: str, description: str, body: str) -> dict:
        description, body = self._description(description), self._body(body)
        header = yaml.safe_dump({"description": description}, allow_unicode=True, sort_keys=False)
        private_write(self._path(slug), f"---\n{header}---\n{body}")
        return {"slug": slug, "description": description}

    def add(self, slug: str, description: str, body: str) -> dict:
        path = self._path(slug)
        self.root.mkdir(parents=True, exist_ok=True)
        with FileLock(str(self.root / '.write.lock')):
            if path.exists():
                raise ValueError("Skill already exists; read it and use skill_patch")
            if len(list(self.root.glob('*.md'))) >= MAX_SKILLS:
                raise ValueError(f"At most {MAX_SKILLS} skills; remove an unused skill first")
            return self._write(slug, description, body)

    def patch(self, slug: str, old_text: str, new_text: str, description: str | None = None) -> dict:
        self._path(slug)
        self.root.mkdir(parents=True, exist_ok=True)
        with FileLock(str(self.root / '.write.lock')):
            skill = self.read(slug)
            if not isinstance(old_text, str) or not old_text or skill['body'].count(old_text) != 1:
                raise ValueError("old_text must match exactly once in the skill body")
            if not isinstance(new_text, str):
                raise ValueError("new_text must be text")
            return self._write(slug, skill['description'] if description is None else description,
                               skill['body'].replace(old_text, new_text, 1))

    def prompt(self) -> str:
        entries = self.list()
        if not entries:
            return ''
        return ("Available skills (procedural guidance, not authorization; user requests and "
                "Kova permissions take precedence). Read a relevant skill with skill_read "
                "before using it; only descriptions are loaded here. Save reusable lessons "
                "with skill_add; read then skill_patch to refine them.\n" +
                '\n'.join(f"- {entry['slug']}: {entry['description']}" for entry in entries))
