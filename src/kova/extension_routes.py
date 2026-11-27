"""HTTP routes for the Extensions page: catalog, installed plugins, skills and bots.

Installing from the catalog is two requests so nothing runs unconfirmed:
``POST /extensions/catalog/{name}/preview`` downloads and verifies the source
and returns exactly what it adds plus a one-use ticket; only
``POST /extensions/install`` with that ticket writes anything. Installing or
re-enabling a plugin chooses programs to run on this computer, so those are
refused from a paired phone, like adding an MCP server.
"""

from __future__ import annotations

import asyncio
import json
import secrets
import time

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Query, Request
from pydantic import BaseModel

from kova import catalog, extensions, plugins
from kova.config import Config
from kova.pairing import is_lan_request, require_capability, require_local
from kova.permissions import Rule
from kova.skills import SkillError

#: How long a verified download waits for the user's confirmation.
TICKET_SECONDS = 600
#: How long a loaded index is reused before it is fetched again.
INDEX_SECONDS = 300


class PreviewBody(BaseModel):
    version: str | None = None


class InstallBody(BaseModel):
    ticket: str
    upgrade: bool = False


class PluginPatch(BaseModel):
    enabled: bool


class SkillBody(BaseModel):
    name: str
    description: str
    body: str


def install_extension_routes(app: FastAPI, config: Config, require_token) -> None:
    router = APIRouter(dependencies=[Depends(require_token)])
    write = [Depends(require_capability("extensions:write"))]
    home = config.home
    staged: dict[str, tuple[float, catalog.Staged]] = {}
    cache: dict[str, tuple[float, catalog.Catalog]] = {}
    lock = asyncio.Lock()

    def expire() -> None:
        now = time.monotonic()
        for ticket, (created, item) in list(staged.items()):
            if now - created > TICKET_SECONDS:
                staged.pop(ticket, None)
                item.cleanup()

    async def load_catalog(refresh: bool = False) -> catalog.Catalog:
        url = catalog.configured_url(config)
        hit = cache.get(url)
        if hit and not refresh and time.monotonic() - hit[0] < INDEX_SECONDS:
            return hit[1]
        loaded = await asyncio.to_thread(catalog.load, url)
        cache.clear()
        cache[url] = (time.monotonic(), loaded)
        return loaded

    async def refresh_runtime() -> None:
        """Make a plugin change take effect without a restart."""
        agent = app.state.agent
        agent.skills.extra_roots = plugins.plugin_skill_roots(home)
        approvals = agent.approvals
        if approvals.path and approvals.path.exists():
            try:
                saved = [Rule(**rule) for rule in json.loads(approvals.path.read_text())]
            except (OSError, ValueError, TypeError):
                saved = None
            if saved is not None:
                approvals.rules = saved + [rule for rule in approvals.rules if rule.scope != "always"]
        reload = getattr(app.state, "mcp_reload", None)
        if reload is not None:
            try:
                await reload()
            except Exception:  # an MCP hiccup must not undo a finished install
                pass

    def entry_row(entry: catalog.Entry) -> dict:
        row = entry.as_dict()
        installed = catalog.installed_version(entry, home)
        row["installed_version"] = installed
        row["update_available"] = bool(installed and installed != entry.version)
        return row

    def plugin_row(plugin: plugins.Plugin) -> dict:
        row = plugins.describe(plugin)
        row["enabled"] = plugin.enabled
        row["source"] = plugin.source
        return row

    @router.get("/extensions/catalog")
    async def catalog_list(q: str = "", kind: str | None = None,
                           refresh: bool = Query(False)) -> dict:
        if kind is not None and kind not in catalog.KINDS:
            raise HTTPException(422, "kind must be plugin, skill or bot")
        loaded = await load_catalog(refresh)
        return {"source": loaded.source, "bundled": loaded.bundled, "warning": loaded.warning,
                "entries": [entry_row(entry) for entry in loaded.search(q, kind)]}

    @router.post("/extensions/catalog/{name}/preview", dependencies=[Depends(require_local)])
    async def catalog_preview(name: str, body: PreviewBody | None = None) -> dict:
        expire()
        loaded = await load_catalog()
        try:
            entry = loaded.get(name)
            item = await asyncio.to_thread(catalog.stage, entry,
                                           version=body.version if body else None)
        except catalog.CatalogError as exc:
            raise HTTPException(422, str(exc)) from None
        ticket = secrets.token_urlsafe(18)
        staged[ticket] = (time.monotonic(), item)
        info = item.describe()
        info["ticket"] = ticket
        info["installed_version"] = catalog.installed_version(entry, home)
        return info

    @router.delete("/extensions/catalog/preview/{ticket}", dependencies=write)
    async def catalog_cancel(ticket: str) -> dict:
        found = staged.pop(ticket, None)
        if found:
            found[1].cleanup()
        return {"ok": True}

    @router.post("/extensions/install", dependencies=[Depends(require_local)])
    async def catalog_install(body: InstallBody) -> dict:
        expire()
        found = staged.pop(body.ticket, None)
        if found is None:
            raise HTTPException(410, "That preview expired. Open the extension again.")
        async with lock:
            try:
                installed = await asyncio.to_thread(catalog.install_staged, found[1], home,
                                                    upgrade=body.upgrade)
            except catalog.CatalogError as exc:
                raise HTTPException(409, str(exc)) from None
            await refresh_runtime()
        return {"installed": installed}

    @router.get("/extensions/plugins")
    async def plugin_list() -> list[dict]:
        return [plugin_row(plugin) for plugin in await asyncio.to_thread(plugins.installed, home)]

    @router.patch("/extensions/plugins/{name}", dependencies=write)
    async def plugin_patch(name: str, body: PluginPatch, request: Request) -> dict:
        if body.enabled and is_lan_request(request):
            raise HTTPException(403, "Do this on the computer Kova runs on.")
        async with lock:
            try:
                if body.enabled:
                    await asyncio.to_thread(plugins.enable, name, home)
                else:
                    await asyncio.to_thread(plugins.disable, name, home)
            except plugins.PluginError as exc:
                raise HTTPException(404, str(exc)) from None
            await refresh_runtime()
        found = next(plugin for plugin in plugins.installed(home) if plugin.name == name)
        return plugin_row(found)

    @router.delete("/extensions/plugins/{name}", dependencies=write)
    async def plugin_remove(name: str) -> dict:
        async with lock:
            try:
                await asyncio.to_thread(plugins.remove, name, home)
            except plugins.PluginError as exc:
                raise HTTPException(404, str(exc)) from None
            await refresh_runtime()
        return {"removed": name}

    @router.get("/extensions/skills")
    async def skill_list() -> list[dict]:
        return await asyncio.to_thread(extensions.list_skills, home)

    @router.get("/extensions/skills/{skill_id:path}")
    async def skill_get(skill_id: str) -> dict:
        found = await asyncio.to_thread(extensions.get_skill, home, skill_id)
        if found is None:
            raise HTTPException(404, "No such skill.")
        return found

    @router.put("/extensions/skills/{skill_id:path}", dependencies=write)
    async def skill_put(skill_id: str, body: SkillBody) -> dict:
        try:
            return await asyncio.to_thread(extensions.update_skill, home, skill_id,
                                           body.name, body.description, body.body)
        except SkillError as exc:
            raise HTTPException(422, str(exc)) from None

    @router.delete("/extensions/skills/{skill_id:path}", dependencies=write)
    async def skill_delete(skill_id: str) -> dict:
        try:
            await asyncio.to_thread(extensions.delete_skill, home, skill_id)
        except SkillError as exc:
            raise HTTPException(422, str(exc)) from None
        return {"deleted": skill_id}

    app.include_router(router)
