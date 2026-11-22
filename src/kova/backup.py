"""Portable backup and restore: one zip with config, projects, chats, notes and reminders.

Backups are deliberately boring: plain JSON, Markdown and TOML inside a zip,
so they can be read without Kova. API keys and the server token are never
written, and in Obsidian mode only Kova's own ``<vault>/Kova/`` notes travel.
Project folders are written relative to the home folder, so an archive never
spells out the account it came from.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
import tomllib
import zipfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from uuid import uuid4

import tomli_w

from kova import __version__
from kova.config import Config, ModelEntry, save_config
from kova.memory import Memory
from kova.store import Store

BACKUP_FORMAT = "kova-backup"
BACKUP_VERSION = 2
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


def _home_folder() -> Path:
    """The home folder project folders are written relative to (a test seam)."""
    return Path.home()


def _portable_path(value: str | None, home: Path | None = None) -> str | None:
    """A project folder as ``~/...`` when it sits below the home folder.

    Archives are portable and meant to be shareable, so they never spell out
    the account name. A folder elsewhere is kept as it is: it names no account.
    """
    if not isinstance(value, str) or not value.strip() or "\0" in value:
        return None
    try:
        path = Path(value).expanduser()
    except RuntimeError:
        return None
    if not path.is_absolute():
        return None
    try:
        relative = path.resolve().relative_to((home or _home_folder()).resolve())
    except (OSError, RuntimeError, ValueError):
        return str(path)
    return "~/" + relative.as_posix() if relative.parts else "~"


def _local_folder(value: str | None, home: Path | None = None) -> str | None:
    """An existing folder on this machine from a backup, or ``None``.

    A restore never creates a folder and never adopts one that is not there:
    that keeps a project's default from pointing at a path from another
    machine, and new chats fall back to Kova Files instead of failing.
    """
    if not isinstance(value, str) or not value.strip() or "\0" in value:
        return None
    text = value
    base = home or _home_folder()
    if text == "~" or text.startswith("~/"):
        text = str(base) + text[1:]
    path = Path(text)
    if not path.is_absolute():
        return None
    try:
        path = path.resolve(strict=True)
    except (OSError, RuntimeError, ValueError):
        return None
    return str(path) if path.is_dir() else None


def _project_entry(project: dict) -> dict:
    """One project as the archive stores it.

    Pinned files travel as references only. The files themselves are not
    copied, so a backup never grows by the project's working folder.
    """
    return {
        "id": project.get("id"),
        "name": project.get("name"),
        "instructions": project.get("instructions") or "",
        "cwd": _portable_path(project.get("cwd")),
        "created": project.get("created"),
        "updated": project.get("updated"),
        "colour": project.get("colour"),
        "icon": project.get("icon"),
        "model": project.get("model"),
        "files": [path for path in project.get("files") or [] if isinstance(path, str)],
        "memory_scope": project.get("memory_scope") is True,
    }


def _note_files(root: Path) -> list[Path]:
    """Markdown notes under ``root``, skipping hidden folders such as .obsidian."""
    if not root.is_dir():
        return []
    return sorted(
        path
        for path in root.rglob("*.md")
        if not path.is_symlink()
        and path.is_file()
        and path.resolve().is_relative_to(root.resolve())
        and not any(part.startswith(".") for part in path.relative_to(root).parts[:-1])
    )


def _strip_api_keys(value):
    """Recursively drop credentials (keys, auth, headers, MCP env) from parsed TOML."""
    if isinstance(value, dict):
        return {key: _strip_api_keys(item) for key, item in value.items()
                if key not in {"api_key", "auth", "headers", "env"}}
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
                    **({"reasoning_effort": model.reasoning_effort} if model.reasoning_effort else {}),
                    **({"reasoning_efforts": model.reasoning_efforts} if model.reasoning_efforts else {}),
                }
                for model in cfg.models
            ],
        }
        if cfg.memory_write_subdir:
            data["memory"]["write_subdir"] = cfg.memory_write_subdir
    return tomli_w.dumps(_strip_api_keys(data)).encode("utf-8")


def render_chat_markdown(chat: dict) -> str:
    """One chat as readable Markdown, for backups and ``kova chats export --md``."""
    if chat.get("ephemeral"):
        raise BackupError("Incognito chats cannot be exported.")
    title = chat.get("title") or "Untitled chat"
    lines = [f"# {title}", ""]
    meta = [f"Chat `{chat.get('id', '')}`"]
    if chat.get("created"):
        meta.append(f"created {chat['created']}")
    lines.extend([" · ".join(meta), ""])
    wrote = False
    for message in chat.get("messages", []):
        if message.get("active") is False:
            continue
        role = message.get("role")
        content = message.get("content")
        names = [row.get("name") for row in message.get("attachments") or []
                 if isinstance(row, dict) and isinstance(row.get("name"), str) and row["name"]]
        if role not in ("user", "assistant") or (not content and not names):
            continue
        wrote = True
        lines.append("## You" if role == "user" else "## Kova")
        lines.append("")
        lines.append(content.rstrip() if content else "_Attached: " + ", ".join(names) + "_")
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

    Projects are ``projects.json``, with their folders relative to the home
    folder, pinned files as relative references only and the memory flag. Chats
    are stored twice: ``chats/<id>.json`` for a faithful restore and
    ``chats/<id>.md`` so the archive is pleasant to read. Global memory notes
    are copied as-is, and each project's own notes travel under
    ``project-memory/<project-id>/``. Bots travel as ``bots/<id>/`` (each
    bot's ``bot.toml``, ``persona.md`` and avatar image) with their private
    notes under ``bot-memory/<id>/``. The server token is never read or written.
    """
    target = _target_path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    store = store or Store(cfg.home / "kova.db")
    memory = Memory(cfg.memory_path, cfg.memory_write_subdir)
    projects = store.list_projects()
    chats = store.list_chats()
    reminders = store.list_reminders(include_done=True)
    notes = [
        (note.relative_to(memory.write_dir).as_posix(), note.read_bytes())
        for note in _note_files(memory.write_dir)
    ]
    project_notes: list[tuple[str, bytes]] = []
    for project in projects:
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", str(project.get("id") or "")):
            continue
        try:
            scope = memory.project_scope(project["id"])
        except ValueError:
            continue
        for note in _note_files(scope.write_dir):
            project_notes.append(
                (f'{project["id"]}/{note.relative_to(scope.write_dir).as_posix()}',
                 note.read_bytes()))
    bot_files = _bot_export_files(cfg.home)
    bot_ids = sorted({member.split("/")[1] for member, _ in bot_files})
    bot_notes: list[tuple[str, bytes]] = []
    try:
        bot_scope_names = sorted(path.name for path in memory.bot_scopes_dir.iterdir())
    except OSError:
        bot_scope_names = []
    for bot_id in bot_scope_names:
        if not _BOT_SCOPE_ID.match(bot_id):
            continue
        try:
            scope = memory.bot_scope(bot_id)
        except ValueError:
            continue
        for note in _note_files(scope.write_dir):
            bot_notes.append(
                (f'{bot_id}/{note.relative_to(scope.write_dir).as_posix()}',
                 note.read_bytes()))

    created = datetime.now().astimezone().isoformat()
    manifest = {
        "format": BACKUP_FORMAT,
        "version": BACKUP_VERSION,
        "app_version": __version__,
        "created": created,
        "projects": len(projects),
        "chats": len(chats),
        "notes": len(notes),
        "project_notes": len(project_notes),
        "bots": len(bot_ids),
        "bot_notes": len(bot_notes),
        "reminders": len(reminders),
        "api_keys": "omitted",
        "token": "omitted",
    }
    readme = (
        f"Kova backup — {created}\n\n"
        f"{len(chats)} chats, {len(projects)} projects, {len(notes)} memory notes, "
        f"{len(project_notes)} project memory notes, {len(bot_ids)} bots, "
        f"{len(bot_notes)} bot memory notes, {len(reminders)} reminders.\n\n"
        "config.toml is included without API keys. Re-add a key with:\n"
        "  kova model add NAME URL MODEL --key KEY\n\n"
        "Project folders are written relative to your home folder (~/...).\n"
        "The server token file is never included.\n"
        "Restore with: kova import this-file.zip\n"
    )

    # Unique, owner-only staging avoids exposing chats or following a planted symlink.
    fd, name = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp", dir=target.parent)
    os.close(fd)
    tmp = Path(name)
    try:
        with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("README.txt", readme)
            archive.writestr("manifest.json", json.dumps(manifest, indent=2) + "\n")
            archive.writestr("config.toml", _config_bytes(cfg))
            archive.writestr(
                "projects.json",
                json.dumps([_project_entry(project) for project in projects],
                           indent=2, ensure_ascii=False) + "\n",
            )
            for chat in chats:
                full = store.get_chat(chat["id"], include_created=True, include_inactive=True)
                if full is None:
                    continue
                base = f"chats/{_safe_component(chat['id'])}"
                archive.writestr(base + ".json", json.dumps(full, indent=2, ensure_ascii=False) + "\n")
                archive.writestr(base + ".md", render_chat_markdown(full))
            for name, data in notes:
                archive.writestr(f"memory/{name}", data)
            for name, data in project_notes:
                archive.writestr(f"project-memory/{name}", data)
            for member, data in bot_files:
                archive.writestr(member, data)
            for name, data in bot_notes:
                archive.writestr(f"bot-memory/{name}", data)
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
        "projects": len(projects),
        "chats": len(chats),
        "notes": len(notes),
        "project_notes": len(project_notes),
        "bots": len(bot_ids),
        "bot_notes": len(bot_notes),
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


