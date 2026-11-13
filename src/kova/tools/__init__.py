"""Tool registry and model-facing schemas."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Awaitable, Callable

from kova.config import Config
from kova.memory import Memory
from kova.store import Store


class ToolError(Exception):
    """An error that can be returned to the model."""


@dataclass
class Tool:
    name: str
    description: str
    parameters: dict
    run: Callable[[dict], str | Awaitable[str]]


@dataclass
class ToolContext:
    config: Config
    store: Store
    memory: Memory
    now: Callable[[], datetime]


def build_tools(ctx: ToolContext) -> dict[str, Tool]:
    """Build the tools bound to a server context."""
    from . import core, files, jobs, memory, reminders, sessions, skills, system_info, web

    tools = [*web.tools(), *files.tools(ctx), *reminders.tools(ctx), *jobs.tools(ctx),
             *skills.tools(ctx), *sessions.tools(ctx),
             *memory.tools(ctx), *system_info.tools(ctx), *core.tools(ctx.config.files_path)]
    return {tool.name: tool for tool in tools}


def openai_schemas(tools: dict[str, Tool]) -> list[dict]:
    """Return OpenAI-compatible function definitions."""
    return [
        {"type": "function", "function": {"name": tool.name, "description": tool.description,
                                          "parameters": tool.parameters}}
        for tool in tools.values()
    ]
