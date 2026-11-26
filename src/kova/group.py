"""The group runner: who answers a message in a group chat, one bot at a time (task 2.5).

A user message in a group is routed with :func:`kova.mentions.route` (the
per-message override, ``@all``, explicit mentions, then the group's own mode),
and the picked members answer **sequentially**, each with its own view of the
shared transcript (:func:`kova.group_context.view_for`). After each reply the
runner decides what happens next from ``replies.mode``:

- ``off``: nothing; only the user's message starts bots.
- ``mentioned``: members @mentioned in the reply are queued as hand-offs.
- ``turns``: after the first pass the members keep answering in order until a
  guard stops them or every member passes in a row.

Before any bot turn the guards from design 4.4 run in order: hops, bot turns,
ping-pong, tokens, cost and time; a repeated reply also ends the chain. A guard
that fires emits ``group_limit`` with the bots that were still queued and stops;
nothing is deleted.

Every event a bot's turn yields is forwarded with its ``author`` and
``bot_turn_id``. A member's own ``done`` becomes ``bot_turn_end``; the whole
user turn ends with one ``done`` carrying ``bot_turns`` and the summed usage.

``auto`` (the lead picks who answers with one routing call) is not built yet:
the lead answers, and ``group_route`` says so.
"""

from __future__ import annotations

import re
import time
from collections.abc import AsyncIterator, Callable, Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any
from uuid import uuid4

from kova import group_settings, mentions
from kova.actors import DEFAULT_ACTOR_ID, Actor

PASS = "(pass)"

#: Keys that older builds stored before the schema left them out.
_LEGACY_KEYS = ("retry", "memory")


@dataclass
class _Queued:
    author: str
    reason: str
    hop: int
    triggered_by: str | None = None


def resolved_settings(stored: Mapping | None) -> dict:
    """A group's resolved settings, reading rows older builds wrote, too."""
    stored = dict(stored or {})
    try:
        return group_settings.resolve(stored)
    except group_settings.SettingsError:
        for key in _LEGACY_KEYS:
            stored.pop(key, None)
        try:
            return group_settings.resolve(stored)
        except group_settings.SettingsError:
            return group_settings.resolve({})


