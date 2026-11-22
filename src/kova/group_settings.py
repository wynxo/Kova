"""The one schema for group and member settings (task 2.1).

Every behaviour a group chat has is a value in this file: who answers, how bots
reply to each other, the limits that stop a chain, approvals, the working
folder and what bots share. Each value carries its label, one-line help, type,
range or allowed values, default, whether it belongs in the Simple view, and
the hard ceiling no setting may exceed. The four presets are named sets of
values, nothing more.

The web, desktop, phone and terminal settings views all render ``GET
/group-settings`` (:func:`schema`), so a new knob is one entry here and cannot
drift between surfaces.

Storage holds **only the values the user changed**; :func:`resolve` fills in
the defaults, and resetting a key is simply deleting it (``None`` means reset
in :func:`validate`). Each layer may only narrow the one above it: the hard
ceilings here, then the chat's permission, then these settings, then per-bot
overrides (:func:`member_resolve`), then the per-message answer override,
which can never raise a limit.

Two settings from the design are deliberately absent until their tasks land,
because a setting appears in the UI only when it works:

- ``retry`` arrives with retry in groups (task 2.10);
- ``memory`` (memory sharing) arrives with the memory scopes (task 2.12).

Nothing here imports the agent, the server or a model client: this module is
pure data, validation and defaults.
"""

from __future__ import annotations

import copy
import math
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

from kova.bots import BOT_ID

# ---------------------------------------------------------------- ceilings
#: A group holds this many bots at most; also the most a mode may pick.
MAX_MEMBERS = 8
#: Bot-to-bot replies after one user message.
MAX_HOPS = 8
#: Times the same two bots may hand work back and forth.
MAX_PING_PONG = 8
#: Every bot turn one user message may start, first pass included.
MAX_BOT_TURNS = 16
#: Summed tokens of every bot turn and routing call in one user message.
MIN_TOKENS, MAX_TOKENS = 1_000, 400_000
#: Wall clock for the whole user turn.
MIN_SECONDS, MAX_SECONDS = 30, 3_600
#: A group's instructions are added to every bot's prompt.
MAX_INSTRUCTIONS = 2_000
#: One bot turn's model calls; 11 is the agent's own hard outer limit.
MAX_MEMBER_ROUNDS = 11
MIN_MEMBER_TOKENS, MAX_MEMBER_TOKENS = 100, 32_000
#: A bot's own [group] defaults, used when the bot is the default assistant.
DEFAULT_MAX_ROUNDS = 6
DEFAULT_MAX_OUTPUT_TOKENS = 4_000

ANSWER_MODES = ("mentioned", "lead", "auto", "everyone", "subset", "count",
                "random", "lead_only")
REPLY_MODES = ("off", "mentioned", "turns")
APPROVALS = ("ask", "once", "allow", "deny")
MEMBER_APPROVALS = ("same", *APPROVALS)
MEMBER_ANSWERS = ("same", "always", "normal")
FOLDER_MODES = ("shared", "own")
SHARE_WORK = ("replies", "tools", "everything")
MENTION_MODES = ("replace", "add")

#: The preset a group is when a value is not one of the four named ones.
CUSTOM = "custom"

#: The sections of the Advanced sheet, in the order the sheet shows them.
SECTIONS = ("Answering", "Bots reply to each other", "Limits", "Approvals",
            "Working folder", "What bots share")
#: The bot card's own blocks.
MEMBER_SECTIONS = ("In this group", "Limits")

#: The debate preset's group instructions (7.4).
DEBATE_INSTRUCTIONS = (
    "Answer the other participants' points directly. Say plainly when you have "
    "been convinced. Reply (pass) when you have nothing new."
)


class SettingsError(ValueError):
    """A settings value Kova refuses; the message is one safe line to show."""


