"""Tools for adding, patching, removing and searching memory notes.

There is deliberately no whole-entry replace: a model that wants to change one
fact patches a span of exactly one note. Memory is capped (see
``Memory.budget``); an over-budget write fails with the exact overflow so the
model can free space itself rather than silently losing text.

Every note carries provenance. Writes made here are recorded as
``origin=model``: the model volunteered them, and Kova cannot prove the user
said them. A fact the post-turn review could quote from the user verbatim is
recorded as ``origin=user`` with the chat id in ``source``, and anything the
user typed themselves in the CLI, TUI or web app is ``user`` too.
"""

from __future__ import annotations

import json
from collections.abc import Callable

from kova.memory import Memory

from . import Tool, ToolContext

# Kept small: the model already sees the most relevant notes in the prompt.
MAX_LISTED_NOTES = 50
MAX_SEARCH_NOTES = 10


def _note_dict(note, budget: int | None = None) -> dict:
    data = {"slug": note.slug, "title": note.title, "body": note.body,
            "tags": note.tags, "updated": note.updated,
            "origin": note.origin, "source": note.source, "created": note.created}
    if budget:
        data["chars"] = len(note.title) + sum(len(t) for t in note.tags) + len(note.body)
    return data


def _tools(memory_of: Callable[[], Memory]) -> list[Tool]:
    """Memory tools that resolve their store on every call.

    The server may swap its memory folder at runtime, so the global tools must
    read the current object rather than capture it. Per-turn tools pass a
    single project scope, which is fixed for that turn.
    """
    project = bool(memory_of().scope_id)
    kind = memory_of().scope_kind if project else ""
    where = ("for this bot" if kind == "bot"
             else "for this project" if project else "about the user")

    def add(args: dict) -> str:
        note = memory_of().add(args["title"], args["body"], args.get("tags"))
        return f"Saved memory: {note.title}"

    def patch(args: dict) -> str:
        note = memory_of().patch(args["slug"], args["old_text"], args["new_text"])
        return f"Patched memory: {note.title}"

    def remove(args: dict) -> str:
        if not memory_of().remove(args["slug"]):
            raise ValueError(f'No note named "{args["slug"]}". Run memory_list to see the slugs.')
        return f"Removed memory: {args['slug']}"

    def list_notes(_args: dict) -> str:
        memory = memory_of()
        notes = memory.list()[:MAX_LISTED_NOTES]
        stats = memory.stats()
        return json.dumps({
            "used": stats["used"],
            "budget": stats["budget"],
            "percent": stats["percent"],
            "total": stats["notes"],
            "notes": [_note_dict(note, stats["budget"]) for note in notes],
        })

    def search(args: dict) -> str:
        notes = memory_of().search(args["query"], MAX_SEARCH_NOTES, mode="hybrid")
        return json.dumps([_note_dict(note) for note in notes])

    # The old name keeps working for prompts/tests that still call it, but it
    # now has add semantics, so it can never overwrite a sibling note.
    save = add

    return [
        Tool("memory_add", f"Save a new lasting fact {where} (name, preferences, people, projects) as a memory note. Fails if a note with that title already exists: use memory_patch to change one instead of replacing it. Fails when memory is over its size budget.", {"type": "object", "properties": {"title": {"type": "string"}, "body": {"type": "string"}, "tags": {"type": "array", "items": {"type": "string"}}}, "required": ["title", "body"]}, add),
        Tool("memory_patch", f"Change a specific span inside one existing memory note {where}. old_text must match exactly once; this never replaces the whole note.", {"type": "object", "properties": {"slug": {"type": "string"}, "old_text": {"type": "string"}, "new_text": {"type": "string"}}, "required": ["slug", "old_text", "new_text"]}, patch),
        Tool("memory_remove", f"Delete one memory note {where} by slug. Use memory_list to find slugs.", {"type": "object", "properties": {"slug": {"type": "string"}}, "required": ["slug"]}, remove),
        Tool("memory_list", f"List saved memory notes {where} with their slugs, plus how much of the memory size budget is used.", {"type": "object", "properties": {}}, list_notes),
        Tool("memory_search", f"Search what you remember {where}.", {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}, search),
        Tool("memory_save", f"Deprecated alias of memory_add {where} (never overwrites an existing note).", {"type": "object", "properties": {"title": {"type": "string"}, "body": {"type": "string"}, "tags": {"type": "array", "items": {"type": "string"}}}, "required": ["title", "body"]}, save),
    ]


def tools_for(memory: Memory) -> list[Tool]:
    """Create memory tools bound to one store: global, or a project's scope."""
    return _tools(lambda: memory)


def tools(ctx: ToolContext) -> list[Tool]:
    """Create memory tools for a context, following its store if it is swapped."""
    return _tools(lambda: ctx.memory)
