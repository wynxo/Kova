"""Tools for scheduling recurring jobs, including the idle-only heartbeat."""

from __future__ import annotations

import json
from datetime import datetime

from kova.scheduler import parse_schedule
from kova.tools.reminders import format_relative, format_when, parse_when

from . import Tool, ToolContext, ToolError

#: Enough to see everything a person would schedule by hand.
MAX_LISTED_JOBS = 50


def tools(ctx: ToolContext) -> list[Tool]:
    """Create job tools for a context."""
    def add(args: dict) -> str:
        now = ctx.now()
        text = " ".join((args.get("text") or "").split())
        if not text:
            raise ToolError("Give the job a short description, so the user knows what it does.")
        command = (args.get("command") or "").strip()
        prompt = (args.get("prompt") or "").strip()
        if bool(command) == bool(prompt):
            raise ToolError("Give exactly one of command (a shell command) or prompt "
                            "(an instruction you will follow), never both or neither.")
        schedule = parse_schedule(args["schedule"], now)
        first = parse_when(args["first_run"], now) if args.get("first_run") else schedule.next_after(now)
        idle_only = bool(args.get("idle_only"))
        jid = ctx.store.add_job(text, schedule.spec, first, command=command or None,
                                prompt=prompt or None, idle_only=idle_only)
        what = "a shell command, no model call" if command else (
            "one of your turns, only when nothing else is running" if idle_only else "one of your turns")
        return (f"Job #{jid} scheduled {schedule.describe()} ({what}); next run is "
                f"{format_when(first, now)} ({format_relative(first, now)}).")

    def list_jobs(_args: dict) -> str:
        jobs = ctx.store.list_jobs()[:MAX_LISTED_JOBS]
        now = ctx.now()
        out = []
        for job in jobs:
            when = job["next_run"]
            try:
                relative = format_relative(datetime.fromisoformat(when), now)
            except ValueError:
                relative = ""
            out.append({**job, "in": relative})
        return json.dumps({"total": len(out), "jobs": out})

    def remove(args: dict) -> str:
        if not ctx.store.delete_job(int(args["id"])):
            raise ToolError(f"No job #{args['id']}. Run job_list to see the ids.")
        return f"Removed job #{args['id']}."

    return [
        Tool(
            "job_add",
            "Schedule a recurring job. Use a reminder instead for a single notification. "
            "A script job (command) runs a shell command with no model call at all; it needs user "
            "approval when you create it. A prompt job (prompt) runs one turn of your own in its own "
            "chat. Set idle_only for a proactive heartbeat: it only starts when no chat turn or other "
            "job is running.",
            {"type": "object", "properties": {
                "text": {"type": "string", "description": "What this job does, in a few words."},
                "schedule": {"type": "string", "description": "Repeat schedule: 'every 30m', 'every 2h', "
                                                             "'6h', 'hourly', 'daily', 'daily 09:00', "
                                                             "'every day at 9am'."},
                "command": {"type": "string", "description": "Shell command to run, for a script job."},
                "prompt": {"type": "string", "description": "Instruction for one of your turns, for a prompt job."},
                "first_run": {"type": "string", "description": "Optional first run time, e.g. 'tomorrow 8am'; "
                                                               "defaults to the next point of the schedule."},
                "idle_only": {"type": "boolean", "description": "Only start when nothing else is running."},
            }, "required": ["text", "schedule"]},
            add,
        ),
        Tool("job_list", "List the scheduled recurring jobs, with their schedules and next run.",
             {"type": "object", "properties": {}}, list_jobs),
        Tool("job_remove", "Delete a scheduled job by its id.",
             {"type": "object", "properties": {"id": {"type": "integer"}}, "required": ["id"]}, remove),
    ]