# ------------------------------------------------------------------ shapes
@dataclass(frozen=True)
class Setting:
    """One knob: what it is called, what it means, what it accepts.

    ``ceiling`` is the hard bound no layer may exceed: the maximum for a
    number, the allowed values for an enum, the length for text, the largest
    group for ``bots``. ``None`` means the setting has no ceiling beyond its
    own validation.
    """

    key: str
    label: str
    help: str
    type: str
    default: Any
    section: str
    scope: str = "group"
    simple: bool = False
    values: tuple[str, ...] = ()
    minimum: int | float | None = None
    maximum: int | float | None = None
    ceiling: Any = None
    max_length: int | None = None

    def public(self) -> dict:
        """The setting as ``GET /group-settings`` describes it to a client."""
        data: dict[str, Any] = {
            "key": self.key,
            "label": self.label,
            "help": self.help,
            "type": self.type,
            "default": list(self.default) if isinstance(self.default, tuple) else self.default,
            "section": self.section,
            "scope": self.scope,
            "simple": self.simple,
        }
        if self.values:
            data["values"] = list(self.values)
        if self.minimum is not None:
            data["min"] = self.minimum
        if self.maximum is not None:
            data["max"] = self.maximum
        if self.max_length is not None:
            data["max_length"] = self.max_length
        data["ceiling"] = list(self.ceiling) if isinstance(self.ceiling, tuple) else self.ceiling
        return data


#: Every group setting, in the order the settings sheet shows it.
SETTINGS: tuple[Setting, ...] = (
    # ---------------------------------------------------------- Answering
    Setting("answer.mode", "Who answers",
            "Who answers your message when you do not @mention anyone.",
            "enum", "lead", "Answering", simple=True,
            values=ANSWER_MODES, ceiling=ANSWER_MODES),
    Setting("answer.lead", "Lead",
            "The bot that answers when nobody is @mentioned, and the one that "
            "decides or receives requests in the lead modes.",
            "bot", None, "Answering", simple=True),
    Setting("answer.bots", "These bots",
            "The members that answer in These bots mode.",
            "bots", (), "Answering", simple=True, ceiling=MAX_MEMBERS),
    Setting("answer.count", "N bots",
            "How many bots answer in N bots (taking turns) and Random modes.",
            "int", 2, "Answering", simple=True,
            minimum=1, maximum=MAX_MEMBERS, ceiling=MAX_MEMBERS),
    Setting("answer.auto_max", "Lead may pick up to",
            "In The lead decides mode, the most bots the lead may pick for one message.",
            "int", 2, "Answering",
            minimum=1, maximum=MAX_MEMBERS, ceiling=MAX_MEMBERS),
    Setting("answer.mentions", "@mentions",
            "Win: only the bots you @mention answer. Add to the picks: they "
            "answer first, then the mode's own picks.",
            "enum", "replace", "Answering",
            values=MENTION_MODES, ceiling=MENTION_MODES),
    Setting("replies.allow_pass", "Let bots pass",
            "A bot may reply (pass) when it has nothing to add, instead of "
            "inventing something.",
            "bool", True, "Answering"),
    Setting("see_earlier_replies", "See earlier replies",
            "Off makes a fair panel: bots in the same round answer without "
            "seeing each other's replies.",
            "bool", True, "Answering"),
    Setting("instructions", "Group instructions",
            "Added to every bot's prompt in this group.",
            "text", "", "Answering", max_length=MAX_INSTRUCTIONS,
            ceiling=MAX_INSTRUCTIONS),

    # --------------------------------------------- Bots reply to each other
    Setting("replies.mode", "Bots reply to each other",
            "Off: only your message starts bots. When @mentioned: a bot's "
            "@mentions hand work on. Take turns: the members keep answering in "
            "order until a limit stops them.",
            "enum", "mentioned", "Bots reply to each other", simple=True,
            values=REPLY_MODES, ceiling=REPLY_MODES),
    Setting("replies.max_hops", "Up to",
            "How many times bots may answer each other after your message.",
            "int", 3, "Bots reply to each other", simple=True,
            minimum=0, maximum=MAX_HOPS, ceiling=MAX_HOPS),
    Setting("replies.ping_pong", "Same two in a row",
            "How many times two bots may hand work back and forth before the "
            "chain stops. Ignored when bots take turns.",
            "int", 2, "Bots reply to each other",
            minimum=1, maximum=MAX_PING_PONG, ceiling=MAX_PING_PONG),

    # -------------------------------------------------------------- Limits
    Setting("limits.max_bot_turns", "Bot turns",
            "How many bot turns one message may start, the first pass included.",
            "int", 8, "Limits",
            minimum=1, maximum=MAX_BOT_TURNS, ceiling=MAX_BOT_TURNS),
    Setting("limits.max_tokens", "Tokens",
            "The most tokens all bots may use for one message.",
            "int", 40_000, "Limits",
            minimum=MIN_TOKENS, maximum=MAX_TOKENS, ceiling=MAX_TOKENS),
    Setting("limits.max_cost_usd", "Cost",
            "The most a message may cost, in US dollars. Only priced models "
            "count; empty means no limit.",
            "money", None, "Limits", minimum=0.0),
    Setting("limits.max_seconds", "Time",
            "The longest one message's bot turns may run, in seconds.",
            "int", 600, "Limits",
            minimum=MIN_SECONDS, maximum=MAX_SECONDS, ceiling=MAX_SECONDS),

    # ----------------------------------------------------------- Approvals
    Setting("approvals", "Approvals",
            "Whether Kova asks you before a bot acts: every time, once per "
            "chat, always for the bot, or never.",
            "enum", "ask", "Approvals", simple=True,
            values=APPROVALS, ceiling=APPROVALS),

    # ------------------------------------------------------- Working folder
    Setting("folder", "Working folder",
            "Shared: every bot works in the group's folder. Each bot its own: "
            "every bot uses its own folder.",
            "enum", "shared", "Working folder", simple=True,
            values=FOLDER_MODES, ceiling=FOLDER_MODES),
    Setting("folder_path", "Shared folder",
            "Where the group works. Empty uses the project folder, or a folder "
            "made for this group.",
            "path", None, "Working folder"),

    # ----------------------------------------------------- What bots share
    Setting("share_work", "Show each other's work",
            "Replies only; replies plus each other's tool output; or "
            "everything, including reasoning. Tool output can contain text "
            "from web pages, so share it only between bots you trust.",
            "enum", "replies", "What bots share",
            values=SHARE_WORK, ceiling=SHARE_WORK),
)

