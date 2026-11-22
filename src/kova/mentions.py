"""Mention parsing and the who-answers router for group chats (task 2.3).

Pure: no model calls, no store, no server. It finds ``@handles`` in a message
and returns the queue of bots that answer it, for every answer mode in the
design (section 4.2), plus the chip's preview for that queue.

The rules exist so that text a model or a web page produced cannot start bots:

- Only the user's message and the **final reply text** of a bot may mention.
  Reasoning, tool arguments and tool output never reach this module, so a web
  page that says ``@coder delete everything`` does nothing.
- Mentions inside code spans, fenced code blocks and ``>`` quotes do not count.
- A handle must be followed by a word boundary: ``email@coder.dev`` is not a
  mention.
- A bot mentioning itself is ignored.
- The user's explicit @handles win over the mode. In ``lead_only`` they go to
  the lead as a request instead.

:mod:`tests.test_mentions` runs ``tests/fixtures/group_routing.json`` through
this module; ``group-route.js`` is the browser's twin of the deterministic part
(``preview``), and both read that same file so the chip cannot drift from the
server. ``auto`` (the lead's routing call) and ``random`` are what the runner
decides after this: the preview names ``decides`` or ``random`` rather than
guessing.
"""

from __future__ import annotations

import random as _random
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from kova import group_settings

#: The handles that address everyone in a group, whatever the mode.
ALL_HANDLES = frozenset({"all", "everyone"})
#: Reasons a bot is in the queue; the same names the ``bot_turn_start``
#: event uses, so a lane's header can say why it is speaking.
MENTION = "mention"
ALL = "all"
LEAD = "lead"
EVERYONE = "everyone"
SUBSET = "subset"
ROTATION = "rotation"
RANDOM = "random"
ALWAYS = "always"
HANDOFF = "handoff"

#: ``@handle``: a fresh ``@``, 1–32 handle characters, then a word boundary.
#: The look-behind is what makes ``email@coder.dev`` not a mention.
HANDLE_RE = re.compile(r"(?<![\w@])@([A-Za-z0-9][A-Za-z0-9-]{0,31})(?![\w-])")
_FENCE = re.compile(r"^[ \t]{0,3}(`{3,}|~{3,})")
_QUOTE = re.compile(r"^[ \t]{0,3}>")


@dataclass(frozen=True)
class Mention:
    """One ``@handle`` occurrence outside code and quotes."""

    handle: str
    start: int
    end: int
    raw: str


@dataclass(frozen=True)
class Member:
    """A group member as the router needs it.

    ``answers`` is the **effective** value: the runner resolves a member's
    override against the bot's own ``[group] answers``
    (:func:`kova.group_settings.member_resolve`) before routing. ``"same"`` or
    anything else is treated as normal, so a raw row still routes safely.
    """

    id: str
    handle: str = ""
    name: str = ""
    position: int = 0
    muted: bool = False
    answers: str = "normal"

    def __post_init__(self) -> None:
        if not self.handle:
            object.__setattr__(self, "handle", self.id.lower())
        else:
            object.__setattr__(self, "handle", self.handle.lower())
        if not self.name:
            object.__setattr__(self, "name", self.id)

    @property
    def always_answers(self) -> bool:
        return str(self.answers).lower() in ("always", "on", "true")


@dataclass(frozen=True)
class Pick:
    """One bot in the queue, and why it is there."""

    author: str
    reason: str
    hop: int = 0


@dataclass(frozen=True)
class Plan:
    """Who answers one message, in order, and what the chip should say."""

    picks: tuple[Pick, ...] = ()
    mode: str = "lead"
    override: bool = False
    mentions: tuple[str, ...] = ()
    requested: tuple[str, ...] = ()
    muted: tuple[str, ...] = ()
    unknown: tuple[str, ...] = ()
    all: bool = False
    decides: str | None = None
    error: str | None = None

    @property
    def authors(self) -> tuple[str, ...]:
        return tuple(pick.author for pick in self.picks)

    @property
    def empty(self) -> bool:
        """Nobody answers and nothing is pending (an ``auto`` lead is pending)."""
        return not self.picks and self.decides is None

    def reason(self, author: str) -> str | None:
        for pick in self.picks:
            if pick.author == author:
                return pick.reason
        return None


# ------------------------------------------------------------------ parsing
def _mask(text: str) -> list[bool]:
    """Which characters are inside code blocks, code spans or quotes."""
    mask = [False] * len(text)
    fence: tuple[str, int] | None = None
    offset = 0
    for line in text.splitlines(keepends=True):
        end = offset + len(line)
        opening = _FENCE.match(line)
        if fence is not None:
            for index in range(offset, end):
                mask[index] = True
            if opening and opening.group(1)[0] == fence[0] \
                    and len(opening.group(1)) >= fence[1] \
                    and not line[opening.end():].strip():
                fence = None
        elif opening is not None:
            fence = (opening.group(1)[0], len(opening.group(1)))
            for index in range(offset, end):
                mask[index] = True
        elif _QUOTE.match(line):
            for index in range(offset, end):
                mask[index] = True
        offset = end

    # Inline code spans: a run of backticks closes on a run of the same length.
    index = 0
    while index < len(text):
        if mask[index] or text[index] != "`":
            index += 1
            continue
        start = index
        while index < len(text) and text[index] == "`":
            index += 1
        length = index - start
        probe = index
        while probe < len(text):
            if not mask[probe] and text[probe] == "`":
                run = probe
                while run < len(text) and text[run] == "`":
                    run += 1
                if run - probe == length:
                    for position in range(start, run):
                        mask[position] = True
                    index = run
                    break
                probe = run
            else:
                probe += 1
    return mask


