"""What is installed: every skill with its origin, and edit/delete for the user.

The prompt sees the skills under ``KOVA_HOME/skills`` plus those of enabled
plugins (see :class:`kova.skills.Skills`). This module lists exactly those,
labelled by where they came from:

- ``learned`` — written by Kova itself (``skills/Kova/``)
- ``catalog`` — installed from the extensions catalog (``skills/catalog/``)
- ``yours`` — a file the user put in the skills folder
- ``plugin`` — shipped by an enabled plugin; read-only, it goes with the plugin

The user may view, edit and delete the first three. Plugin skills are changed
by disabling or removing their plugin.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import yaml

from kova import catalog, plugins
from kova.skills import (MAX_DESCRIPTION_CHARS, MAX_NAME_CHARS, WRITE_SUBDIR, Skill,
                         SkillError, _atomic_write, load_skills)

ORIGINS = ("learned", "catalog", "yours", "plugin")
_PLUGIN_PREFIX = "plugin:"


def _record(skill: Skill, *, origin: str, skill_id: str, plugin: str | None = None,
            version: str | None = None) -> dict:
    return {"id": skill_id, "name": skill.name, "description": skill.description,
            "body": skill.body, "updated": skill.updated, "origin": origin,
            "plugin": plugin, "version": version, "editable": origin != "plugin"}


def skills_root(home: Path) -> Path:
    return Path(home) / "skills"


def list_skills(home: Path) -> list[dict]:
    """Every skill the model can see, with its origin, sorted by origin then name."""
    home = Path(home)
    root = skills_root(home)
    versions = {name: info.get("version") for name, info in catalog.installed_skills(home).items()}
    rows = []
    for skill in load_skills([root]):
        relative = skill.path.relative_to(root)
        first = relative.parts[0] if len(relative.parts) > 1 else ""
        origin = {WRITE_SUBDIR: "learned", catalog.SKILL_SUBDIR: "catalog"}.get(first, "yours")
        version = versions.get(skill.path.stem) if origin == "catalog" else None
        rows.append(_record(skill, origin=origin, skill_id=relative.as_posix(), version=version))
    for plugin in plugins.enabled_plugins(home):
        base = plugin.root.resolve()
        for skill in load_skills(plugins._resolved_skill_paths(plugin)):
            relative = skill.path.resolve().relative_to(base).as_posix()
            rows.append(_record(skill, origin="plugin", plugin=plugin.name, version=plugin.version,
                                skill_id=f"{_PLUGIN_PREFIX}{plugin.name}/{relative}"))
    rows.sort(key=lambda row: (ORIGINS.index(row["origin"]), row["name"].casefold()))
    return rows


def get_skill(home: Path, skill_id: str) -> dict | None:
    return next((row for row in list_skills(home) if row["id"] == skill_id), None)


def _writable_path(home: Path, skill_id: str) -> Path:
    """The file behind an editable skill id, refusing plugins, links and escapes."""
    if skill_id.startswith(_PLUGIN_PREFIX):
        plugin = skill_id[len(_PLUGIN_PREFIX):].split("/", 1)[0]
        raise SkillError(
            f"This skill comes with the {plugin} plugin. Disable or remove the plugin instead.")
    root = skills_root(home).resolve()
    path = skills_root(home) / skill_id
    if (not skill_id.endswith(".md") or ".." in Path(skill_id).parts
            or Path(skill_id).is_absolute() or path.is_symlink()
            or not path.resolve().is_relative_to(root) or not path.is_file()):
        raise SkillError("No such skill.")
    return path


def update_skill(home: Path, skill_id: str, name: str, description: str, body: str) -> dict:
    """Rewrite an editable skill in place, keeping its file name."""
    path = _writable_path(home, skill_id)
    name = " ".join((name or "").split())
    description = " ".join((description or "").split())
    body = (body or "").strip()
    if not name:
        raise SkillError("A skill needs a name.")
    if len(name) > MAX_NAME_CHARS:
        raise SkillError(f"Skill names are at most {MAX_NAME_CHARS} characters.")
    if not description:
        raise SkillError("A skill needs a one-line description.")
    if len(description) > MAX_DESCRIPTION_CHARS:
        raise SkillError(f"Keep the description to {MAX_DESCRIPTION_CHARS} characters or fewer.")
    if not body:
        raise SkillError("A skill needs a body: the instructions themselves.")
    meta = {"name": name, "description": description,
            "updated": datetime.now().astimezone().isoformat()}
    _atomic_write(path, "---\n" + yaml.safe_dump(meta, sort_keys=False) + "---\n" + body + "\n")
    found = get_skill(home, skill_id)
    assert found is not None
    return found


def delete_skill(home: Path, skill_id: str) -> None:
    """Delete an editable skill; a catalog skill also loses its install record."""
    path = _writable_path(home, skill_id)
    path.unlink()
    relative = Path(skill_id)
    if len(relative.parts) == 2 and relative.parts[0] == catalog.SKILL_SUBDIR:
        catalog.remove_skill(path.stem, home)  # drop its install record too