#: Every per-bot override in a group, shown on the bot card. Muting and
#: position are membership data (``chat_members`` columns), not settings.
MEMBER_SETTINGS: tuple[Setting, ...] = (
    Setting("approvals", "Approvals",
            "What this bot may do without asking in this group. Same as the "
            "group follows the group's approval setting.",
            "enum", "same", "In this group", scope="member", simple=True,
            values=MEMBER_APPROVALS, ceiling=MEMBER_APPROVALS),
    Setting("answers", "Answers every message",
            "Always: this bot answers every message you send, even when the "
            "rule picks others. Only when picked: it answers when the group's "
            "rule or an @mention picks it. Same as the bot follows the bot's "
            "own setting.",
            "enum", "same", "In this group", scope="member", simple=True,
            values=MEMBER_ANSWERS, ceiling=MEMBER_ANSWERS),
    Setting("muted", "Muted",
            "Stays in the group but never answers.",
            "bool", False, "In this group", scope="member", simple=True),
    Setting("max_rounds", "Model calls per turn",
            "The most model calls this bot may make in one turn here. Empty "
            "uses the bot's own limit.",
            "int", None, "Limits", scope="member",
            minimum=1, maximum=MAX_MEMBER_ROUNDS, ceiling=MAX_MEMBER_ROUNDS),
    Setting("max_output_tokens", "Output tokens",
            "The most tokens one reply from this bot may have here. Empty uses "
            "the bot's own limit.",
            "int", None, "Limits", scope="member",
            minimum=MIN_MEMBER_TOKENS, maximum=MAX_MEMBER_TOKENS,
            ceiling=MAX_MEMBER_TOKENS),
)

