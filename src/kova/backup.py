"""Portable backup and restore: one zip with config, chats, notes and reminders.

Backups are deliberately boring: plain JSON, Markdown and TOML inside a zip,
so they can be read without Kova. API keys and the server token are never
written, and in Obsidian mode only Kova's own ``<vault>/Kova/`` notes travel.
"""

from __future__ import annotations

import json
import os
import re
import tomllib
import zipfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

import tomli_w

from kova import __version__
from kova.config import Config, ModelEntry, save_config
from kova.memory import Memory
from kova.store import Store

BACKUP_FORMAT = "kova-backup"
BACKUP_VERSION = 1
DEFAULT_PREFIX = "Kova-backup-"


class BackupError(RuntimeError):
    """A backup could not be written, read or restored."""


def default_export_path(now: datetime | None = None) -> Path:
    """``~/Kova-backup-YYYY-MM-DD.zip`` in the user's home folder."""
    day = (now or datetime.now()).strftime("%Y-%m-%d")
    return Path.home() / f"{DEFAULT_PREFIX}{day}.zip"


def _target_path(path: Path | str | None) -> Path:
    """Resolve the requested path, adding the default name or ``.zip``."""
    if path is None:
        return default_export_path()
    target = Path(path).expanduser()
    if target.is_dir():
        return target / default_export_path().name
    if not target.suffix:
        target = target.with_suffix(".zip")
    return target


def _safe_component(value: str) -> str:
    """A filename-safe version of a chat id (never a path)."""
    return re.sub(r"[^A-Za-z0-9_-]+", "_", value).strip("_") or "chat"


def _note_files(root: Path) -> list[Path]:
    """Markdown notes under ``root``, skipping hidden folders such as .obsidian."""
    if not root.is_dir():
        return []
    return sorted(
        path
        for path in root.rglob("*.md")
        if not any(part.startswith(".") for part in path.relative_to(root).parts[:-1])
    )


def _strip_api_keys(value):
    """Recursively drop every ``api_key`` key from parsed TOML."""
    if isinstance(value, dict):
        return {key: _strip_api_keys(item) for key, item in value.items() if key not in {"api_key", "auth", "headers"}}
    if isinstance(value, list):
        return [_strip_api_keys(item) for item in value]
    return value


def _config_bytes(cfg: Config) -> bytes:
    """config.toml with API keys removed, or one built from the loaded config."""
    path = cfg.home / "config.toml"
    if path.is_file():
        data = tomllib.loads(path.read_text())
    else:
        data = {
            "active": cfg.active,
            "port": cfg.port,
            "files_path": str(cfg.files_path),
            "memory": {"path": str(cfg.memory_path)},
            "models": [
                {
                    "name": model.name,
                    "base_url": model.base_url,
                    "model": model.model,
                    "context": model.context,
                }
                for model in cfg.models
            ],
        }
        if cfg.memory_write_subdir:
            data["memory"]["write_subdir"] = cfg.memory_write_subdir
    return tomli_w.dumps(_strip_api_keys(data)).encode("utf-8")


def render_chat_markdown(chat: dict) -> str:
    """One chat as readable Markdown, for backups and ``kova chats export --md``."""
    title = chat.get("title") or "Untitled chat"
    lines = [f"# {title}", ""]
    meta = [f"Chat `{chat.get('id', '')}`"]
    if chat.get("created"):
        meta.append(f"created {chat['created']}")
    lines.extend([" · ".join(meta), ""])
    wrote = False
    for message in chat.get("messages", []):
        role = message.get("role")
        content = message.get("content")
        if role not in ("user", "assistant") or not content:
            continue
        wrote = True
        lines.append("## You" if role == "user" else "## Kova")
        lines.append("")
        lines.append(content.rstrip())
        lines.append("")
    if not wrote:
        lines.extend(["_No messages yet._", ""])
    return "\n".join(lines).rstrip() + "\n"