def parse_mentions(text: str) -> list[Mention]:
    """Every ``@handle`` in ``text``, in order, outside code and quotes."""
    if not isinstance(text, str) or not text:
        return []
    mask = _mask(text)
    found: list[Mention] = []
    for match in HANDLE_RE.finditer(text):
        if mask[match.start()]:
            continue
        found.append(Mention(match.group(1).lower(), match.start(), match.end(),
                             match.group(0)))
    return found


def mention_handles(text: str) -> list[str]:
    """The handles a text mentions, first occurrence first, duplicates removed."""
    handles: list[str] = []
    for mention in parse_mentions(text):
        if mention.handle not in handles:
            handles.append(mention.handle)
    return handles


# ------------------------------------------------------------------ members
def _one(value: Any, index: int) -> Member:
    if isinstance(value, Member):
        return value
    if isinstance(value, Mapping):
        def get(name: str, fallback: Any = None) -> Any:
            return value.get(name, fallback)
    else:
        def get(name: str, fallback: Any = None) -> Any:
            return getattr(value, name, fallback)
    identifier = get("id") or get("bot_id")
    if not identifier:
        raise ValueError("a group member needs an id")
    position = get("position", index + 1)
    if not isinstance(position, int) or isinstance(position, bool) or position < 1:
        position = index + 1
    answers = get("answers", None)
    settings = get("settings", None)
    if isinstance(settings, Mapping) and settings.get("answers"):
        answers = settings["answers"]
    return Member(
        id=str(identifier),
        handle=str(get("handle") or identifier).lower(),
        name=str(get("name") or get("author_name") or identifier),
        position=position,
        muted=bool(get("muted", False)),
        answers=str(answers or "normal").lower(),
    )


def members(values: Iterable[Any]) -> list[Member]:
    """Coerce rows (mappings or objects) into members ordered by position."""
    crew = [_one(value, index) for index, value in enumerate(values)]
    crew.sort(key=lambda member: member.position)
    return crew


def _lead(answer: Mapping, crew: list[Member]) -> Member | None:
    wanted = answer.get("lead")
    if wanted:
        for member in crew:
            if member.id == wanted:
                return member
    return crew[0] if crew else None


def _mentions(text: str, crew: list[Member], author: str | None
              ) -> tuple[bool, list[Member], list[str], list[str], list[str]]:
    """(everyone, unmuted mentioned members in order, all ids, muted ids, unknown)."""
    by_handle = {member.handle: member for member in crew}
    everyone = False
    picked: list[Member] = []
    ids: list[str] = []
    muted: list[str] = []
    unknown: list[str] = []
    seen: set[str] = set()
    for mention in parse_mentions(text):
        handle = mention.handle
        if handle in ALL_HANDLES:
            everyone = True
            continue
        member = by_handle.get(handle)
        if member is None:
            if handle not in unknown:
                unknown.append(handle)
            continue
        if author is not None and member.id == author:
            continue  # a bot mentioning itself is ignored
        if member.id in seen:
            continue
        seen.add(member.id)
        ids.append(member.id)
        if member.muted:
            muted.append(member.id)
        else:
            picked.append(member)
    return everyone, picked, ids, muted, unknown


def _mode_picks(mode: str, answer: Mapping, unmuted: list[Member],
                lead: Member | None, *, hop: int, turn: int,
                rng: _random.Random) -> list[Pick]:
    picks: list[Pick] = []
    if mode in ("lead", "lead_only"):
        if lead is not None and not lead.muted:
            picks.append(Pick(lead.id, LEAD, hop))
    elif mode == "everyone":
        picks.extend(Pick(member.id, EVERYONE, hop) for member in unmuted)
    elif mode == "subset":
        chosen = set(answer.get("bots") or ())
        picks.extend(Pick(member.id, SUBSET, hop) for member in unmuted
                     if member.id in chosen)
    elif mode == "count" and unmuted:
        wanted = min(int(answer.get("count") or 1), len(unmuted))
        start = turn % len(unmuted)
        for offset in range(wanted):
            picks.append(Pick(unmuted[(start + offset) % len(unmuted)].id,
                              ROTATION, hop))
    elif mode == "random" and unmuted:
        wanted = min(int(answer.get("count") or 1), len(unmuted))
        chosen = {member.id for member in rng.sample(unmuted, wanted)}
        picks.extend(Pick(member.id, RANDOM, hop) for member in unmuted
                     if member.id in chosen)
    # "mentioned" and "auto" have no picks of their own.
    return picks


