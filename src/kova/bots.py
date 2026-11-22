"""Bot definitions: a named, saved assistant stored as plain files.

A bot is **data, never code** — the same rule as themes. One folder per bot
under ``KOVA_HOME/bots/``, whose name is the bot id::

    bots/researcher/
        bot.toml       the definition (validated, no secrets)
        persona.md     the role prompt, plain Markdown
        avatar.png     optional image, at most 256 KB

The user never has to open these files: the bot editor writes them. They are
the storage and the full-control path, nothing more. Writes are atomic and mode
0600, and a file the user breaks by hand is listed with its one-line error and
cannot be chatted with, exactly like a broken theme file.

Nothing here imports the agent, the server or a model client: this module is
definitions and file I/O only.
"""

from __future__ import annotations

import os
import re
import tempfile
import tomllib
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

import tomli_w

from kova import chat_appearance, secrets
from kova.modes import MODES

#: The folder name and ``id`` a bot may use; deliberately filename-safe.
BOT_ID = re.compile(r"\A[a-z0-9][a-z0-9-]{0,31}\Z")
#: What ``@mentions`` match: the same safe shape as an id.
HANDLE = re.compile(r"\A[a-z0-9][a-z0-9-]{0,31}\Z")
#: The default assistant's own id; a bot may never take it.
RESERVED_IDS = frozenset({"kova"})
#: Handles that would address the default assistant or "everybody".
RESERVED_HANDLES = frozenset({"kova", "all", "everyone", "here", "you"})
#: The only definition format this Kova understands.
SCHEMA = 1
MAX_NAME = 40
MAX_DESCRIPTION = 120
MAX_PERSONA = 8_000
MAX_AVATAR_BYTES = 256 * 1024
#: Image type -> the filename extension the avatar is stored under.
AVATAR_TYPES = {"image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp"}
CEILINGS = ("manual", "auto", "full")
ANSWERS = ("normal", "always")
USER_MEMORY = ("read", "none")
REASONING_EFFORTS = ("off", "low", "medium", "high")
#: A bot turn's model calls; 11 is the agent's own hard outer limit.
MAX_ROUNDS = 11
DEFAULT_MAX_ROUNDS = 6
DEFAULT_MAX_OUTPUT_TOKENS = 4_000
MAX_OUTPUT_TOKENS = 32_000
MIN_OUTPUT_TOKENS = 100
#: The tool allowlist a new bot starts with: web, files it may read, memory.
DEFAULT_TOOLS = ("web_*", "read_file", "list_files", "memory_*")
#: A character budget above this is a typo, not a choice.
MAX_MEMORY_BUDGET = 200_000
DEFINITION = "bot.toml"
PERSONA = "persona.md"


class BotError(ValueError):
    """A bot definition Kova refuses; the message is one safe line to show."""


# ------------------------------------------------------------------ shapes
@dataclass(frozen=True)
class BotAvatar:
    """How the bot looks: a semantic colour key, an icon or an image file."""

    colour: str = "info"
    icon: str | None = None
    image: str | None = None


@dataclass(frozen=True)
class BotModel:
    """Which model answers for the bot, and how."""

    name: str = ""
    reasoning_effort: str = ""
    mode: str = "work"
    web: bool = True


@dataclass(frozen=True)
class BotPermissions:
    """What the bot may ever do: a ceiling, a tool allowlist and its folder."""

    ceiling: str = "manual"
    tools: tuple[str, ...] = DEFAULT_TOOLS
    deny: tuple[str, ...] = ()
    folder: str | None = None
    inherit_rules: bool = True


@dataclass(frozen=True)
class BotMemory:
    """The bot's own notes, their budget and whether it may read the user's."""

    own: bool = True
    budget: int = 4_000
    user_memory: str = "read"


