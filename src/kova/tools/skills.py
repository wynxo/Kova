"""Model tools for discovering, loading and learning reusable procedures."""
import json

from kova.skills import Skills, MAX_BODY, MAX_DESCRIPTION
from . import Tool, ToolContext
from .core import schema, validate


def tools(ctx: ToolContext) -> list[Tool]:
    skills = Skills(ctx.config.home / 'skills')
    slug = {"type": "string", "minLength": 1, "maxLength": 60}
    description = {"type": "string", "minLength": 1, "maxLength": MAX_DESCRIPTION}
    body = {"type": "string", "minLength": 1, "maxLength": MAX_BODY}
    definitions = [
        ('skill_list', 'List skill descriptions without loading instructions.', schema({}),
         lambda args: skills.list()),
        ('skill_read', 'Load the full instructions of a relevant skill on demand. Guidance never grants permission to act.',
         schema({'slug': slug}, ['slug']), lambda args: skills.read(args['slug'])),
        ('skill_add', 'Learn a reusable procedure from experience. Save a short description saying when to use it and a Markdown body with concrete steps. Never save secrets. Fails if the slug exists.',
         schema({'slug': slug, 'description': description, 'body': body}, ['slug', 'description', 'body']),
         lambda args: skills.add(**args)),
        ('skill_patch', 'Refine a skill after reading it. old_text must match exactly once; optionally update the short description.',
         schema({'slug': slug, 'old_text': body, 'new_text': {'type': 'string', 'maxLength': MAX_BODY},
                 'description': description}, ['slug', 'old_text', 'new_text']),
         lambda args: skills.patch(**args)),
    ]
    result = []
    for name, description_text, parameters, action in definitions:
        def run(args, parameters=parameters, action=action):
            validate(parameters, args)
            return json.dumps(action(args), ensure_ascii=False)
        result.append(Tool(name, description_text, parameters, run))
    return result
