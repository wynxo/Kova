"""Tools for adding, patching, removing and searching memory notes.

There is deliberately no whole-entry replace: a model that wants to change one
fact patches a span of exactly one note. Memory is capped (see
``Memory.budget``); an over-budget write fails with the exact overflow so the
model can free space itself rather than silently losing text.
"""

from __future__ import annotations

import json

from . import Tool, ToolContext

# Kept small: the model already sees the most relevant notes in the prompt.
MAX_LISTED_NOTES = 50
MAX_SEARCH_NOTES = 10


def _note_dict(note, budget: int | None = None) -> dict:
    data = {"slug": note.slug, "title": note.title, "body": note.body,
            "tags": note.tags, "updated": note.updated}
    if budget:
        data["chars"] = len(note.title) + sum(len(t) for t in note.tags) + len(note.body)
    return data


def tools(ctx: ToolContext) -> list[Tool]:
    """Create memory tools for a context."""
    def add(args: dict) -> str:
        note = ctx.memory.add(args["title"], args["body"], args.get("tags"))
        return f"Saved memory: {note.title}"

    def patch(args: dict) -> str:
        note = ctx.memory.patch(args["slug"], args["old_text"], args["new_text"])
        return f"Patched memory: {note.title}"

    def remove(args: dict) -> str:
        if not ctx.memory.remove(args["slug"]):
            raise ValueError(f'No note named "{args["slug"]}". Run memory_list to see the slugs.')
        return f"Removed memory: {args['slug']}"

    def list_notes(_args: dict) -> str:
        notes = ctx.memory.list()[:MAX_LISTED_NOTES]
        stats = ctx.memory.stats()
        return json.dumps({
            "used": stats["used"],
            "budget": stats["budget"],
            "percent": stats["percent"],
            "total": stats["notes"],
            "notes": [_note_dict(note, stats["budget"]) for note in notes],
        })

    def search(args: dict) -> str:
        notes = ctx.memory.search(args["query"], MAX_SEARCH_NOTES)
        return json.dumps([_note_dict(note) for note in notes])

    # The old name keeps working for prompts/tests that still call it, but it
    # now has add semantics, so it can never overwrite a sibling note.
    save = add

    return [
        Tool("memory_add", "Save a new lasting fact about the user (name, preferences, people, projects) as a memory note. Fails if a note with that title already exists: use memory_patch to change one instead of replacing it. Fails when memory is over its size budget.", {"type": "object", "properties": {"title": {"type": "string"}, "body": {"type": "string"}, "tags": {"type": "array", "items": {"type": "string"}}}, "required": ["title", "body"]}, add),
        Tool("memory_patch", "Change a specific span inside one existing memory note. old_text must match exactly once; this never replaces the whole note.", {"type": "object", "properties": {"slug": {"type": "string"}, "old_text": {"type": "string"}, "new_text": {"type": "string"}}, "required": ["slug", "old_text", "new_text"]}, patch),
        Tool("memory_remove", "Delete one memory note by slug. Use memory_list to find slugs.", {"type": "object", "properties": {"slug": {"type": "string"}}, "required": ["slug"]}, remove),
        Tool("memory_list", "List saved memory notes with their slugs, plus how much of the memory size budget is used.", {"type": "object", "properties": {}}, list_notes),
        Tool("memory_search", "Search what you remember about the user.", {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}, search),
        Tool("memory_save", "Deprecated alias of memory_add (never overwrites an existing note).", {"type": "object", "properties": {"title": {"type": "string"}, "body": {"type": "string"}, "tags": {"type": "array", "items": {"type": "string"}}}, "required": ["title", "body"]}, save),
    ]