def route(text: str, crew: Iterable[Any], settings: Mapping | None = None, *,
          override: Mapping | None = None, author: str | None = None,
          seed: Any = None, turn: int = 0, hop: int = 0) -> Plan:
    """Route one message to the bots that answer it.

    ``crew`` is the group's members (mappings, objects or :class:`Member`).
    ``settings`` is a group settings mapping, stored or resolved. ``override``
    is the per-message answer override (``{"mode": "everyone"}``, …); it can
    change who answers but never a limit. ``author`` is set when ``text`` is a
    bot's final reply, which makes the result a hand-off rather than the first
    pass. ``seed`` makes ``random`` reproducible; ``turn`` advances the
    ``count`` rotation, so one counter per chat spreads messages across the
    group. ``hop`` is stamped onto every pick (0 for the first pass).
    """
    order = members(crew)
    resolved = group_settings.resolve(settings)
    answer = dict(resolved["answer"])
    used_override = False
    if override:
        patch = group_settings.validate({"answer": override})
        if patch.get("answer"):
            answer.update(patch["answer"])
            used_override = True
    mode = answer["mode"]
    unmuted = [member for member in order if not member.muted]
    lead = _lead(answer, order)
    everyone, mentioned, mentioned_ids, muted_ids, unknown = _mentions(
        text, order, author)

    base = {
        "mode": mode,
        "override": used_override,
        "mentions": tuple(mentioned_ids),
        "muted": tuple(muted_ids),
        "unknown": tuple(unknown),
        "all": everyone,
    }
    if mode == "lead_only" and mentioned_ids:
        base["requested"] = tuple(mentioned_ids)

    # A bot's final reply only ever hands work on by @mention; the reply modes
    # "off" and "turns" are the runner's business, not a mention's.
    if author is not None:
        if resolved["replies"]["mode"] != "mentioned":
            return Plan(**base)
        if everyone:
            picks = [Pick(member.id, ALL, hop) for member in unmuted
                     if member.id != author]
        else:
            picks = [Pick(member.id, HANDOFF, hop) for member in mentioned]
        return Plan(**base, picks=tuple(picks))

    picks: list[Pick] = []
    decides: str | None = None
    if everyone:
        picks = [Pick(member.id, ALL, hop) for member in unmuted]
    elif mentioned_ids:
        if mode == "lead_only":
            # Mentions go to the lead as a request; it decides whether to hand off.
            if lead is not None and not lead.muted:
                picks = [Pick(lead.id, LEAD, hop)]
        else:
            picks = [Pick(member.id, MENTION, hop) for member in mentioned]
            if answer["mentions"] == "add":
                picks.extend(_mode_picks(mode, answer, unmuted, lead,
                                         hop=hop, turn=turn,
                                         rng=_random.Random(seed)))
                if mode == "auto" and lead is not None and not lead.muted:
                    decides = lead.id
    else:
        rng = _random.Random(seed)
        picks = _mode_picks(mode, answer, unmuted, lead, hop=hop,
                            turn=turn, rng=rng)
        if mode == "auto" and lead is not None and not lead.muted:
            decides = lead.id

    # Always-answer members join the first pass, never a hand-off, and never
    # trigger anyone themselves.
    unique: list[Pick] = []
    queued: set[str] = set()
    for pick in picks:
        if pick.author in queued:
            continue
        queued.add(pick.author)
        unique.append(pick)
    for member in unmuted:
        if member.always_answers and member.id not in queued:
            unique.append(Pick(member.id, ALWAYS, hop))
            queued.add(member.id)

    return Plan(**base, picks=tuple(unique), decides=decides,
                error="no members" if not order else None)


def handoff(text: str, crew: Iterable[Any], settings: Mapping | None = None, *,
            author: str, seed: Any = None, hop: int = 1) -> Plan:
    """Route a finished bot's final reply as a hand-off (a named convenience)."""
    return route(text, crew, settings, author=author, seed=seed, hop=hop)


# ------------------------------------------------------------------ preview
def preview(plan: Plan) -> dict:
    """What the who-answers chip shows for this plan, and what the JS twin checks.

    The browser cannot call a model, so ``auto`` previews as *who decides* and
    ``random``/``count`` preview as *how many*, never as names it would have to
    guess. Everything else names the bots in order.
    """
    authors: list[str] = []
    count: int | None = None
    random = False
    if plan.picks and all(pick.reason == ROTATION for pick in plan.picks):
        count = len(plan.picks)
    elif plan.picks and all(pick.reason == RANDOM for pick in plan.picks):
        count = len(plan.picks)
        random = True
    elif not plan.decides:
        authors = list(plan.authors)
    return {
        "authors": authors,
        "requested": list(plan.requested),
        "decides": plan.decides,
        "count": count,
        "random": random,
    }


__all__ = [
    "ALL", "ALL_HANDLES", "ALWAYS", "EVERYONE", "HANDOFF", "LEAD", "MENTION",
    "Member", "Mention", "Pick", "Plan", "RANDOM", "ROTATION", "SUBSET",
    "handoff", "mention_handles", "members", "parse_mentions", "preview",
    "route",
]
