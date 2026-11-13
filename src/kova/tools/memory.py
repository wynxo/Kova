"""Tools for adding, patching, removing and searching memory notes.

There is deliberately no whole-entry replace: a model that wants to change one
fact patches a span of exactly one note. Memory is capped (see
``Memory.budget``); an over-budget write fails with the exact overflow so the
model can free space itself rather than silently losing text.
"""

from __future__ import annotations

import json
import re

from kova.memory import MemoryStoreError
from .sessions import current_chat

from . import Tool, ToolContext

# Kept small: the model already sees the most relevant notes in the prompt.
MAX_LISTED_NOTES = 50
MAX_SEARCH_NOTES = 10


def _note_dict(note, budget: int | None = None) -> dict:
    data = {"slug": note.slug, "title": note.title, "body": note.body,
            "tags": note.tags, "updated": note.updated, "provenance": note.provenance}
    if budget:
        data["chars"] = len(note.title) + sum(len(t) for t in note.tags) + len(note.body)
    return data


def tools(ctx: ToolContext) -> list[Tool]:
    """Create memory tools for a context."""
    def authorize(*, deleting: bool = False) -> dict | None:
        chat_id = current_chat()
        if not chat_id:
            return None  # direct, human-invoked tool context, not a model turn
        rows = ctx.store.memory_evidence(chat_id)
        source = next((r for r in reversed(rows) if r['role'] == 'user'), None)
        request = source['content'] or '' if source else ''
        explicit = re.search(r"\b(?:forget|delete|remove)\b" if deleting else r"\b(?:remember|memorize|save (?:this|that|a|my|the)\b.*memory)\b", request, re.I)
        if not ctx.store.memory_settings()['model_writes'] and not explicit:
            raise MemoryStoreError("Model-suggested memory writes are opt-in. Ask the user to enable them in memory settings, or wait for an explicit remember/forget request.")
        return {"source_chat_id": chat_id, "date": ctx.now().isoformat(), "kind": "model-inferred",
                "evidence": [{"message_id": source['id'], "quote": request}] if source else []}

    def add(args: dict) -> str:
        provenance = authorize()
        if provenance and any(args['body'] in e['quote'] for e in provenance['evidence']):
            provenance['kind'] = 'user-stated'
            provenance['evidence'][0]['quote'] = args['body']
        note = ctx.memory.add(args["title"], args["body"], args.get("tags"), provenance=provenance)
        return f"Saved memory: {note.title}"

    def patch(args: dict) -> str:
        provenance = authorize()
        note = ctx.memory.patch(args["slug"], args["old_text"], args["new_text"], provenance=provenance)
        return f"Patched memory: {note.title}"

    def remove(args: dict) -> str:
        authorize(deleting=True)
        if not ctx.memory.remove(args["slug"]):
            raise ValueError(f'No note named "{args["slug"]}". Run memory_list to see the slugs.')
        return f"Removed memory: {args['slug']}"

    def list_notes(_args: dict) -> str:
        if _args.get('slug'):
            note = ctx.memory.get(_args['slug'])
            return json.dumps(_note_dict(note) if note else None)
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
        Tool("memory_add", "Only when the user asks to remember or has opted in: save a new lasting fact about the user (name, preferences, people, projects) as a memory note. Fails if a note with that title already exists: use memory_patch to change one instead of replacing it. Fails when memory is over its size budget.", {"type": "object", "properties": {"title": {"type": "string"}, "body": {"type": "string"}, "tags": {"type": "array", "items": {"type": "string"}}}, "required": ["title", "body"]}, add),
        Tool("memory_patch", "Change a specific span inside one existing memory note. old_text must match exactly once; this never replaces the whole note.", {"type": "object", "properties": {"slug": {"type": "string"}, "old_text": {"type": "string"}, "new_text": {"type": "string"}}, "required": ["slug", "old_text", "new_text"]}, patch),
        Tool("memory_remove", "Delete one memory note by slug. Use memory_list to find slugs.", {"type": "object", "properties": {"slug": {"type": "string"}}, "required": ["slug"]}, remove),
        Tool("memory_list", "List saved memory notes, provenance and budget; pass slug to read one indexed topic on demand.", {"type": "object", "properties": {"slug": {"type": "string"}}}, list_notes),
        Tool("memory_search", "Search what you remember about the user.", {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}, search),
        Tool("memory_save", "Deprecated alias of memory_add (never overwrites an existing note).", {"type": "object", "properties": {"title": {"type": "string"}, "body": {"type": "string"}, "tags": {"type": "array", "items": {"type": "string"}}}, "required": ["title", "body"]}, save),
    ]
