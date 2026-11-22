"""Who is speaking: the default assistant, or one bot in a specific chat.

An :class:`Actor` is the resolved, per-turn view of a speaker. The default
assistant (id ``kova``) is today's behaviour: the user's persona name, the
chat's model, permission and folder, every tool the mode offers. A bot brings
its own name, role prompt, model, tool allowlist, permission ceiling, working
folder, memory scope and turn limits.

:meth:`Actor.for_chat` is the **one** place that combines a bot with a chat, so
the rules that keep a bot from exceeding what the chat allows live in one
function with one test file. A tool a bot is not offered cannot be brought back
by a rule, an approval or an approval setting — the same guarantee a paired
phone already has (see :mod:`kova.pairing`).
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from fnmatch import fnmatchcase
from pathlib import Path

from kova.bots import BotDefinition, default_folder
from kova.permissions import PERMISSIONS

#: The default assistant's actor id; every bot is something else.
DEFAULT_ACTOR_ID = "kova"
#: The agent's own hard outer limit on model calls in one turn.
MAX_ROUNDS = 11


def stricter(*permissions: str) -> str:
    """The least permissive of these chat permissions (``manual`` < ``auto`` < ``full``)."""
    known = [permission for permission in permissions if permission in PERMISSIONS]
    if not known:
        return "manual"
    return min(known, key=PERMISSIONS.index)


@dataclass(frozen=True)
class Actor:
    """One speaker's resolved turn settings."""

    id: str = DEFAULT_ACTOR_ID
    name: str = "Kova"
    #: ``persona.md``: the bot's role section in its system prompt.
    role: str = ""
    #: A ``[[models]]`` entry name; ``None`` keeps the chat's own model.
    model: str | None = None
    reasoning_effort: str | None = None
    #: The effective permission: the stricter of the bot's ceiling and the chat's.
    permission: str = "manual"
    #: The bot's own ceiling, before the chat narrows it.
    ceiling: str = "full"
    #: The bot's working folder for this chat; ``None`` keeps the chat's.
    folder: Path | None = None
    #: Tool-name globs the bot may use; ``None`` means every tool the mode offers.
    tools: tuple[str, ...] | None = None
    #: Globs applied after ``tools``; they win.
    deny: tuple[str, ...] = ()
    max_rounds: int = MAX_ROUNDS
    max_output_tokens: int | None = None
    memory_own: bool = False
    memory_budget: int | None = None
    user_memory: str = "read"
    inherit_rules: bool = True

    @property
    def is_default(self) -> bool:
        """Whether this is the default assistant, whose turns stay as they were."""
        return self.id == DEFAULT_ACTOR_ID

    def allows(self, tool: str) -> bool:
        """Whether this actor may use a tool, by name, after the mode's own choice."""
        if self.is_default:
            return True
        if any(fnmatchcase(tool, pattern) for pattern in self.deny):
            return False
        if self.tools is None:
            return True
        return any(fnmatchcase(tool, pattern) for pattern in self.tools)

    @classmethod
    def bot(cls, bot: BotDefinition, permission: str, *, files_path: Path | None = None,
            name: str | None = None) -> "Actor":
        """The actor for one bot, with the effective permission already narrowed."""
        ceiling = bot.permissions.ceiling if bot.permissions.ceiling in PERMISSIONS else "manual"
        folder = bot.permissions.folder
        if folder:
            resolved = Path(folder).expanduser()
        elif files_path is not None:
            resolved = default_folder(Path(files_path), bot.id)
        else:
            resolved = None
        return Actor(
            id=bot.id,
            name=name or bot.name,
            role=bot.persona,
            model=bot.model.name or None,
            reasoning_effort=bot.model.reasoning_effort or None,
            permission=stricter(ceiling, permission),
            ceiling=ceiling,
            folder=resolved,
            tools=None if bot.permissions.tools is None else tuple(bot.permissions.tools),
            deny=tuple(bot.permissions.deny),
            max_rounds=max(1, min(MAX_ROUNDS, bot.group.max_rounds)),
            max_output_tokens=bot.group.max_output_tokens,
            memory_own=bot.memory.own,
            memory_budget=bot.memory.budget,
            user_memory=bot.memory.user_memory,
            inherit_rules=bot.permissions.inherit_rules,
        )

    @classmethod
    def for_chat(cls, chat: dict | None, bot: BotDefinition | None = None, *,
                 files_path: Path | None = None, name: str = "Kova") -> "Actor":
        """The default assistant (no bot), or one bot in one chat.

        The chat's permission is the chat's ceiling: a bot never gets more than
        the stricter of its own ceiling and the chat's. An explicit folder on
        the bot wins for its direct chats; without one it gets its own folder
        under Kova Files. Every other setting comes from the bot alone.
        """
        chat = chat or {}
        permission = chat.get("permission") or "manual"
        if bot is None:
            return cls(permission=permission if permission in PERMISSIONS else "manual", name=name)
        return cls.bot(bot, permission, files_path=files_path)

    def with_permission(self, permission: str) -> "Actor":
        """The same actor with its effective permission narrowed further."""
        return replace(self, permission=stricter(self.permission, permission))


__all__ = ["Actor", "DEFAULT_ACTOR_ID", "MAX_ROUNDS", "stricter"]
