"""HTTP routes for MCP servers: list, add, switch on/off, test and remove.

Every client (``kova mcp``, the web and desktop settings) goes through these
routes, so the running server's config stays the one source of truth and its
tool registry updates without a restart.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace

from fastapi import APIRouter, Depends, FastAPI, HTTPException
from pydantic import BaseModel, Field

from kova import plugins, egress
from kova.config import Config, McpServerEntry, save_config
from kova.mcp import McpManager, tool_name, validate_server
from kova.pairing import require_capability, require_local
from kova.permissions import Rule
from kova.tools import build_tools


class McpBody(BaseModel):
    """A new server: ``command`` (+ ``args``/``env``) for stdio, or ``url`` (+ ``headers``)."""
    name: str
    command: str = ""
    args: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)
    url: str = ""
    headers: dict[str, str] = Field(default_factory=dict)
    enabled: bool = True
    sandbox: bool = False


class McpPatch(BaseModel):
    enabled: bool | None = None


class McpToolPatch(BaseModel):
    """Switch one tool on or off, and choose whether it still asks first."""
    enabled: bool | None = None
    always_allow: bool | None = None


def _is_tool_grant(rule: Rule, name: str) -> bool:
    """The rule the settings switch writes: allow one tool, any arguments, always."""
    return (rule.action == "allow" and rule.tool == name and rule.pattern == "*"
            and rule.scope == "always" and not rule.exact)


def install_mcp_routes(app: FastAPI, config: Config, require_token) -> None:
    router = APIRouter(dependencies=[Depends(require_token)])
    write = [Depends(require_capability("extensions:write"))]
    # One change at a time: a reload connects servers and swaps the registry.
    lock = asyncio.Lock()

    async def reload(restart: tuple[str, ...] = ()) -> None:
        egress.check_external_process()
        async with lock:
            manager: McpManager = app.state.mcp
            servers = list(config.mcp)
            servers += plugins.plugin_mcp_entries(config.home)
            await asyncio.to_thread(manager.reload, servers, restart=restart)
            # No await between rebuilding and assigning: a turn sees the old
            # registry or the new one, never half of each.
            tools = build_tools(app.state.tool_context)
            from kova import image_routes
            if image_routes.enabled(config):
                tools[image_routes.TOOL] = image_routes.image_tool(app)
            app.state.agent.tools = tools

    app.state.mcp_reload = reload

    def rows() -> list[dict]:
        rules = app.state.agent.approvals.rules
        status = app.state.mcp.status()
        for row in status:
            for tool in row["tools"]:
                tool["always_allow"] = any(_is_tool_grant(rule, tool["kova_name"]) for rule in rules)
        return status

    def row(name: str) -> dict:
        return next(item for item in rows() if item["name"] == name)

    def entry(name: str) -> McpServerEntry:
        found = next((server for server in [*config.mcp, *plugins.plugin_mcp_entries(config.home)]
                      if server.name == name), None)
        if found is None:
            raise HTTPException(404, f"No MCP server named {name!r}. List them with: kova mcp list")
        return found

    @router.get("/mcp")
    async def mcp_list() -> list[dict]:
        return rows()

    # Adding a server chooses a program to run on this computer: never from a phone.
    @router.post("/mcp", dependencies=[Depends(require_local)])
    async def mcp_add(body: McpBody) -> dict:
        egress.check_external_process()
        server = McpServerEntry(**body.model_dump())
        try:
            validate_server(server)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None
        if any(existing.name == server.name for existing in config.mcp):
            raise HTTPException(409, f"An MCP server named {server.name!r} already exists. "
                                     f"Remove it first with: kova mcp remove {server.name}")
        config.mcp.append(server)
        save_config(config)
        await reload()
        return row(server.name)

    @router.post("/mcp/reload", dependencies=write)
    async def mcp_reload() -> dict:
        """Reconnect every MCP server now, including any from plugins."""
        await reload()
        return {"ok": True}

    @router.patch("/mcp/{name}", dependencies=write)
    async def mcp_update(name: str, body: McpPatch) -> dict:
        egress.check_external_process()
        server = entry(name)
        if server.plugin:
            raise HTTPException(409, f"{name} comes from the {server.plugin} plugin. "
                                f"Manage it with: kova plugin disable {server.plugin}")
        if body.enabled is not None:
            config.mcp[config.mcp.index(server)] = replace(server, enabled=body.enabled)
            save_config(config)
            await reload()
        return row(name)

    @router.delete("/mcp/{name}", dependencies=write)
    async def mcp_remove(name: str) -> dict:
        egress.check_external_process()
        server = entry(name)
        if server.plugin:
            return {"ok": True, "plugin": server.plugin}
        config.mcp.remove(server)
        save_config(config)
        # A later server with the same name must not inherit this one's grants.
        approvals = app.state.agent.approvals
        prefix = f"{name}__"
        kept = [rule for rule in approvals.rules if not rule.tool.startswith(prefix)]
        if len(kept) != len(approvals.rules):
            approvals.rules = kept
            approvals.save()
        await reload()
        return {"ok": True}

    # ``:path`` because MCP tool names may contain "/".
    @router.patch("/mcp/{name}/tools/{tool:path}", dependencies=write)
    async def mcp_tool_update(name: str, tool: str, body: McpToolPatch) -> dict:
        egress.check_external_process()
        server = entry(name)
        status = row(name)
        known = {item["name"] for item in status["tools"]}
        if tool not in known and tool not in server.disabled_tools:
            raise HTTPException(404, f"{name} has no tool named {tool!r}")
        if body.enabled is not None and server.plugin:
            raise HTTPException(409, f"{name} comes from the {server.plugin} plugin. "
                                f"Manage it with: kova plugin disable {server.plugin}")
        if body.enabled is not None:
            disabled = [item for item in server.disabled_tools if item != tool]
            if not body.enabled:
                disabled.append(tool)
            config.mcp[config.mcp.index(server)] = replace(server, disabled_tools=disabled)
            save_config(config)
        if body.always_allow is not None:
            approvals = app.state.agent.approvals
            kova_name = tool_name(name, tool)
            approvals.rules = [rule for rule in approvals.rules if not _is_tool_grant(rule, kova_name)]
            if body.always_allow:
                approvals.rules.append(Rule("allow", kova_name, "*", scope="always"))
            approvals.save()
        await reload()
        return row(name)

    @router.post("/mcp/{name}/test", dependencies=write)
    async def mcp_test(name: str) -> dict:
        """Reconnect one server now and report what it offers."""
        entry(name)
        await reload(restart=(name,))
        return row(name)

    app.include_router(router)