def _project_memory_relative(name: str) -> tuple[str, PurePosixPath] | None:
    """A ``project-memory/<project-id>/<note>`` entry as (id, relative note path)."""
    parts = PurePosixPath(name).parts
    if len(parts) < 3 or parts[0] != "project-memory" or not name.endswith(".md"):
        return None
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", parts[1]):
        return None
    relative = parts[2:]
    if any(part in ("", ".", "..") or part.startswith(".") for part in relative):
        return None
    return parts[1], PurePosixPath(*relative)


#: Bot files the archive may carry: the definition, the persona and images.
_BOT_DIRECT = {"bot.toml", "persona.md"}
_BOT_IMAGE_SUFFIXES = (".png", ".jpg", ".jpeg", ".webp")
_BOT_PLAIN = re.compile(r"\A[A-Za-z0-9_][A-Za-z0-9_.-]*\Z")
_BOT_SCOPE_ID = re.compile(r"\A[A-Za-z0-9_-]{1,64}\Z")


def _archive_has_traversal(name: str) -> bool:
    """Whether a zip member escapes its folder (``..``, absolute or NUL)."""
    if not isinstance(name, str) or not name or "\0" in name or "\\" in name:
        return True
    path = PurePosixPath(name)
    if path.is_absolute():
        return True
    return any(part in ("", ".", "..") for part in path.parts)