_BY_KEY: dict[tuple[str, str], Setting] = {
    (setting.scope, setting.key): setting for setting in (*SETTINGS, *MEMBER_SETTINGS)
}
_TABLES: dict[str, frozenset[str]] = {}
for _setting in SETTINGS:
    if "." in _setting.key:
        _table, _leaf = _setting.key.split(".", 1)
        _TABLES.setdefault(_table, frozenset())
        _TABLES[_table] = _TABLES[_table] | {_leaf}


# --------------------------------------------------------------- defaults
def _set_path(target: dict, key: str, value: Any) -> None:
    if "." in key:
        table, leaf = key.split(".", 1)
        target.setdefault(table, {})[leaf] = value
    else:
        target[key] = value


def _get_path(source: Mapping, key: str) -> Any:
    if "." in key:
        table, leaf = key.split(".", 1)
        return source.get(table, {}).get(leaf)
    return source.get(key)


def _drop_empty_tables(target: dict) -> dict:
    for table in [name for name, value in target.items() if isinstance(value, dict)]:
        if not target[table]:
            del target[table]
    return target


def defaults() -> dict:
    """Every group setting at its default, as a fresh JSON-ready mapping."""
    out: dict[str, Any] = {}
    for setting in SETTINGS:
        _set_path(out, setting.key, copy.deepcopy(setting.default))
    return _json_ready(out)


def _json_ready(value: Any) -> Any:
    """Tuples become lists so every surface sees the same JSON shapes."""
    if isinstance(value, tuple):
        return list(value)
    if isinstance(value, dict):
        return {key: _json_ready(item) for key, item in value.items()}
    return value


# --------------------------------------------------------------- validate
def _walk(partial: Mapping, scope: str) -> Iterable[tuple[str, Any]]:
    """Yield ``(key, value)`` pairs from a partial, nested or flat-dotted mapping.

    Nested (``{"answer": {"mode": "lead"}}``) is what the routes send; flat
    dotted keys (``{"answer.mode": "lead"}``) are accepted so ``kova groups
    set`` uses the same validator. An unknown key is refused, because a typo
    that is silently ignored is a setting that never works.
    """
    tables = _TABLES if scope == "group" else {}
    flat = {s.key for s in (SETTINGS if scope == "group" else MEMBER_SETTINGS)
            if "." not in s.key}
    for key, value in partial.items():
        if not isinstance(key, str):
            raise SettingsError("Settings keys must be text.")
        if key in flat:
            yield key, value
        elif "." in key:
            table, leaf = key.split(".", 1)
            if table not in tables or "." in leaf or leaf not in tables[table]:
                raise SettingsError(f"{key} is not a setting")
            yield key, value
        elif key in tables:
            if not isinstance(value, Mapping):
                raise SettingsError(f"{key} must be a table of settings")
            for leaf, item in value.items():
                if leaf not in tables[key]:
                    raise SettingsError(f"{key}.{leaf} is not a setting")
                yield f"{key}.{leaf}", item
        else:
            raise SettingsError(f"{key} is not a setting")


def _coerce(setting: Setting, value: Any) -> Any:
    """One value, checked against its setting, or a one-line refusal."""
    key = setting.key
    if setting.type == "enum":
        if not isinstance(value, str) or value not in setting.values:
            raise SettingsError(f"{key} must be one of: {', '.join(setting.values)}")
        return value
    if setting.type == "bool":
        if not isinstance(value, bool):
            raise SettingsError(f"{key} must be true or false")
        return value
    if setting.type == "int":
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise SettingsError(f"{key} must be a whole number")
        if isinstance(value, float):
            if not value.is_integer():
                raise SettingsError(f"{key} must be a whole number")
            value = int(value)
        _check_range(setting, value)
        return value
    if setting.type == "money":
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise SettingsError(f"{key} must be an amount in US dollars")
        if not math.isfinite(value):
            raise SettingsError(f"{key} must be an amount in US dollars")
        if value < 0:
            raise SettingsError(f"{key} must be 0 or more")
        return float(value)
    if setting.type == "text":
        if not isinstance(value, str):
            raise SettingsError(f"{key} must be text")
        limit = setting.max_length
        if limit is not None and len(value) > limit:
            raise SettingsError(f"{key} must be at most {limit:,} characters")
        return value
    if setting.type == "path":
        if not isinstance(value, str) or not value.strip():
            raise SettingsError(f"{key} must be a folder path")
        return value
    if setting.type == "bot":
        if not isinstance(value, str):
            raise SettingsError(f"{key} must be a bot id")
        if not value.strip():
            return None  # an empty picker means the default, which is "reset"
        if not BOT_ID.match(value):
            raise SettingsError(f"{key} must be a bot id")
        return value
    if setting.type == "bots":
        if (isinstance(value, (str, bytes, Mapping))
                or not isinstance(value, Iterable)):
            raise SettingsError(f"{key} must be a list of bot ids")
        picked: list[str] = []
        for item in value:
            if not isinstance(item, str) or not BOT_ID.match(item):
                raise SettingsError(f"{key} must be a list of bot ids")
            if item not in picked:
                picked.append(item)
        if len(picked) > MAX_MEMBERS:
            raise SettingsError(f"{key} must name at most {MAX_MEMBERS} bots")
        return picked
    raise SettingsError(f"{key} is not a setting")  # pragma: no cover - typo guard


