"""Build a model-facing group transcript without changing the stored rows."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from copy import deepcopy
from dataclasses import dataclass, field
from itertools import groupby
from typing import TYPE_CHECKING

from kova.actors import DEFAULT_ACTOR_ID

if TYPE_CHECKING:
    from kova.actors import Actor
    from kova.bots import BotDefinition

_SHARED_CHARS = 2_000


def _quote(label: str, text: str) -> str:
    lines = (label + ": " + text[:_SHARED_CHARS]).splitlines()
    return "\n".join("> " + line for line in lines)


@dataclass
class _SharedWork:
    calls: dict[str, str] = field(default_factory=dict)
    results: list[tuple[str, str]] = field(default_factory=list)
    reasoning: str = ""

    def add(self, row: Mapping, share_work: str) -> None:
        for call in row.get("tool_calls") or ():
            self.calls[call["id"]] = call["function"]["name"]
        if share_work != "replies" and row["role"] == "tool":
            text = (row.get("content") or "")[:_SHARED_CHARS]
            self.results.append((row["tool_call_id"], text))
        if share_work == "everything":
            reasoning = (row.get("reasoning") or row.get("reasoning_content")
                         or (row.get("content") if row["role"] == "reasoning" else None))
            if reasoning:
                if self.reasoning:
                    self.reasoning += "\n"
                self.reasoning = (self.reasoning + reasoning)[:_SHARED_CHARS]

    def describe(self, handle: str, share_work: str) -> list[str]:
        if share_work == "replies":
            if not self.calls:
                return []
            count = len(self.calls)
            tools = "tool" if count == 1 else "tools"
            return [f"(@{handle} used {count} {tools}: {', '.join(self.calls.values())})"]
        lines = [_quote(self.calls.get(ident, "Tool result"), text)
                 for ident, text in self.results]
        if share_work == "everything" and self.reasoning:
            lines.append(_quote("Reasoning", self.reasoning))
        return lines


def _as_user(content: str) -> dict:
    return {"role": "user", "content": content}


def _merge_user_rows(messages: list[dict]) -> list[dict]:
    merged: list[dict] = []
    for is_user, batch in groupby(messages, key=lambda message: message["role"] == "user"):
        if is_user:
            merged.append(_as_user("\n\n".join(message["content"] for message in batch)))
        else:
            merged.extend(batch)
    return merged


def _current_turn_start(rows: list[Mapping]) -> int:
    latest = next((i for i in range(len(rows) - 1, -1, -1)
                   if rows[i]["role"] == "user"), len(rows))
    turn_id = rows[latest].get("turn_id") if latest < len(rows) else None
    if turn_id is None:
        return latest
    # A steer adds another user row with the same turn id.
    return next(i for i, row in enumerate(rows)
                if row["role"] == "user" and row.get("turn_id") == turn_id)


def view_for(bot: str | Actor | BotDefinition, rows: Iterable[Mapping], settings: Mapping, *,
             context_tokens: int | None = None, reserved_chars: int = 0,
             handles: Mapping[str, str] | None = None, summary: str | None = None) -> list[dict]:
    """Project chronological stored rows into one bot's chat-completions history.

    Pass the model's context and reserved prompt size to trim the history.
    Handles are keyed by bot id; a missing handle falls back to that id. The
    optional per-chat compaction summary is shared unchanged across bots.
    """
    from kova.agent import repair_tool_pairs, trim_history

    bot_id = bot if isinstance(bot, str) else bot.id
    handles = handles or {}
    rows = [row for row in rows if row.get("active", True)]
    current = _current_turn_start(rows)
    share_work = settings.get("share_work")
    if share_work not in ("tools", "everything"):
        share_work = "replies"
    see_earlier = settings.get("see_earlier_replies", True)
    messages = [_as_user("Conversation summary (earlier context):\n" + summary)] if summary else []
    latest_user = None
    pending: dict[tuple[str, str | None], _SharedWork] = {}
    for index, row in enumerate(rows):
        role = row["role"]
        content = row.get("content") or ""
        if role == "user":
            # Explicit bot_turn_ids survive a steer. Unattributed work cannot
            # carry across a user message into a later bot reply.
            pending = {key: work for key, work in pending.items() if key[1] is not None}
            latest_user = len(messages)
            messages.append(_as_user("@you: " + content))
            continue
        if role not in {"assistant", "tool", "reasoning"}:
            continue

        author = row.get("author") or DEFAULT_ACTOR_ID
        is_reply = role == "assistant" and not row.get("tool_calls") and bool(content.strip())
        is_pass = is_reply and content.strip() == "(pass)"
        if author == bot_id:
            if role != "reasoning" and not is_pass:
                messages.append({key: deepcopy(row[key]) for key in
                                 ("role", "content", "tool_calls", "tool_call_id") if key in row})
            continue
        if not see_earlier and index >= current:
            continue

        key = author, row.get("bot_turn_id")
        if key not in pending:
            pending[key] = _SharedWork()
        work = pending[key]
        work.add(row, share_work)
        if is_reply:
            del pending[key]
            if not is_pass:
                handle = handles.get(author, author)
                messages.append(_as_user("\n".join([f"@{handle}: {content}",
                                                    *work.describe(handle, share_work)])))

    if context_tokens is not None:
        # Keep individual text fragments until we've chosen the retained window:
        # older replies may sit in the same folded user row as the newest ask.
        budget_rows = [
            _as_user("\n\n" + message["content"])
            if index and message["role"] == messages[index - 1]["role"] == "user" else message
            for index, message in enumerate(messages)
        ]
        start = len(messages) - len(trim_history(budget_rows, context_tokens, reserved_chars))
        # An attributed bot reply uses the user role on the wire but cannot
        # displace the latest actual user message.
        messages = messages[start if latest_user is None else min(start, latest_user):]
    return repair_tool_pairs(_merge_user_rows(messages))
