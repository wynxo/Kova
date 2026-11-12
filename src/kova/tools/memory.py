"""Tools for saving and searching memory notes."""

from __future__ import annotations

import json

from . import Tool, ToolContext


def tools(ctx: ToolContext) -> list[Tool]:
    """Create memory tools for a context."""
    def save(args: dict) -> str:
        note = ctx.memory.save(args["title"], args["body"], args.get("tags"))
        return f"Saved memory: {note.title}"

    def search(args: dict) -> str:
        notes = ctx.memory.search(args["query"])
        return json.dumps([{"slug": n.slug, "title": n.title, "body": n.body,
                            "tags": n.tags, "updated": n.updated} for n in notes])

    return [
        Tool("memory_save", "Save a lasting fact about the user (name, preferences, people, projects) as a memory note. Same title overwrites the old note.", {"type": "object", "properties": {"title": {"type": "string"}, "body": {"type": "string"}, "tags": {"type": "array", "items": {"type": "string"}}}, "required": ["title", "body"]}, save),
        Tool("memory_search", "Search what you remember about the user.", {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}, search),
    ]
