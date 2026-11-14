"""Tool registry and model-facing schemas."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Awaitable, Callable

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
    #: Runs code Kova does not control (an MCP server). Such a tool asks before
    #: every call unless a permission rule allows it or the chat has full access.
    external: bool = False


@dataclass
class ToolContext:
    config: Config
    store: Store
    memory: Memory
    now: Callable[[], datetime]
    #: Connected MCP servers (``kova.mcp.McpManager``); their tools join the registry.
    mcp: Any = None


def build_tools(ctx: ToolContext) -> dict[str, Tool]:
    """Build the tools bound to a server context."""
    from . import core, files, jobs, memory, reminders, sessions, skills, system_info, web

    tools = [*web.tools(), *files.tools(ctx), *reminders.tools(ctx), *jobs.tools(ctx),
             *skills.tools(ctx), *sessions.tools(ctx),
             *memory.tools(ctx), *system_info.tools(ctx), *core.tools(ctx.config.files_path)]
    registry = {tool.name: tool for tool in tools}
    # MCP tools are named "server__tool", so they cannot shadow a built-in;
    # setdefault keeps that true even for a hand-made manager.
    for tool in ctx.mcp.tools() if ctx.mcp is not None else []:
        registry.setdefault(tool.name, tool)
    return registry


def openai_schemas(tools: dict[str, Tool]) -> list[dict]:
    """Return OpenAI-compatible function definitions."""
    return [
        {"type": "function", "function": {"name": tool.name, "description": tool.description,
                                          "parameters": tool.parameters}}
        for tool in tools.values()
    ]
