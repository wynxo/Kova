"""Tools for creating and listing reminders."""

from __future__ import annotations

import json
from datetime import datetime

from . import Tool, ToolContext, ToolError


def tools(ctx: ToolContext) -> list[Tool]:
    """Create reminder tools for a context."""
    def add(args: dict) -> str:
        try:
            when = datetime.fromisoformat(args["when"])
        except ValueError as exc:
            raise ToolError("Invalid reminder time; use ISO 8601") from exc
        now = ctx.now()
        if when.tzinfo is None:
            when = when.replace(tzinfo=now.tzinfo)
        if when <= now:
            raise ToolError("Reminder time must be in the future")
        rid = ctx.store.add_reminder(when, args["text"])
        return f"Reminder #{rid} set for {when.isoformat()}"

    def list_reminders(args: dict) -> str:
        return json.dumps(ctx.store.list_reminders())

    return [
        Tool("reminder_add", "Set a reminder that pops up at a given time, even if the user closed the chat.", {"type": "object", "properties": {"when": {"type": "string", "description": "Local date and time in ISO 8601, e.g. 2026-11-12T18:30. Work it out from the current time in your instructions."}, "text": {"type": "string"}}, "required": ["when", "text"]}, add),
        Tool("reminder_list", "List the user's upcoming reminders.", {"type": "object", "properties": {}}, list_reminders),
    ]