@dataclass(frozen=True)
class BotGroup:
    """The defaults this bot brings into a group chat."""

    answers: str = "normal"
    max_rounds: int = DEFAULT_MAX_ROUNDS
    max_output_tokens: int = DEFAULT_MAX_OUTPUT_TOKENS


@dataclass(frozen=True)
class BotDefinition:
    """One validated bot. ``path`` is the folder it was read from, if any.

    ``warnings`` are advisory only (a removed model, a missing avatar): the bot
    still works, so a hand edit never hides it from the list.
    """

    id: str
    name: str
    handle: str
    description: str = ""
    schema: int = SCHEMA
    avatar: BotAvatar = field(default_factory=BotAvatar)
    model: BotModel = field(default_factory=BotModel)
    permissions: BotPermissions = field(default_factory=BotPermissions)
    memory: BotMemory = field(default_factory=BotMemory)
    group: BotGroup = field(default_factory=BotGroup)
    persona: str = ""
    warnings: tuple[str, ...] = ()
    path: Path | None = None

    def to_data(self) -> dict:
        """The definition as the shape ``bot.toml`` stores (no warnings, no path)."""
        avatar: dict[str, Any] = {"colour": self.avatar.colour}
        if self.avatar.icon:
            avatar["icon"] = self.avatar.icon
        if self.avatar.image:
            avatar["image"] = self.avatar.image
        permissions: dict[str, Any] = {
            "ceiling": self.permissions.ceiling,
            "tools": list(self.permissions.tools),
            "deny": list(self.permissions.deny),
            "inherit_rules": self.permissions.inherit_rules,
        }
        if self.permissions.folder:
            permissions["folder"] = self.permissions.folder
        return {
            "schema": self.schema,
            "id": self.id,
            "name": self.name,
            "handle": self.handle,
            "description": self.description,
            "avatar": avatar,
            "model": {
                "name": self.model.name,
                "reasoning_effort": self.model.reasoning_effort,
                "mode": self.model.mode,
                "web": self.model.web,
            },
            "permissions": permissions,
            "memory": {"own": self.memory.own, "budget": self.memory.budget,
                       "user_memory": self.memory.user_memory},
            "group": {"answers": self.group.answers,
                      "max_rounds": self.group.max_rounds,
                      "max_output_tokens": self.group.max_output_tokens},
        }

    def summary(self) -> dict:
        """The shape clients list: identity, look, model and state, no persona text."""
        return {
            "id": self.id,
            "name": self.name,
            "handle": self.handle,
            "description": self.description,
            "colour": self.avatar.colour,
            "icon": self.avatar.icon,
            "image": self.avatar.image,
            "model": self.model.name,
            "mode": self.model.mode,
            "ceiling": self.permissions.ceiling,
        }


@dataclass(frozen=True)
class BotRecord:
    """One folder under ``bots/``: a working bot, or a file's one-line error."""

    id: str
    path: Path
    bot: BotDefinition | None = None
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.bot is not None and self.error is None


# ------------------------------------------------------------------ helpers
def bot_root(home: Path) -> Path:
    """Where every bot folder lives: ``KOVA_HOME/bots``."""
    return Path(home) / "bots"


def default_folder(files_path: Path, bot_id: str) -> Path:
    """The working folder a bot gets when its file names none."""
    return Path(files_path) / "bots" / bot_id


def avatar_path(bot: BotDefinition) -> Path | None:
    """The stored avatar image, when the definition names one that exists."""
    if bot.path is None or not bot.avatar.image:
        return None
    path = bot.path / bot.avatar.image
    return path if path.is_file() and not path.is_symlink() else None


def _one_line(text: str) -> str:
    """Collapse whatever a parser produced into one display line."""
    return " ".join(str(text).split())


def _table(data: Mapping[str, Any], key: str) -> dict:
    value = data.get(key, {})
    if not isinstance(value, Mapping):
        raise BotError(f"[{key}] must be a table.")
    return dict(value)