def _bot_file_relative(name: str) -> tuple[str, str] | None:
    """A ``bots/<id>/<file>`` entry as (id, file), or None when not carried.

    Only the definition, the persona and plain image names travel. Anything
    else under ``bots/`` is ignored on import, so a hand-edited archive never
    smuggles an unexpected file into a bot folder.
    """
    parts = PurePosixPath(name).parts
    if len(parts) != 3 or parts[0] != "bots":
        return None
    bot_id, filename = parts[1], parts[2]
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,31}", bot_id):
        return None
    if filename in _BOT_DIRECT:
        return bot_id, filename
    if not _BOT_PLAIN.match(filename) or filename.startswith("."):
        return None
    if filename.lower().endswith(_BOT_IMAGE_SUFFIXES):
        return bot_id, filename
    return None


def _bot_memory_relative(name: str) -> tuple[str, PurePosixPath] | None:
    """A ``bot-memory/<bot-id>/<note>`` entry as (id, relative note path)."""
    parts = PurePosixPath(name).parts
    if len(parts) < 3 or parts[0] != "bot-memory" or not name.endswith(".md"):
        return None
    if not _BOT_SCOPE_ID.match(parts[1]):
        return None
    relative = parts[2:]
    if any(part in ("", ".", "..") or part.startswith(".") for part in relative):
        return None
    return parts[1], PurePosixPath(*relative)