def export_archive(
    cfg: Config,
    path: Path | str | None = None,
    *,
    store: Store | None = None,
) -> dict:
    """Write one zip backup and return counts plus its path.

    Chats are stored twice: ``chats/<id>.json`` for a faithful restore and
    ``chats/<id>.md`` so the archive is pleasant to read. Memory notes are
    copied as-is. The server token is never read or written.
    """
    target = _target_path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    store = store or Store(cfg.home / "kova.db")
    memory = Memory(cfg.memory_path, cfg.memory_write_subdir)
    chats = store.list_chats()
    reminders = store.list_reminders(include_done=True)
    notes = [
        (note.relative_to(memory.write_dir).as_posix(), note.read_bytes())
        for note in _note_files(memory.write_dir)
    ]

    created = datetime.now().astimezone().isoformat()
    manifest = {
        "format": BACKUP_FORMAT,
        "version": BACKUP_VERSION,
        "app_version": __version__,
        "created": created,
        "chats": len(chats),
        "notes": len(notes),
        "reminders": len(reminders),
        "api_keys": "omitted",
        "token": "omitted",
    }
    readme = (
        f"Kova backup — {created}\n\n"
        f"{len(chats)} chats, {len(notes)} memory notes, {len(reminders)} reminders.\n\n"
        "config.toml is included without API keys. Re-add a key with:\n"
        "  kova model add NAME URL MODEL --key KEY\n\n"
        "The server token file is never included.\n"
        "Restore with: kova import this-file.zip\n"
    )

    tmp = target.with_name(target.name + ".tmp")
    try:
        with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("README.txt", readme)
            archive.writestr("manifest.json", json.dumps(manifest, indent=2) + "\n")
            archive.writestr("config.toml", _config_bytes(cfg))
            for chat in chats:
                full = store.get_chat(chat["id"], include_created=True)
                if full is None:
                    continue
                base = f"chats/{_safe_component(chat['id'])}"
                archive.writestr(base + ".json", json.dumps(full, indent=2, ensure_ascii=False) + "\n")
                archive.writestr(base + ".md", render_chat_markdown(full))
            for name, data in notes:
                archive.writestr(f"memory/{name}", data)
            archive.writestr(
                "reminders.json",
                json.dumps(reminders, indent=2, ensure_ascii=False) + "\n",
            )
        os.replace(tmp, target)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    return {
        "path": str(target),
        "chats": len(chats),
        "notes": len(notes),
        "reminders": len(reminders),
        "created": created,
    }


def _read_manifest(archive: zipfile.ZipFile) -> dict:
    try:
        manifest = json.loads(archive.read("manifest.json"))
    except (KeyError, ValueError):
        return {}
    return manifest if isinstance(manifest, dict) else {}


def _chat_entries(names: list[str]) -> list[str]:
    return sorted(
        name
        for name in names
        if name.startswith("chats/") and name.endswith(".json") and name.count("/") == 1
    )


def _memory_relative(name: str) -> PurePosixPath | None:
    """The path below ``memory/``, or None when it is hidden or unsafe."""
    parts = PurePosixPath(name).parts
    if len(parts) < 2 or parts[0] != "memory":
        return None
    relative = parts[1:]
    if any(part in ("", ".", "..") or part.startswith(".") for part in relative):
        return None
    return PurePosixPath(*relative)


def _reminder_key(when: str, text: str) -> tuple:
    """Compare reminder times by instant, not by their ISO spelling."""
    try:
        stamp = datetime.fromisoformat(when)
    except (TypeError, ValueError):
        return (str(when), text)
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return (stamp.astimezone(timezone.utc), text)


def _clear_managed_notes(memory: Memory) -> None:
    """Delete only Kova's own Markdown files, never other vault notes."""
    for note in _note_files(memory.write_dir):
        note.unlink(missing_ok=True)


def _restore_models(cfg: Config, data: dict) -> bool:
    """Replace model entries from the backup, keeping matching local API keys."""
    from kova.config import ProviderEntry
    current = {model.name: model for model in cfg.models}
    existing = {p.id: p for p in cfg.providers}
    for raw in data.get('providers', []):
        if raw.get('id') not in existing:
            p = ProviderEntry(**{k: v for k, v in raw.items() if k not in ('api_key', 'auth', 'headers')})
            cfg.providers.append(p)
            existing[p.id] = p
    models: list[ModelEntry] = []
    for raw in data.get("models", []):
        if not isinstance(raw, dict) or not raw.get("name"):
            continue
        try:
            context = int(raw.get("context", 131072))
        except (TypeError, ValueError):
            context = 131072
        entry = ModelEntry(
            name=str(raw["name"]),
            base_url=str(raw.get("base_url", "")),
            model=str(raw.get("model", "")),
            api_key=None,
            context=context,
            provider=str(raw.get('provider', '')),
        )
        if entry.name in current:
            entry.api_key = current[entry.name].api_key
        models.append(entry)
    if not models:
        return False
    cfg.models[:] = models
    if data.get("active") in {model.name for model in models}:
        cfg.active = data["active"]
    save_config(cfg)
    return True