def _check_range(setting: Setting, value: int | float) -> None:
    low, high = setting.minimum, setting.maximum
    if low is not None and high is not None and not low <= value <= high:
        raise SettingsError(f"{setting.key} must be {low:,}–{high:,}")
    if low is not None and value < low:
        raise SettingsError(f"{setting.key} must be {low:,} or more")
    if high is not None and value > high:
        raise SettingsError(f"{setting.key} must be at most {high:,}")


def _validated(partial: Mapping | None, scope: str) -> dict:
    """The changed keys only, nested, with every ``None`` removed (reset)."""
    if partial is None:
        return {}
    if not isinstance(partial, Mapping):
        raise SettingsError("Settings must be a table.")
    out: dict[str, Any] = {}
    for key, value in _walk(partial, scope):
        if value is None:
            continue  # reset: deleting the key is the reset
        setting = _BY_KEY[(scope, key)]
        _set_path(out, key, _coerce(setting, value))
    return _drop_empty_tables(out)


def validate(partial: Mapping | None) -> dict:
    """Check a partial group setting and return only the keys it sets.

    ``None`` for a key resets it to its default, which is the same as not
    storing it, so the key is left out of the result. Raises
    :class:`SettingsError` with a one-line reason such as
    ``answer.count must be 1–8``.
    """
    return _validated(partial, "group")


def member_validate(partial: Mapping | None) -> dict:
    """The same for one member's overrides (``approvals``, ``muted``, …)."""
    return _validated(partial, "member")


def _default_for(setting: Setting, changed: Mapping) -> Any:
    """The default of one key, which for ``answer.count`` depends on the mode."""
    if setting.key == "answer.count" and _get_path(changed, "answer.mode") == "random":
        return 1
    return _json_ready(setting.default)


def changed(settings: Mapping | None) -> dict:
    """Only the values that differ from their default, for storage.

    A resolved mapping (every key filled in) comes back as just the keys the
    user changed, and an explicit value that equals the default is dropped, so
    a later better default still reaches the group.
    """
    clean = validate(settings)
    out: dict[str, Any] = {}
    for setting in SETTINGS:
        value = _get_path(clean, setting.key)
        if value is not None and value != _default_for(setting, clean):
            _set_path(out, setting.key, copy.deepcopy(value))
    return _drop_empty_tables(out)


# ---------------------------------------------------------------- resolve
def resolve(settings: Mapping | None = None) -> dict:
    """Every setting, with the stored values over the defaults.

    ``answer.count`` defaults to 1 in Random mode and 2 otherwise, as the
    design says; everything else has one default. The result is a fresh
    mapping, safe to mutate.
    """
    clean = validate(settings)
    out = defaults()
    for setting in SETTINGS:
        value = _get_path(clean, setting.key)
        if value is not None:
            _set_path(out, setting.key, copy.deepcopy(value))
    if out["answer"]["mode"] == "random" and _get_path(clean, "answer.count") is None:
        out["answer"]["count"] = 1
    return out


