"""Tools for the model's own skills: list them, load one, write one, delete one.

The descriptions are already in the system prompt, so ``skill_list`` is for
checking what exists; ``skill_load`` is the only way to see a body, which is
what keeps long procedures out of every request.
"""

from __future__ import annotations

import json

from kova.skills import MAX_DESCRIPTION_CHARS, SkillError, Skills

from . import Tool, ToolContext, ToolError


def _store(ctx: ToolContext) -> Skills:
    """The skills folder of this context's home; one folder per Kova home."""
    return Skills.for_home(ctx.config.home)


def _summary(skill) -> dict:
    return {"name": skill.name, "slug": skill.slug, "description": skill.description,
            "chars": len(skill.body)}


def tools(ctx: ToolContext) -> list[Tool]:
    """Create skill tools for a context."""
    def load(args: dict) -> str:
        skill = _store(ctx).get(args["name"])
        if skill is None:
            raise ToolError(_missing(ctx, args["name"]))
        return skill.text

    def list_skills(_args: dict) -> str:
        skills = _store(ctx).list()
        return json.dumps({"total": len(skills), "skills": [_summary(skill) for skill in skills]})

    def write(args: dict) -> str:
        try:
            skill = _store(ctx).save(args["name"], args["description"], args["body"])
        except SkillError as exc:
            raise ToolError(str(exc)) from exc
        return f"Saved skill '{skill.name}' ({len(skill.body)} body characters). It is listed from now on."

    def remove(args: dict) -> str:
        if not _store(ctx).remove(args["name"]):
            raise ToolError(f'No skill you wrote is named "{args["name"]}". {_note(ctx)}')
        return f"Removed skill '{args['name']}'."

    return [
        Tool(
            "skill_load",
            "Read a skill's full instructions. The one-line summaries in your prompt are only for "
            "choosing: load the skill before you follow it, then do the steps it gives.",
            {"type": "object", "properties": {"name": {"type": "string",
                                                       "description": "The skill's name or slug."}},
             "required": ["name"]},
            load,
        ),
        Tool(
            "skill_list",
            "List the skills you have, with a one-line description of each. Their summaries are "
            "already in your prompt; use this to check for duplicates before writing.",
            {"type": "object", "properties": {}},
            list_skills,
        ),
        Tool(
            "skill_write",
            "Save a skill: reusable procedural memory for a task you worked out how to do. Use the "
            "same name to update a skill you wrote before. Keep the description to one line of at "
            f"most {MAX_DESCRIPTION_CHARS} characters (what it is for, so you can pick it later); "
            "put every step, command and gotcha in the body, which is only read when the skill is loaded.",
            {"type": "object", "properties": {
                "name": {"type": "string", "description": "Short name, e.g. 'Deploy the site'."},
                "description": {"type": "string", "description": "One line: what this skill is for."},
                "body": {"type": "string", "description": "The full instructions, in Markdown."},
            }, "required": ["name", "description", "body"]},
            write,
        ),
        Tool(
            "skill_remove",
            "Delete a skill you wrote and no longer need.",
            {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]},
            remove,
        ),
    ]


def _note(ctx: ToolContext) -> str:
    names = [skill.name for skill in _store(ctx).list()][:20]
    return ("Your skills: " + ", ".join(names)) if names else "You have no skills yet."


def _missing(ctx: ToolContext, name: str) -> str:
    return f'No skill named "{name}". {_note(ctx)}'