def import_archive(
    cfg: Config,
    path: Path | str,
    *,
    merge: bool = True,
    store: Store | None = None,
) -> dict:
    """Restore a backup. Merge keeps existing chats/notes/reminders.

    A replace clears chats, reminders and Kova's own memory notes first; it
    never touches notes outside ``<vault>/Kova/`` in Obsidian mode.
    """
    source = Path(path).expanduser()
    if not source.is_file():
        raise BackupError(f"No backup file at {source}.")
    try:
        archive = zipfile.ZipFile(source)
    except zipfile.BadZipFile as exc:
        raise BackupError(f"Not a zip file: {source}.") from exc

    with archive:
        names = archive.namelist()
        manifest = _read_manifest(archive)
        if manifest.get("format") not in (None, BACKUP_FORMAT):
            raise BackupError("That zip is not a Kova backup.")
        chat_names = _chat_entries(names)
        memory_names = [
            name for name in names
            if _memory_relative(name) is not None and name.endswith(".md")
        ]
        if not chat_names and not memory_names and "reminders.json" not in names:
            raise BackupError("That zip does not look like a Kova backup.")

        chats = []
        for name in chat_names:
            try:
                chat = json.loads(archive.read(name))
            except ValueError as exc:
                raise BackupError(f"Invalid chat data in {name}.") from exc
            if not isinstance(chat, dict):
                raise BackupError(f"Invalid chat data in {name}.")
            chats.append(chat)

        reminders = []
        if "reminders.json" in names:
            try:
                reminders = json.loads(archive.read("reminders.json"))
            except ValueError as exc:
                raise BackupError("Invalid reminders.json in the backup.") from exc
            if not isinstance(reminders, list):
                raise BackupError("Invalid reminders.json in the backup.")

        notes: list[tuple[PurePosixPath, bytes]] = []
        for name in memory_names:
            relative = _memory_relative(name)
            if relative is not None:
                notes.append((relative, archive.read(name)))

        config_data = None
        if not merge and "config.toml" in names:
            try:
                config_data = tomllib.loads(archive.read("config.toml").decode("utf-8"))
            except (UnicodeDecodeError, tomllib.TOMLDecodeError):
                config_data = None

    store = store or Store(cfg.home / "kova.db")
    if not merge:
        store.clear_chats()
        store.clear_reminders()

    chat_ids = {chat["id"] for chat in store.list_chats()} if merge else set()
    chats_imported = chats_skipped = 0
    for chat in chats:
        if merge and chat.get("id") in chat_ids:
            chats_skipped += 1
            continue
        try:
            imported = store.import_chat(chat)
        except (ValueError, TypeError) as exc:
            raise BackupError(f"Could not restore a chat: {exc}") from exc
        if imported:
            chats_imported += 1
            chat_ids.add(chat.get("id"))
        else:
            chats_skipped += 1

    # A restore must not reject a backup that predates or exceeds the budget:
    # it reproduces what was saved, and later writes enforce the cap again.
    memory = Memory(cfg.memory_path, cfg.memory_write_subdir, budget=0)
    memory.write_dir.mkdir(parents=True, exist_ok=True)
    if not merge:
        _clear_managed_notes(memory)
    existing_slugs = {note.stem for note in _note_files(memory.write_dir)}
    notes_imported = notes_skipped = 0
    root = memory.write_dir.resolve()
    for relative, data in notes:
        target = memory.write_dir / relative
        try:
            target.resolve().relative_to(root)
        except ValueError as exc:
            raise BackupError(f"Unsafe memory path in backup: {relative}") from exc
        if merge and (relative.stem in existing_slugs or target.exists()):
            notes_skipped += 1
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        existing_slugs.add(relative.stem)
        notes_imported += 1

    existing_reminders = set()
    if merge:
        existing_reminders = {
            _reminder_key(reminder["when"], reminder["text"])
            for reminder in store.list_reminders(include_done=True)
        }
    reminders_imported = reminders_skipped = 0
    for reminder in reminders:
        if not isinstance(reminder, dict) or not reminder.get("when") or not reminder.get("text"):
            continue
        key = _reminder_key(str(reminder["when"]), str(reminder["text"]))
        if merge and key in existing_reminders:
            reminders_skipped += 1
            continue
        try:
            store.import_reminder(str(reminder["when"]), str(reminder["text"]),
                                  bool(reminder.get("done")))
        except (TypeError, ValueError) as exc:
            raise BackupError(f"Could not restore a reminder: {exc}") from exc
        existing_reminders.add(key)
        reminders_imported += 1

    config_restored = False
    if config_data is not None:
        config_restored = _restore_models(cfg, config_data)

    return {
        "path": str(source),
        "chats": chats_imported,
        "chats_skipped": chats_skipped,
        "notes": notes_imported,
        "notes_skipped": notes_skipped,
        "reminders": reminders_imported,
        "reminders_skipped": reminders_skipped,
        "config_restored": config_restored,
    }