def _manifest_time(manifest: dict) -> datetime | None:
    """The backup's creation instant, or None when it cannot be told."""
    created = manifest.get("created") if isinstance(manifest, dict) else None
    if not isinstance(created, str) or not created.strip():
        return None
    try:
        stamp = datetime.fromisoformat(created)
    except ValueError:
        return None
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return stamp


def _archived_handle(data: bytes, fallback: str) -> str:
    """The handle a backed-up ``bot.toml`` claims, lower-cased."""
    try:
        parsed = tomllib.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError, ValueError):
        return fallback.casefold()
    if not isinstance(parsed, dict):
        return fallback.casefold()
    handle = parsed.get("handle")
    if isinstance(handle, str) and handle.strip():
        return handle.strip().casefold()
    bot_id = parsed.get("id")
    if isinstance(bot_id, str) and bot_id.strip():
        return bot_id.strip().casefold()
    return fallback.casefold()


def _local_bot_handles(home: Path) -> dict[str, str]:
    """Every local bot id mapped to its handle (the folder name on failure)."""
    root = Path(home) / "bots"
    handles: dict[str, str] = {}
    try:
        children = sorted(root.iterdir(), key=lambda path: path.name)
    except OSError:
        return handles
    for child in children:
        if child.name.startswith(".") or not child.is_dir() or child.is_symlink():
            continue
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,31}", child.name):
            continue
        handle = child.name.casefold()
        definition = child / "bot.toml"
        if definition.is_file() and not definition.is_symlink():
            try:
                parsed = tomllib.loads(definition.read_bytes().decode("utf-8"))
            except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError, ValueError):
                parsed = None
            if isinstance(parsed, dict):
                raw = parsed.get("handle")
                if isinstance(raw, str) and raw.strip():
                    handle = raw.strip().casefold()
                elif isinstance(parsed.get("id"), str) and parsed["id"].strip():
                    handle = parsed["id"].strip().casefold()
        handles[child.name] = handle
    return handles


def _private_chmod(path: Path) -> None:
    """Restored bot files stay readable only by their owner on POSIX."""
    if os.name == "nt":
        return
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def _bot_export_files(home: Path) -> list[tuple[str, bytes]]:
    """Every bot file the archive carries as (member name, bytes).

    Only ``bot.toml``, ``persona.md`` and plain image files travel, never a
    link and never a subfolder, so the archive cannot smuggle one in.
    """
    members: list[tuple[str, bytes]] = []
    root = Path(home) / "bots"
    try:
        children = sorted(root.iterdir(), key=lambda path: path.name)
    except OSError:
        return members
    for child in children:
        if child.name.startswith(".") or not child.is_dir() or child.is_symlink():
            continue
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,31}", child.name):
            continue
        try:
            files = sorted(child.iterdir(), key=lambda path: path.name)
        except OSError:
            continue
        for path in files:
            if not path.is_file() or path.is_symlink() or path.name.startswith("."):
                continue
            if len(path.parts) != len(child.parts) + 1:
                continue
            if path.name in _BOT_DIRECT or (
                _BOT_PLAIN.match(path.name) and path.name.lower().endswith(_BOT_IMAGE_SUFFIXES)
            ):
                try:
                    members.append((f"bots/{child.name}/{path.name}", path.read_bytes()))
                except OSError:
                    continue
    return members


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