# ----------------------------------------------------------------- members
def _bot_group(bot: Any) -> tuple[str, int, int]:
    """A bot's own ``[group]`` defaults, for the member overrides' defaults."""
    if bot is None:
        return "normal", DEFAULT_MAX_ROUNDS, DEFAULT_MAX_OUTPUT_TOKENS
    group = None
    if isinstance(bot, Mapping):
        group = bot.get("group", bot)
    else:
        group = getattr(bot, "group", None)

    def pick(name: str, fallback: Any) -> Any:
        if isinstance(group, Mapping):
            return group.get(name, fallback)
        return getattr(group, name, fallback) if group is not None else fallback

    answers = pick("answers", "normal")
    if answers not in ("normal", "always"):
        answers = "normal"
    rounds = pick("max_rounds", DEFAULT_MAX_ROUNDS)
    tokens = pick("max_output_tokens", DEFAULT_MAX_OUTPUT_TOKENS)
    if not isinstance(rounds, int) or isinstance(rounds, bool):
        rounds = DEFAULT_MAX_ROUNDS
    if not isinstance(tokens, int) or isinstance(tokens, bool):
        tokens = DEFAULT_MAX_OUTPUT_TOKENS
    return answers, rounds, tokens


def member_resolve(group: Mapping | None = None, member: Mapping | None = None,
                   bot: Any = None) -> dict:
    """One member's effective overrides in one group.

    ``"same"`` follows the group for approvals and the bot for answers; the
    two limits fall back to the bot's own values and are clamped to the hard
    ceilings, so a hand-edited bot file can never raise them. ``bot`` is a
    :class:`kova.bots.BotDefinition`, a mapping with the same shape, or
    ``None`` for the default assistant.
    """
    resolved = resolve(group)
    clean = member_validate(member)
    answers, rounds, tokens = _bot_group(bot)

    approvals = clean.get("approvals", "same")
    if approvals == "same":
        approvals = resolved["approvals"]

    answer_style = clean.get("answers", "same")
    if answer_style == "same":
        answer_style = answers

    rounds = clean.get("max_rounds") or rounds
    tokens = clean.get("max_output_tokens") or tokens
    rounds = max(1, min(int(rounds), MAX_MEMBER_ROUNDS))
    tokens = max(MIN_MEMBER_TOKENS, min(int(tokens), MAX_MEMBER_TOKENS))
    return {
        "approvals": approvals,
        "answers": answer_style,
        "muted": bool(clean.get("muted", False)),
        "max_rounds": rounds,
        "max_output_tokens": tokens,
    }


# ----------------------------------------------------------------- presets
@dataclass(frozen=True)
class Preset:
    """A named set of values. It sets knobs; every knob stays editable."""

    id: str
    name: str
    help: str
    values: Mapping[str, Any] = field(repr=False)


PRESETS: tuple[Preset, ...] = (
    Preset("ask-one", "Ask one",
           "@mentioned bots answer, otherwise the lead.",
           {"answer": {"mode": "lead"},
            "replies": {"mode": "mentioned", "max_hops": 3}}),
    Preset("panel", "Panel",
           "Everyone answers without seeing the others; no bot replies to a bot.",
           {"answer": {"mode": "everyone"},
            "replies": {"mode": "off"},
            "see_earlier_replies": False}),
    Preset("team", "Team with a lead",
           "The lead decides who answers your message.",
           {"answer": {"mode": "auto", "auto_max": 2},
            "replies": {"mode": "mentioned", "max_hops": 3}}),
    Preset("debate", "Debate",
           "Everyone answers in turn and argues the question out.",
           {"answer": {"mode": "everyone"},
            "replies": {"mode": "turns", "max_hops": 4},
            "instructions": DEBATE_INSTRUCTIONS}),
)

def _flatten(partial: Mapping, prefix: str = "") -> list[str]:
    keys: list[str] = []
    for key, value in partial.items():
        path = f"{prefix}{key}"
        if isinstance(value, Mapping):
            keys.extend(_flatten(value, f"{path}."))
        else:
            keys.append(path)
    return keys