def _normalised(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().casefold()


def _is_pass(text: str) -> bool:
    return _normalised(text).strip(".!") in {"(pass)", "pass"}


def _add_usage(total: dict, usage: Mapping | None) -> None:
    if not usage:
        return
    for key in ("prompt_tokens", "completion_tokens", "ms_total"):
        total[key] = total.get(key, 0) + int(usage.get(key) or 0)
    cost = usage.get("cost_usd")
    if cost is not None:
        total["cost_usd"] = round(total.get("cost_usd", 0.0) + float(cost), 6)
    if usage.get("estimated"):
        total["estimated"] = True


class GroupRunner:
    """Runs one user message in a group chat through its members.

    ``agent`` is the :class:`kova.agent.Agent`; ``bot_loader(bot_id)`` returns
    a :class:`kova.bots.BotDefinition` or ``None`` (the default assistant, or a
    bot that was deleted). ``clock`` is injectable for the time guard's tests.
    """

    def __init__(self, agent: Any, config: Any, bot_loader: Callable[[str], Any], *,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.agent = agent
        self.store = agent.store
        self.config = config
        self.bot_loader = bot_loader
        self.clock = clock
        #: Messages routed per chat, for the ``count`` rotation.
        self._rotation: dict[str, int] = {}

    # ------------------------------------------------------------ members
    def _crew(self, chat: Mapping, settings: Mapping) -> tuple[list[mentions.Member], dict[str, Actor], dict[str, str]]:
        crew: list[mentions.Member] = []
        actors: dict[str, Actor] = {}
        handles: dict[str, str] = {}
        folder_path = settings.get("folder_path")
        for index, row in enumerate(self.store.chat_members(chat["id"])):
            bot_id = row["bot_id"]
            bot = self.bot_loader(bot_id)
            if bot is None and bot_id != DEFAULT_ACTOR_ID:
                continue  # a deleted or broken bot: its history stays, it never answers
            try:
                member = group_settings.member_resolve(settings, row.get("settings") or {}, bot)
            except group_settings.SettingsError:
                member = group_settings.member_resolve(settings, {}, bot)
            if bot is None:
                name = getattr(self.config, "persona_name", None) or "Kova"
                actor = Actor.for_chat(chat, None, name=name)
                handle = DEFAULT_ACTOR_ID
            else:
                actor = Actor.for_chat(chat, bot, files_path=getattr(self.config, "files_path", None))
                name, handle = bot.name, bot.handle
                # A shared folder is the group's (or the chat's); "own" keeps the bot's.
                if settings.get("folder") != "own":
                    actor = replace(actor, folder=Path(folder_path).expanduser()
                                    if folder_path else None)
                actor = replace(actor, max_rounds=member["max_rounds"],
                                max_output_tokens=member["max_output_tokens"])
            actors[bot_id] = actor
            handles[bot_id] = handle
            crew.append(mentions.Member(id=bot_id, handle=handle, name=name,
                                        position=index + 1, muted=bool(row.get("muted")),
                                        answers=member["answers"]))
        return crew, actors, handles

    @staticmethod
    def _member_lines(crew: list[mentions.Member], me: str, descriptions: Mapping[str, str]) -> list[str]:
        lines = []
        for member in crew:
            if member.id == me:
                continue
            about = descriptions.get(member.id, "")
            lines.append(f"@{member.handle} ({member.name})" + (f": {about}" if about else ""))
        return lines

    # ---------------------------------------------------------------- run
    async def run(self, chat_id: str, text: str, *, model: str | None = None,
                  steering: Any = None, attachment_ids: list[str] | None = None,
                  capabilities: frozenset[str] | None = None,
                  override: Mapping | None = None, seed: Any = None) -> AsyncIterator[dict]:
        chat = self.store.get_chat(chat_id)
        if chat is None:
            yield {"type": "error", "message": "chat was deleted"}
            return
        settings = resolved_settings(chat.get("group_settings"))
        crew, actors, handles = self._crew(chat, settings)
        descriptions = {}
        for member in crew:
            bot = self.bot_loader(member.id) if member.id != DEFAULT_ACTOR_ID else None
            descriptions[member.id] = (getattr(bot, "description", "") or "").strip()

        turn = self._rotation.get(chat_id, 0)
        self._rotation[chat_id] = turn + 1
        try:
            plan = mentions.route(text, crew, settings, override=override, seed=seed, turn=turn)
        except group_settings.SettingsError as exc:
            yield {"type": "error", "message": f"Invalid answer override: {exc}"}
            return

        queue = [_Queued(pick.author, pick.reason, pick.hop) for pick in plan.picks]
        if plan.decides and not any(item.author == plan.decides for item in queue):
            # The lead's routing call (auto) is not built yet: the lead answers.
            queue.insert(0, _Queued(plan.decides, mentions.LEAD, 0))
            yield {"type": "group_route", "by": plan.decides, "picked": [plan.decides],
                   "why": "The lead answers.", "usage": None}

        def plan_event() -> dict:
            return {"type": "group_plan", "mode": plan.mode, "override": plan.override,
                    "queue": [{"author": item.author, "reason": item.reason, "hop": item.hop}
                              for item in queue]}

        yield plan_event()
        turn_id = uuid4().hex
        if not queue:
            # Nobody was asked: the message is kept as context for later.
            message_id = self.store.add_message(chat_id, "user", text, turn_id=turn_id, variant=1)
            if attachment_ids:
                self.store.mark_attachments_used(chat_id, list(attachment_ids))
            yield {"type": "notice", "kind": "group_nobody",
                   "notice": "Nobody was asked to answer. @mention a bot, or ask the lead."}
            yield {"type": "done", "message_id": None, "user_message_id": message_id,
                   "created": None, "model": None, "usage": {}, "bot_turns": []}
            return

        limits = settings["limits"]
        replies = settings["replies"]
        reply_mode = replies["mode"]
        started = self.clock()
        bot_turns: list[dict] = []
        total: dict = {}
        replies_seen: set[str] = set()
        handoffs: list[tuple[str, str]] = []
        passes_in_a_row = 0
        first_pass = {item.author for item in queue}
        record_user = True
        last: dict = {}
        unmuted = [member for member in crew if not member.muted]
        turns_hop = max((item.hop for item in queue), default=0)
        turns_index = 0
        stop: dict | None = None

        while queue or (reply_mode == "turns" and unmuted and stop is None):
            if not queue:
                # Take turns: everyone again, in order, each turn one more hop.
                if passes_in_a_row >= len(unmuted):
                    break
                turns_hop += 1
                member = unmuted[turns_index % len(unmuted)]
                turns_index += 1
                queue.append(_Queued(member.id, "turns", turns_hop))
            item = queue[0]
            stop = self._guard(item, limits, replies, reply_mode, len(bot_turns), total,
                               started, handoffs)
            if stop is not None:
                stop["dropped"] = [queued.author for queued in queue]
                yield {"type": "group_limit", **stop}
                break
            queue.pop(0)
            if item.reason == mentions.HANDOFF and item.triggered_by:
                handoffs.append((item.triggered_by, item.author))
            actor = actors[item.author]
            group = {"settings": settings, "handles": handles,
                     "members": self._member_lines(crew, item.author, descriptions)}
            yield {"type": "bot_turn_start", "author": item.author,
                   "author_name": actor.name, "reason": item.reason, "hop": item.hop,
                   "triggered_by": item.triggered_by,
                   "model": actor.model}
            reply: list[str] = []
            status, error, bot_turn_id, usage = "done", None, None, None
            async for event in self.agent.run_turn(
                    chat_id, text, model=model, steering=steering,
                    attachment_ids=attachment_ids, capabilities=capabilities,
                    record_user=record_user, turn_id=turn_id, variant=1,
                    actor=actor, hop=item.hop, group=group):
                kind = event.get("type")
                bot_turn_id = event.get("bot_turn_id", bot_turn_id)
                if kind == "text":
                    reply.append(event.get("text") or "")
                elif kind in ("tool_call", "steer"):
                    reply.clear()  # only the reply after the last tool call counts
                if kind == "done":
                    usage = event.get("usage")
                    last = event
                    continue
                if kind == "error":
                    status, error = "error", event.get("message")
                    continue
                yield event
            record_user = record_user and not self.store.turn_has_user_message(chat_id, turn_id)
            final = "".join(reply).strip()
            passed = status == "done" and _is_pass(final)
            if passed:
                status = "passed"
            _add_usage(total, usage)
            entry = {"author": item.author, "bot_turn_id": bot_turn_id, "usage": usage}
            bot_turns.append(entry)
            end = {"type": "bot_turn_end", **entry, "status": status,
                   "message_id": last.get("message_id") if status != "error" else None}
            if error:
                end["message"] = error
            yield end
            if status != "done":
                passes_in_a_row += 1 if passed else 0
                continue
            passes_in_a_row = 0
            key = _normalised(final)
            if key and key in replies_seen:
                yield {"type": "group_limit", "limit": "repeat", "value": None, "max": None,
                       "dropped": [queued.author for queued in queue]}
                stop = {"limit": "repeat"}
                break
            replies_seen.add(key)
            if reply_mode == "mentioned":
                handed = mentions.handoff(final, crew, settings, author=item.author,
                                          hop=item.hop + 1)
                added = False
                for pick in handed.picks:
                    if any(queued.author == pick.author for queued in queue):
                        continue
                    queue.append(_Queued(pick.author, pick.reason, pick.hop, item.author))
                    added = True
                if added:
                    yield plan_event()
            elif reply_mode == "turns" and not queue and item.author in first_pass:
                # The first pass is over: the turns start after its last member.
                turns_index = next((i + 1 for i, m in enumerate(unmuted) if m.id == item.author), 0)
                first_pass = set()

        done = {"type": "done", "message_id": last.get("message_id"),
                "created": last.get("created"), "model": last.get("model"),
                "usage": total, "bot_turns": bot_turns}
        for key in ("budget_warning",):
            if key in last:
                done[key] = last[key]
        yield done

    # ------------------------------------------------------------- guards
    def _guard(self, item: _Queued, limits: Mapping, replies: Mapping, reply_mode: str,
               turns: int, total: Mapping, started: float,
               handoffs: list[tuple[str, str]]) -> dict | None:
        """The first guard that stops this bot turn from starting, or None."""
        if item.hop > 0 and item.hop > replies["max_hops"]:
            return {"limit": "max_hops", "value": item.hop, "max": replies["max_hops"]}
        if turns >= limits["max_bot_turns"]:
            return {"limit": "max_bot_turns", "value": turns, "max": limits["max_bot_turns"]}
        if reply_mode != "turns" and item.reason == mentions.HANDOFF and item.triggered_by:
            pair = frozenset((item.triggered_by, item.author))
            streak = 1
            for before in reversed(handoffs):
                if frozenset(before) != pair:
                    break
                streak += 1
            if streak > replies["ping_pong"]:
                return {"limit": "ping_pong", "value": streak, "max": replies["ping_pong"]}
        tokens = int(total.get("prompt_tokens", 0)) + int(total.get("completion_tokens", 0))
        if tokens >= limits["max_tokens"]:
            return {"limit": "max_tokens", "value": tokens, "max": limits["max_tokens"]}
        cost_cap = limits.get("max_cost_usd")
        spent = total.get("cost_usd")
        # Only priced models spend money; a free turn never reaches a 0 cap.
        if cost_cap is not None and spent is not None and spent > 0 and spent >= cost_cap:
            return {"limit": "max_cost_usd", "value": spent, "max": cost_cap}
        elapsed = self.clock() - started
        if elapsed >= limits["max_seconds"]:
            return {"limit": "max_seconds", "value": round(elapsed), "max": limits["max_seconds"]}
        return None


__all__ = ["GroupRunner", "PASS", "resolved_settings"]