def _clear_bots(home: Path) -> None:
    """Remove every local bot folder for a replacing restore.

    Links are unlinked, never followed, so a planted link cannot delete
    outside the bot folder.
    """
    root = Path(home) / "bots"
    try:
        children = sorted(root.iterdir(), key=lambda path: path.name)
    except OSError:
        return
    for child in children:
        if child.name.startswith(".") or not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,31}", child.name):
            continue
        try:
            if child.is_symlink() or child.is_file():
                child.unlink(missing_ok=True)
            elif child.is_dir():
                for sub in sorted(child.rglob("*"), key=lambda path: len(path.parts), reverse=True):
                    try:
                        if sub.is_symlink() or sub.is_file():
                            sub.unlink(missing_ok=True)
                        elif sub.is_dir():
                            sub.rmdir()
                    except OSError:
                        continue
                child.rmdir()
        except OSError:
            continue


def _clear_bot_scopes(memory: Memory) -> None:
    """Delete every bot memory scope, for a replacing backup restore."""
    root = memory.bot_scopes_dir
    try:
        children = list(root.iterdir())
    except OSError:
        return
    for child in children:
        if child.name.startswith(".") or not _BOT_SCOPE_ID.match(child.name):
            continue
        try:
            memory.remove_bot_scope(child.name)
        except (ValueError, OSError):
            continue


def _restore_bots(
    home: Path,
    members: dict[str, dict[str, bytes]],
    *,
    merge: bool,
    backup_time: datetime | None,
) -> dict[str, int]:
    """Restore ``bots/<id>/`` definition, persona and avatar files.

    A bot that does not exist here is added. A bot with the same handle is
    kept as is unless the backup is newer, then replaced. A handle that
    would collide with a different local bot is never imported, so a restore
    cannot leave two bots fighting over one ``@mention``. Only the
    definition, the persona and plain image names are written, through no
    link, and every restored file is owner-only on POSIX.
    """
    root = Path(home) / "bots"
    if root.is_symlink():
        raise BackupError("Unsafe bot folder: bots is a link.")
    local_handles = _local_bot_handles(home)
    imported = skipped = 0
    for bot_id in sorted(members):
        files = members[bot_id]
        if "bot.toml" not in files:
            skipped += 1
            continue
        archived_handle = _archived_handle(files["bot.toml"], bot_id)
        local_handle = local_handles.get(bot_id)
        if local_handle is not None:
            if local_handle != archived_handle:
                taken = {handle for other, handle in local_handles.items() if other != bot_id}
                if archived_handle in taken:
                    skipped += 1
                    continue
            if merge and backup_time is not None:
                try:
                    local_time = datetime.fromtimestamp(
                        (root / bot_id / "bot.toml").stat().st_mtime, tz=timezone.utc)
                except OSError:
                    local_time = None
                if local_time is not None and local_time >= backup_time:
                    skipped += 1
                    continue
            elif merge and backup_time is None:
                skipped += 1
                continue
        else:
            if archived_handle in set(local_handles.values()):
                skipped += 1
                continue
        folder = root / bot_id
        try:
            if folder.is_symlink():
                folder.unlink()
            folder.mkdir(parents=True, exist_ok=True)
            if folder.is_symlink():
                raise BackupError(f"Unsafe bot path in backup: bots/{bot_id}")
            base = folder.resolve()
            # A replacing restore drops files the archive no longer carries
            # (a stale avatar), but a merge never deletes local files.
            if not merge:
                for existing in list(folder.iterdir()):
                    if existing.is_symlink() or existing.is_file():
                        if existing.name not in files:
                            existing.unlink(missing_ok=True)
            for filename, data in files.items():
                target = folder / filename
                if target.is_symlink():
                    target.unlink()
                target.write_bytes(data)
                _private_chmod(target)
                try:
                    if target.resolve().parent != base:
                        raise BackupError(f"Unsafe bot path in backup: bots/{bot_id}/{filename}")
                except (OSError, RuntimeError) as exc:
                    raise BackupError(f"Unsafe bot path in backup: bots/{bot_id}/{filename}") from exc
        except BackupError:
            raise
        except OSError as exc:
            raise BackupError(f"Could not restore bot {bot_id}: {exc}") from exc
        local_handles[bot_id] = archived_handle
        imported += 1
    return {"imported": imported, "skipped": skipped}