#: The keys any preset may set. Picking a preset writes these and resets the
#: ones its own set leaves out, so switching from Debate to Panel drops
#: Debate's instructions; approvals, folders, limits and the rest are never
#: touched, so picking a preset is always safe.
PRESET_MANAGED: tuple[str, ...] = tuple(dict.fromkeys(
    key for preset in PRESETS for key in _flatten(preset.values)))

_BY_ID = {preset.id: preset for preset in PRESETS}


def preset(preset_id: str) -> Preset:
    """One preset by id, or a one-line refusal."""
    found = _BY_ID.get(preset_id)
    if found is None:
        raise SettingsError(
            f"preset must be one of: {', '.join(p.id for p in PRESETS)}")
    return found


def preset_values(preset_id: str) -> dict:
    """A preset's own values, validated, as a partial mapping."""
    return validate(preset(preset_id).values)


def _without(target: dict, key: str) -> None:
    if "." in key:
        table, leaf = key.split(".", 1)
        nested = target.get(table)
        if isinstance(nested, dict):
            nested.pop(leaf, None)
    else:
        target.pop(key, None)


def apply_preset(settings: Mapping | None, preset_id: str) -> dict:
    """Pick a preset: write its values over the settings the user keeps.

    Only the keys presets manage are replaced; approvals, folders, limits and
    what bots share stay exactly as they were.
    """
    merged = validate(settings)
    for key in PRESET_MANAGED:
        _without(merged, key)
    values = preset_values(preset_id)
    for key in PRESET_MANAGED:
        value = _get_path(values, key)
        if value is not None:
            _set_path(merged, key, copy.deepcopy(value))
    return _drop_empty_tables(merged)


def matching_preset(settings: Mapping | None) -> str:
    """The preset these settings still match, or ``"custom"``.

    Only the keys presets manage are compared, so changing the lead,
    approvals, a folder or a limit never hides the preset the group was
    started as.
    """
    resolved = resolve(settings)
    for candidate in PRESETS:
        base = resolve(candidate.values)
        if all(_get_path(resolved, key) == _get_path(base, key)
               for key in PRESET_MANAGED):
            return candidate.id
    return CUSTOM


# ------------------------------------------------------------------ schema
def schema() -> dict:
    """``GET /group-settings``: every knob, its bounds, and the presets."""
    return {
        "version": 1,
        "sections": list(SECTIONS),
        "member_sections": list(MEMBER_SECTIONS),
        "settings": [setting.public() for setting in SETTINGS],
        "member_settings": [setting.public() for setting in MEMBER_SETTINGS],
        "presets": [{"id": preset.id, "name": preset.name, "help": preset.help,
                     "values": preset_values(preset.id)} for preset in PRESETS],
        "custom": CUSTOM,
    }


__all__ = [
    "ANSWER_MODES", "APPROVALS", "CUSTOM", "DEBATE_INSTRUCTIONS",
    "DEFAULT_MAX_OUTPUT_TOKENS", "DEFAULT_MAX_ROUNDS", "FOLDER_MODES",
    "MAX_BOT_TURNS", "MAX_HOPS", "MAX_INSTRUCTIONS", "MAX_MEMBERS",
    "MAX_MEMBER_ROUNDS", "MAX_MEMBER_TOKENS", "MAX_PING_PONG", "MAX_SECONDS",
    "MAX_TOKENS", "MEMBER_APPROVALS", "MEMBER_ANSWERS", "MEMBER_SECTIONS",
    "MEMBER_SETTINGS", "MENTION_MODES", "MIN_MEMBER_TOKENS", "MIN_SECONDS",
    "MIN_TOKENS", "PRESETS", "PRESET_MANAGED", "REPLY_MODES", "SECTIONS",
    "SETTINGS", "SHARE_WORK", "Preset", "Setting", "SettingsError",
    "apply_preset", "changed", "defaults", "matching_preset", "member_resolve",
    "member_validate", "preset", "preset_values", "resolve", "schema",
    "validate",
]