def _known(table: Mapping[str, Any], allowed: Iterable[str], where: str) -> None:
    """Refuse a key Kova does not know: a typo must fail loudly."""
    for key in table:
        if key not in allowed:
            label = f"{where}.{key}" if where else str(key)
            raise BotError(f"Unknown key {label}. Remove it or fix the spelling.")


def _text(value: Any, where: str) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise BotError(f"{where} must be text.")
    return value


def _flag(value: Any, where: str, default: bool) -> bool:
    if value is None:
        return default
    if not isinstance(value, bool):
        raise BotError(f"{where} must be true or false.")
    return value


def _patterns(value: Any, where: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise BotError(f"{where} must be a list of tool-name patterns.")
    for item in value:
        if not item.strip() or any(ord(char) < 32 for char in item):
            raise BotError(f"{where} has an empty or unprintable pattern.")
    return tuple(item.strip() for item in value)


def _number(value: Any, where: str, default: int, low: int, high: int) -> int:
    if value is None:
        return default
    if not isinstance(value, int) or isinstance(value, bool):
        raise BotError(f"{where} must be a whole number.")
    if not low <= value <= high:
        raise BotError(f"{where} must be {low}-{high}.")
    return value


def _secret_free(values: Iterable[Any]) -> None:
    """Refuse a definition that carries something shaped like a credential."""
    for value in values:
        if not isinstance(value, str) or not value:
            continue
        scan = secrets.detect(value)
        if scan.findings:
            raise BotError(f"This looks like a secret ({scan.findings[0].kind}). "
                           "Bots reference models by name only; never paste keys into a bot.")


def _strings(value: Any) -> Iterable[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, Mapping):
        for key, item in value.items():
            yield str(key)
            yield from _strings(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from _strings(item)


def _working_folder(value: str, *, bot_folder: Path | None, kova_home: Path | None) -> str:
    """Validate a bot's own working folder, returning it as an absolute path."""
    raw = value.strip()
    if not raw or "\0" in raw:
        raise BotError("Working folder must be a path.")
    path = Path(raw).expanduser()
    if not path.is_absolute():
        raise BotError("Working folder must be an absolute path.")
    if path.is_symlink():
        raise BotError("Working folder must not be a link or junction.")
    if kova_home is not None:
        home = Path(kova_home).expanduser()
        if path == home or path.is_relative_to(home) or home.is_relative_to(path):
            raise BotError("Working folder must be outside the Kova home.")
    if bot_folder is not None:
        try:
            if path.resolve().is_relative_to(Path(bot_folder).resolve()):
                raise BotError("Working folder must not be inside the bot's own files.")
        except OSError:
            pass
    if path.exists() and not path.is_dir():
        raise BotError("Working folder is not a directory.")
    return str(path)


# ------------------------------------------------------------------ validate
def validate(data: Mapping[str, Any], *, bot_folder: Path | None = None,
             models: Iterable[str] = (), handles: Iterable[str] = (),
             kova_home: Path | None = None, persona: str = "",
             warnings: Iterable[str] = ()) -> BotDefinition:
    """Turn a parsed ``bot.toml`` into a definition, or raise :class:`BotError`.

    ``bot_folder`` is the folder the definition is stored in (its name must be
    the id); ``handles`` are the handles other bots already use. A model name
    that no longer exists is only a warning: a removed model must not hide a
    bot that can still answer with the active one.
    """
    if not isinstance(data, Mapping):
        raise BotError("A bot definition must be a table.")
    _known(data, ("schema", "id", "name", "handle", "description", "avatar", "model",
                  "permissions", "memory", "group"), "")
    schema = _number(data.get("schema"), "schema", SCHEMA, 1, 9_999)
    if schema != SCHEMA:
        raise BotError(f"This bot file is version {schema}; this Kova understands version {SCHEMA}.")

    bot_id = _text(data.get("id"), "id").strip()
    if not BOT_ID.match(bot_id):
        raise BotError("A bot id uses lowercase letters, digits and dashes (1-32).")
    if bot_id in RESERVED_IDS:
        raise BotError(f'"{bot_id}" is reserved. Pick another id.')
    if bot_folder is not None and Path(bot_folder).name != bot_id:
        raise BotError(f"The id must match its folder name ({Path(bot_folder).name}).")

    name = _text(data.get("name"), "name").strip()
    if not name or len(name) > MAX_NAME or any(ord(char) < 32 for char in name):
        raise BotError(f"Name must be 1-{MAX_NAME} printable characters.")

    handle = _text(data.get("handle"), "handle").strip().lower() or bot_id
    if not HANDLE.match(handle):
        raise BotError("A handle uses lowercase letters, digits and dashes (1-32).")
    if handle in RESERVED_HANDLES:
        raise BotError(f'"{handle}" is reserved. Pick another handle.')
    taken = {str(other).casefold() for other in handles}
    if handle.casefold() in taken:
        raise BotError(f"The handle @{handle} is already used by another bot.")

    description = _text(data.get("description"), "description").strip()
    if len(description) > MAX_DESCRIPTION or "\n" in description or "\r" in description:
        raise BotError(f"Description must be one line of at most {MAX_DESCRIPTION} characters.")

    avatar_data = _table(data, "avatar")
    _known(avatar_data, ("colour", "icon", "image"), "avatar")
    colour = _text(avatar_data.get("colour"), "avatar.colour").strip() or "info"
    try:
        colour = chat_appearance.normalize_colour(colour) or "info"
        icon = chat_appearance.normalize_icon(_text(avatar_data.get("icon"), "avatar.icon"))
    except ValueError as exc:
        raise BotError(str(exc)) from None
    image = _text(avatar_data.get("image"), "avatar.image").strip() or None
    if image is not None:
        if (Path(image).name != image or image.startswith(".") or "/" in image
                or "\\" in image or any(ord(char) < 32 for char in image)):
            raise BotError("avatar.image must be a plain file name inside the bot folder.")

    model_data = _table(data, "model")
    _known(model_data, ("name", "reasoning_effort", "mode", "web"), "model")
    model_name = _text(model_data.get("name"), "model.name").strip()
    effort = _text(model_data.get("reasoning_effort"), "model.reasoning_effort").strip().lower()
    if effort and effort not in REASONING_EFFORTS:
        raise BotError(f"model.reasoning_effort must be one of: {', '.join(REASONING_EFFORTS)}.")
    mode = _text(model_data.get("mode"), "model.mode").strip().lower() or "work"
    if mode not in MODES:
        raise BotError(f"model.mode must be one of: {', '.join(MODES)}.")
    web = _flag(model_data.get("web"), "model.web", True)

    permissions_data = _table(data, "permissions")
    _known(permissions_data, ("ceiling", "tools", "deny", "folder", "inherit_rules"),
           "permissions")
    ceiling = _text(permissions_data.get("ceiling"), "permissions.ceiling").strip().lower() or "manual"
    if ceiling not in CEILINGS:
        raise BotError(f"permissions.ceiling must be one of: {', '.join(CEILINGS)}.")
    tools = _patterns(permissions_data.get("tools", list(DEFAULT_TOOLS)), "permissions.tools")
    deny = _patterns(permissions_data.get("deny"), "permissions.deny")
    inherit_rules = _flag(permissions_data.get("inherit_rules"), "permissions.inherit_rules", True)
    folder = _text(permissions_data.get("folder"), "permissions.folder").strip() or None
    if folder is not None:
        folder = _working_folder(folder, bot_folder=bot_folder, kova_home=kova_home)

    memory_data = _table(data, "memory")
    _known(memory_data, ("own", "budget", "user_memory"), "memory")
    own = _flag(memory_data.get("own"), "memory.own", True)
    budget = _number(memory_data.get("budget"), "memory.budget", 4_000, 0, MAX_MEMORY_BUDGET)
    user_memory = _text(memory_data.get("user_memory"), "memory.user_memory").strip().lower() or "read"
    if user_memory not in USER_MEMORY:
        raise BotError(f"memory.user_memory must be one of: {', '.join(USER_MEMORY)}.")

    group_data = _table(data, "group")
    _known(group_data, ("answers", "max_rounds", "max_output_tokens"), "group")
    answers = _text(group_data.get("answers"), "group.answers").strip().lower() or "normal"
    if answers not in ANSWERS:
        raise BotError(f"group.answers must be one of: {', '.join(ANSWERS)}.")
    rounds = _number(group_data.get("max_rounds"), "group.max_rounds", DEFAULT_MAX_ROUNDS,
                     1, MAX_ROUNDS)
    output_tokens = _number(group_data.get("max_output_tokens"), "group.max_output_tokens",
                            DEFAULT_MAX_OUTPUT_TOKENS, MIN_OUTPUT_TOKENS, MAX_OUTPUT_TOKENS)

    if len(persona) > MAX_PERSONA:
        raise BotError(f"Persona must be at most {MAX_PERSONA:,} characters.")
    _secret_free([*_strings(data), persona])

    model_names = {str(entry) for entry in models}
    notes = list(warnings)
    if model_name and model_names and model_name not in model_names:
        notes.append(f"Model '{model_name}' is not configured, so the active model is used.")

    return BotDefinition(
        schema=schema, id=bot_id, name=name, handle=handle, description=description,
        avatar=BotAvatar(colour=colour, icon=icon, image=image),
        model=BotModel(name=model_name, reasoning_effort=effort, mode=mode, web=web),
        permissions=BotPermissions(ceiling=ceiling, tools=tools, deny=deny, folder=folder,
                                   inherit_rules=inherit_rules),
        memory=BotMemory(own=own, budget=budget, user_memory=user_memory),
        group=BotGroup(answers=answers, max_rounds=rounds, max_output_tokens=output_tokens),
        persona=persona, warnings=tuple(notes),
    )


# ------------------------------------------------------------------- reading
def _read_persona(folder: Path) -> str:
    """The bot's role prompt; a link or an oversized file is refused."""
    path = folder / PERSONA
    if not path.exists():
        return ""
    if path.is_symlink():
        raise BotError(f"{PERSONA} must not be a link.")
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise BotError(f"{PERSONA} cannot be read: {_one_line(exc)}") from None
    if len(text) > MAX_PERSONA:
        raise BotError(f"Persona must be at most {MAX_PERSONA:,} characters.")
    return text


def load_bot(folder: Path, *, models: Iterable[str] = (), handles: Iterable[str] = (),
             kova_home: Path | None = None) -> BotDefinition:
    """Read and validate one bot folder.

    Raises :class:`BotError` (never a bare parser exception) so a listing can
    show one safe line for the file instead of failing the whole list.
    """
    folder = Path(folder)
    try:
        folder = folder.expanduser()
        if folder.is_symlink():
            raise BotError("The bot folder must not be a link.")
        path = folder / DEFINITION
        if not path.exists():
            raise BotError(f"{DEFINITION} is missing.")
        if path.is_symlink():
            raise BotError(f"{DEFINITION} must not be a link.")
        try:
            raw = path.read_bytes()
        except OSError as exc:
            raise BotError(f"{DEFINITION} cannot be read: {_one_line(exc)}") from None
        try:
            data = tomllib.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
            raise BotError(f"{DEFINITION} is not valid TOML: {_one_line(exc)}") from None
    except BotError:
        raise
    except (OSError, ValueError) as exc:  # an unreadable path is still one line
        raise BotError(_one_line(exc)) from None
    bot = validate(data, bot_folder=folder, models=models, handles=handles,
                   kova_home=kova_home, persona=_read_persona(folder))
    image = bot.avatar.image
    if image and avatar_path(replace(bot, path=folder)) is None:
        bot = replace(bot, warnings=(*bot.warnings, f"Avatar image {image} is missing."))
    return replace(bot, path=folder)


def list_bots(home: Path, *, models: Iterable[str] = ()) -> list[BotRecord]:
    """Every bot folder with its definition or its one-line error.

    A duplicate handle makes both bots unusable (routing would be ambiguous),
    so each is listed with the reason instead of silently picking one.
    """
    root = bot_root(home)
    records: list[BotRecord] = []
    if not root.is_dir() or root.is_symlink():
        return records
    try:
        children = sorted(root.iterdir(), key=lambda path: path.name.casefold())
    except OSError:
        return records
    for child in children:
        if child.name.startswith(".") or not child.is_dir():
            continue
        try:
            bot = load_bot(child, models=models, kova_home=Path(home))
        except BotError as exc:
            records.append(BotRecord(id=child.name, path=child, error=_one_line(str(exc))))
        except Exception as exc:  # a hand-edited file must never break the list
            records.append(BotRecord(id=child.name, path=child,
                                     error=_one_line(f"{type(exc).__name__}: {exc}")))
        else:
            records.append(BotRecord(id=bot.id, path=child, bot=bot))
    by_handle: dict[str, list[BotRecord]] = {}
    for record in records:
        if record.bot is not None:
            by_handle.setdefault(record.bot.handle.casefold(), []).append(record)
    for handle, group in by_handle.items():
        if len(group) < 2:
            continue
        reason = f"The handle @{handle} is used by another bot."
        for record in group:
            if record.bot is not None:
                records[records.index(record)] = replace(record, error=reason)
    return records


def get_bot(home: Path, bot_id: str, *, models: Iterable[str] = ()) -> BotDefinition | None:
    """One bot by id, or None when it is missing or its file is broken."""
    if not BOT_ID.match(bot_id or ""):
        return None
    folder = bot_root(home) / bot_id
    if not folder.is_dir() or folder.is_symlink():
        return None
    try:
        return load_bot(folder, models=models, kova_home=Path(home))
    except BotError:
        return None


# ------------------------------------------------------------------- writing
def _atomic_write(path: Path, data: bytes) -> None:
    """Replace ``path`` through a 0600 temp file, never through a link."""
    if path.is_symlink():
        raise BotError(f"{path.name} must not be a link.")
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".bot-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def save_bot(home: Path, definition: BotDefinition, *, models: Iterable[str] = (),
             persona: str | None = None) -> BotDefinition:
    """Write one bot's files, refusing a definition the folder would not accept.

    The definition is validated against its own folder and the handles other
    bots already use, so a hand-built draft can never overwrite another bot's
    handle. ``persona`` replaces ``persona.md`` when given; otherwise the
    definition's own persona text is written.
    """
    home = Path(home)
    folder = bot_root(home) / definition.id
    if folder.is_symlink():
        raise BotError("The bot folder must not be a link.")
    others = [record.bot.handle for record in list_bots(home, models=models)
              if record.bot is not None and record.bot.id != definition.id]
    text = definition.persona if persona is None else persona
    checked = validate(definition.to_data(), bot_folder=folder, models=models, handles=others,
                       kova_home=home, persona=text)
    checked = replace(checked, persona=text, path=folder)
    folder.mkdir(parents=True, exist_ok=True)
    _atomic_write(folder / DEFINITION,
                  (tomli_w.dumps(checked.to_data()) + "\n").encode("utf-8"))
    _atomic_write(folder / PERSONA, text.encode("utf-8"))
    if checked.avatar.image:
        image = folder / checked.avatar.image
        if not image.is_file():
            # A definition may name an image before it is uploaded; drop the
            # reference rather than keep a bot pointing at nothing.
            checked = replace(checked, avatar=replace(checked.avatar, image=None),
                              warnings=(*checked.warnings,
                                        f"Avatar image {checked.avatar.image} was missing."))
            _atomic_write(folder / DEFINITION,
                          (tomli_w.dumps(checked.to_data()) + "\n").encode("utf-8"))
    return checked


def save_avatar(bot: BotDefinition, data: bytes, mime: str) -> BotDefinition:
    """Store one validated avatar image in the bot folder and point the file at it."""
    extension = AVATAR_TYPES.get(mime)
    if extension is None:
        raise BotError("Avatar must be a PNG, JPEG or WebP image.")
    if len(data) > MAX_AVATAR_BYTES:
        raise BotError(f"Avatar must be at most {MAX_AVATAR_BYTES // 1024} KB.")
    if not data:
        raise BotError("Avatar image is empty.")
    _check_image(data, mime)
    if bot.path is None:
        raise BotError("Save the bot before adding an avatar.")
    folder = Path(bot.path)
    name = f"avatar{extension}"
    _atomic_write(folder / name, data)
    for other, other_extension in AVATAR_TYPES.items():
        if other_extension != extension:
            (folder / f"avatar{other_extension}").unlink(missing_ok=True)
    updated = replace(bot, avatar=replace(bot.avatar, image=name))
    _atomic_write(folder / DEFINITION, (tomli_w.dumps(_without(updated).to_data()) + "\n").encode("utf-8"))
    return replace(updated, persona=bot.persona, path=folder)


def clear_avatar(bot: BotDefinition) -> BotDefinition:
    """Remove the avatar image and the reference to it."""
    if bot.path is None:
        return replace(bot, avatar=replace(bot.avatar, image=None))
    folder = Path(bot.path)
    for extension in AVATAR_TYPES.values():
        (folder / f"avatar{extension}").unlink(missing_ok=True)
    updated = replace(bot, avatar=replace(bot.avatar, image=None))
    _atomic_write(folder / DEFINITION, (tomli_w.dumps(_without(updated).to_data()) + "\n").encode("utf-8"))
    return updated


def _without(bot: BotDefinition) -> BotDefinition:
    """The definition with load-time extras cleared, so it writes back exactly."""
    return replace(bot, warnings=(), path=bot.path)


def _check_image(data: bytes, mime: str) -> None:
    """Refuse a file whose bytes are not the type its name claims."""
    signatures = {"image/png": b"\x89PNG\r\n\x1a\n", "image/jpeg": b"\xff\xd8\xff",
                  "image/webp": b"RIFF"}
    signature = signatures[mime]
    if not data.startswith(signature):
        raise BotError("That file is not the image type it claims to be.")
    if mime == "image/webp" and data[8:12] != b"WEBP":
        raise BotError("That file is not the image type it claims to be.")


def delete_bot(home: Path, bot_id: str) -> bool:
    """Remove one bot's folder; its chats and messages are left alone."""
    if not BOT_ID.match(bot_id or ""):
        return False
    folder = bot_root(home) / bot_id
    if not folder.is_dir() or folder.is_symlink() or folder.is_junction():
        return False
    if folder.resolve().parent != bot_root(home).resolve():
        return False
    removed = True
    for child in sorted(folder.rglob("*"), key=lambda path: len(path.parts), reverse=True):
        try:
            if child.is_symlink() or child.is_file():
                child.unlink()
            elif child.is_dir():
                child.rmdir()
        except OSError:
            removed = False
    try:
        folder.rmdir()
    except OSError:
        removed = False
    return removed


__all__ = [
    "BotAvatar", "BotDefinition", "BotError", "BotGroup", "BotMemory", "BotModel",
    "BotPermissions", "BotRecord", "MAX_AVATAR_BYTES", "avatar_path", "bot_root",
    "clear_avatar", "default_folder", "delete_bot", "get_bot", "list_bots", "load_bot",
    "save_avatar", "save_bot", "validate",
]