def _restore_models(cfg: Config, data: dict) -> bool:
    """Replace model entries from the backup, keeping matching local API keys."""
    from kova.config import ProviderEntry
    current = {model.name: model for model in cfg.models}
    existing = {p.id: p for p in cfg.providers}
    provider_ids = {}
    for raw in data.get('providers', []):
        p = ProviderEntry(**{k: v for k, v in raw.items() if k not in ('api_key', 'auth', 'headers')})
        original_id = p.id
        local = existing.get(original_id)
        # IDs are local labels, not connection identities. Never attach local
        # credentials to a backup's different endpoint or authentication kind.
        identity = lambda item: (item.base_url.rstrip('/'), item.kind, item.adapter)
        if local is not None and identity(local) == identity(p):
            provider_ids[original_id] = local.id
            continue
        while p.id in existing:
            p.id += '-restored'
        cfg.providers.append(p)
        existing[p.id] = p
        provider_ids[original_id] = p.id
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
            reasoning_effort=raw.get("reasoning_effort"),
            reasoning_efforts=raw.get("reasoning_efforts", []),
            provider=provider_ids.get(str(raw.get('provider', '')), str(raw.get('provider', ''))),
        )
        local = current.get(entry.name)
        if (local is not None and not entry.provider
                and local.base_url.rstrip('/') == entry.base_url.rstrip('/')):
            entry.api_key = local.api_key
        models.append(entry)
    if not models:
        return False
    cfg.models[:] = models
    if data.get("active") in {model.name for model in models}:
        cfg.active = data["active"]
    save_config(cfg)
    return True


def _same_project(local: dict, project: dict) -> bool:
    """Whether a project already in the store is the one the archive came from.

    The id is the identity. The name and the creation time only tell an edited
    local copy (same project) from an unrelated project that reuses the id.
    """
    if local.get("name") == project.get("name"):
        return True
    return bool(project.get("created")) and local.get("created") == project.get("created")


def _restore_projects(store: Store, projects: list[dict]) -> tuple[dict[str, str], dict[str, int]]:
    """Restore project definitions before their chats and map archived ids.

    A project already here keeps its local name, instructions and folder. An id
    that belongs to a different project (a hand-edited or foreign archive) is
    imported under a fresh id, so the archive neither overwrites nor fails.
    """
    mapping: dict[str, str] = {}
    taken = {project["id"] for project in store.list_projects()}

    def fresh_id() -> str:
        while True:
            candidate = uuid4().hex[:12]
            if candidate not in taken:
                taken.add(candidate)
                return candidate

    imported = skipped = remapped = 0
    for project in projects:
        name = project.get("name") if isinstance(project, dict) else None
        if not isinstance(name, str) or not name.strip():
            # A hand-edited archive may carry junk entries; skip them rather
            # than lose the chats and notes that are still readable.
            skipped += 1
            continue
        source_id = project.get("id") if isinstance(project.get("id"), str) else ""
        if source_id in mapping:
            skipped += 1
            continue
        target_id = source_id
        local = store.get_project(target_id) if target_id else None
        if local is not None and _same_project(local, project):
            skipped += 1
            mapping[source_id] = local["id"]
            continue
        if local is not None:
            target_id = fresh_id()
            remapped += 1
        elif not target_id:
            target_id = fresh_id()
        else:
            taken.add(target_id)
        try:
            added = store.import_project({
                **project,
                "id": target_id,
                # The folder is only kept when it is a real folder here.
                "cwd": _local_folder(project.get("cwd")),
            })
        except (TypeError, ValueError) as exc:
            raise BackupError(f"Could not restore a project: {exc}") from exc
        if added:
            imported += 1
        else:
            # A second entry with the same id in one archive keeps the first.
            skipped += 1
        if source_id:
            mapping[source_id] = target_id
    return mapping, {"imported": imported, "skipped": skipped, "remapped": remapped}


