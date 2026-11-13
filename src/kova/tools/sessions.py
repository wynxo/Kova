"""Read-only recall over past chats, exposed to the model.

Kova's curated memory is small and always injected; this is the unbounded half:
an FTS5 (or LIKE) search over user/assistant text in every past conversation.
Results are capped and each hit is a short snippet, so recall stays cheap until
the model actually asks for it.
"""

from __future__ import annotations

import contextvars
import json

from . import Tool, ToolContext

#: The chat currently being answered. Set per turn by ``Agent.run_turn``; a
#: context variable keeps concurrent chats in the server from seeing each
#: other's value.
_CURRENT_CHAT: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "kova_current_chat", default=None
)

MAX_RESULTS = 20
DEFAULT_RESULTS = 5
MAX_SNIPPET_CHARS = 240
MAX_TITLE_CHARS = 120


def set_current_chat(chat_id: str | None) -> None:
    """Remember which chat a tool call belongs to for the current task."""
    _CURRENT_CHAT.set(chat_id)


def current_chat() -> str | None:
    """The chat id of the turn that is running, when known."""
    return _CURRENT_CHAT.get()


def _clip(text: str, limit: int) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _result(row: dict) -> dict:
    return {
        "chat_id": row["id"],
        "title": _clip(row.get("title") or "Untitled chat", MAX_TITLE_CHARS),
        "when": row.get("last_message") or row.get("created"),
        "role": row.get("role"),
        "snippet": _clip(row.get("snippet") or "", MAX_SNIPPET_CHARS),
    }


def tools(ctx: ToolContext) -> list[Tool]:
    """Create the chat-recall tool for a context."""
    def recall(args: dict) -> str:
        try:
            limit = int(args.get("limit") or DEFAULT_RESULTS)
        except (TypeError, ValueError):
            limit = DEFAULT_RESULTS
        limit = max(1, min(limit, MAX_RESULTS))
        exclude = None if args.get("include_current") else current_chat()
        rows = ctx.store.search_chats(args["query"], limit=limit,
                                      exclude_chat_id=exclude)
        results = [_result(row) for row in rows]
        payload: dict = {"query": args["query"], "count": len(results),
                         "results": results}
        if not results:
            payload["hint"] = ("No matching past conversations. Try fewer or different "
                               "words; only user and assistant messages are searched.")
        return json.dumps(payload)

    description = (
        "Search your past conversations with this user (read-only). Use it when "
        "they refer to something from an earlier chat instead of asking them to "
        "repeat it. Returns short snippets, newest first; the current chat is "
        "excluded unless include_current is true."
    )
    schema = {
        "type": "object",
        "properties": {
            "query": {"type": "string"},
            "limit": {"type": "integer", "minimum": 1, "maximum": MAX_RESULTS},
            "include_current": {"type": "boolean"},
        },
        "required": ["query"],
    }
    # ``search_chats`` is the same operation under the store method's name, so
    # either name works for a model or prompt that learned one of them.
    return [
        Tool("recall", description, schema, recall),
        Tool("search_chats", "Alias of recall: search past conversations (read-only).",
             schema, recall),
    ]