def import_archive(
    cfg: Config,
    path: Path | str,
    *,
    merge: bool = True,
    store: Store | None = None,
) -> dict:
    """Restore a backup. Merge keeps existing chats/projects/notes/reminders.

    Projects are restored before their chats, so membership survives; a chat
    from an archive without projects imports ungrouped. Bots merge by handle:
    a bot that does not exist here is added, while a bot with the same handle
    is kept as is unless the backup is newer, then replaced. Nothing is ever
    restored outside ``bots/`` and ``bot-memory/``: traversal entries and
    links are refused. A replace clears chats, projects, reminders, bots and
    Kova's own memory notes first; it never touches notes outside
    ``<vault>/Kova/`` in Obsidian mode.
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
        project_memory_names = [
            name for name in names if _project_memory_relative(name) is not None
        ]
        infos = {info.filename: info for info in archive.infolist()}
        # A traversal or an absolute path is refused outright: the archive
        # must never write outside the bot folders it claims.
        for name in names:
            if name.endswith("/"):
                continue
            if name.startswith("bots/") or name.startswith("bot-memory/"):
                if _archive_has_traversal(name):
                    raise BackupError(f"Unsafe bot path in backup: {name}")
                info = infos.get(name)
                if info is not None and (info.external_attr >> 16) & 0o170000 == 0o120000:
                    raise BackupError(f"Unsafe bot link in backup: {name}")
        bot_names = [
            name for name in names if _bot_file_relative(name) is not None
        ]
        bot_memory_names = [
            name for name in names if _bot_memory_relative(name) is not None
        ]
        if (not chat_names and not memory_names and not project_memory_names
                and not bot_names and not bot_memory_names
                and "reminders.json" not in names and "projects.json" not in names):
            raise BackupError("That zip does not look like a Kova backup.")

        projects = []
        if "projects.json" in names:
            try:
                projects = json.loads(archive.read("projects.json"))
            except ValueError as exc:
                raise BackupError("Invalid projects.json in the backup.") from exc
            if not isinstance(projects, list):
                raise BackupError("Invalid projects.json in the backup.")

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
        project_notes: list[tuple[str, PurePosixPath, bytes]] = []
        for name in project_memory_names:
            parsed = _project_memory_relative(name)
            if parsed is not None:
                project_id, relative = parsed
                project_notes.append((project_id, relative, archive.read(name)))

        bot_members: dict[str, dict[str, bytes]] = {}
        for name in bot_names:
            parsed = _bot_file_relative(name)
            if parsed is not None:
                bot_id, filename = parsed
                bot_members.setdefault(bot_id, {})[filename] = archive.read(name)
        bot_memory_notes: list[tuple[str, PurePosixPath, bytes]] = []
        for name in bot_memory_names:
            parsed = _bot_memory_relative(name)
            if parsed is not None:
                bot_id, relative = parsed
                bot_memory_notes.append((bot_id, relative, archive.read(name)))

        config_data = None
        if not merge and "config.toml" in names:
            try:
                config_data = tomllib.loads(archive.read("config.toml").decode("utf-8"))
            except (UnicodeDecodeError, tomllib.TOMLDecodeError):
                config_data = None

    store = store or Store(cfg.home / "kova.db")
    if not merge:
        store.clear_chats()
        store.clear_projects()
        store.clear_reminders()
        _clear_bots(cfg.home)

    # Projects come first: a chat can only join one that exists. Archives from
    # before projects carry a chat's project_id with no definition, and those
    # chats import ungrouped.
    project_ids, project_counts = _restore_projects(store, projects)

    # Bots are files, not database rows: a missing bot is added, a bot with
    # the same handle is kept unless the backup is newer, then replaced.
    bot_counts = _restore_bots(
        cfg.home, bot_members, merge=merge,
        backup_time=_manifest_time(manifest),
    )

    chat_ids = {chat["id"] for chat in store.list_chats()} if merge else set()
    chats_imported = chats_skipped = 0
    for chat in chats:
        if merge and chat.get("id") in chat_ids:
            chats_skipped += 1
            continue
        archived_project = chat.get("project_id")
        restored_project = project_ids.get(archived_project) if isinstance(archived_project, str) else None
        try:
            imported = store.import_chat({**chat, "project_id": restored_project})
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

    if not merge:
        memory.clear_scopes()
        _clear_bot_scopes(memory)
    bot_notes_imported = bot_notes_skipped = 0
    seen_bot_notes: set[tuple[str, str]] = set()
    for bot_id, relative, data in bot_memory_notes:
        try:
            scope = memory.bot_scope(bot_id)
        except ValueError:
            bot_notes_skipped += 1
            continue
        target = scope.write_dir / relative
        try:
            target_resolved = target.resolve()
            target_resolved.relative_to(scope.write_dir.resolve())
        except ValueError as exc:
            raise BackupError(f"Unsafe bot memory path in backup: {relative}") from exc
        except (OSError, RuntimeError) as exc:
            raise BackupError(f"Unsafe bot memory path in backup: {relative}") from exc
        if target.is_symlink():
            bot_notes_skipped += 1
            continue
        key = (bot_id, relative.as_posix())
        if merge and (key in seen_bot_notes or target.exists()):
            bot_notes_skipped += 1
            continue
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
            _private_chmod(target)
        except OSError as exc:
            raise BackupError(f"Could not restore bot memory: {exc}") from exc
        seen_bot_notes.add(key)
        bot_notes_imported += 1

    project_notes_imported = project_notes_skipped = 0
    seen_project_notes: set[tuple[str, str]] = set()
    for source_id, relative, data in project_notes:
        # A project id may have been remapped to avoid a local collision; its
        # notes follow it to the restored id. Notes with no project are skipped.
        project_id = project_ids.get(source_id, source_id)
        if store.get_project(project_id) is None:
            project_notes_skipped += 1
            continue
        try:
            scope = memory.project_scope(project_id)
        except ValueError:
            project_notes_skipped += 1
            continue
        target = scope.write_dir / relative
        try:
            target.resolve().relative_to(scope.write_dir.resolve())
        except ValueError as exc:
            raise BackupError(f"Unsafe project memory path in backup: {relative}") from exc
        key = (project_id, relative.as_posix())
        if merge and (key in seen_project_notes or target.exists()):
            project_notes_skipped += 1
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        seen_project_notes.add(key)
        project_notes_imported += 1

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
        "projects": project_counts["imported"],
        "projects_skipped": project_counts["skipped"],
        "projects_remapped": project_counts["remapped"],
        "chats": chats_imported,
        "chats_skipped": chats_skipped,
        "notes": notes_imported,
        "notes_skipped": notes_skipped,
        "project_notes": project_notes_imported,
        "project_notes_skipped": project_notes_skipped,
        "bots": bot_counts["imported"],
        "bots_skipped": bot_counts["skipped"],
        "bot_notes": bot_notes_imported,
        "bot_notes_skipped": bot_notes_skipped,
        "reminders": reminders_imported,
        "reminders_skipped": reminders_skipped,
        "config_restored": config_restored,
    }
