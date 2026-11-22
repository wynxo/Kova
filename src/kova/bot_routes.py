"""Authenticated bot management, shared by every client (task 1.5).

A bot is a folder of files (see :mod:`kova.bots`); these routes are the only
way the app writes one. Reads are available to any paired phone that may chat,
because a bot's definition is not a secret and the sidebar needs it. Writes that
change what a bot may do need ``settings:write``, and a bot that would get
commands, scheduled jobs, MCP tools, a ``full`` ceiling or a working folder
outside Kova Files needs ``host:exec`` too — the same capabilities those tools
need in a turn. A request from the computer itself is never limited.

Every change publishes ``bot_changed`` on the event bus so a second window
refreshes its contacts without polling.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from fnmatch import fnmatchcase
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Body, Depends, File, HTTPException, Request, UploadFile
from fastapi.responses import Response

from kova import bots, group_settings, pairing
from kova.actors import Actor
from kova.bots import AVATAR_TYPES, BotDefinition, BotError
from kova.config import Config
from kova.pairing import COMMAND_TOOLS, EXTENSION_TOOLS, require_capability

#: Tool names that stand for "can reach the computer": commands, scheduled
#: jobs, and anything Kova cannot see into (an MCP tool is ``server__tool``).
HOST_TOOLS = frozenset({*COMMAND_TOOLS, *EXTENSION_TOOLS, "server__tool"})

#: Suffix -> media type for an avatar the bot folder points at.
_AVATAR_MEDIA = {extension.lstrip("."): mime for mime, extension in AVATAR_TYPES.items()}
_AVATAR_MEDIA["jpg"] = "image/jpeg"


def grants_host_access(bot: BotDefinition, *, files_path: Path, home: Path,
                       memory_path: Path | None) -> bool:
    """Whether this definition would let a bot reach beyond an ordinary chat.

    A ``full`` ceiling, a tool allowlist that can match a command, job, skill or
    MCP tool, or a working folder outside Kova Files all give a turn something a
    phone without ``host:exec`` may not have. The answer is derived from the
    same lists :func:`kova.pairing.action_capabilities` uses, so a new such tool
    is one edit here and one there.
    """
    if bot.permissions.ceiling == "full":
        return True
    patterns = bot.permissions.tools or ()
    for pattern in patterns:
        # A pattern with a double underscore can only name an MCP tool
        # (`<server>__<tool>`), even before that server is connected.
        if "__" in pattern or fnmatchcase("server__tool", pattern):
            return True
        if any(fnmatchcase(name, pattern) for name in HOST_TOOLS):
            return True
    folder = bot.permissions.folder
    if folder and not pairing.folder_is_shared(folder, files_path, home, memory_path):
        return True
    return False


def _model_names(config: Config) -> list[str]:
    config.bind_providers()
    return [entry.name for entry in config.models]


def _merge(base: Mapping[str, Any], changes: Mapping[str, Any]) -> dict:
    """Merge one level of tables, so a PATCH names only the fields it changes."""
    out = dict(base)
    for key, value in changes.items():
        if isinstance(value, Mapping) and isinstance(out.get(key), Mapping):
            out[key] = {**out[key], **value}
        else:
            out[key] = value
    return out


def install_bot_routes(app, config: Config, require_token) -> None:
    """Install the ``/bots`` router on ``app``."""
    router = APIRouter(dependencies=[Depends(require_token)])
    write = [Depends(require_capability("settings:write"))]

    def models() -> list[str]:
        return _model_names(config)

    def folder(bot_id: str) -> Path:
        return bots.bot_root(config.home) / bot_id

    def load(bot_id: str) -> BotDefinition:
        bot = bots.get_bot(config.home, bot_id, models=models())
        if bot is None:
            if not (folder(bot_id).is_dir()):
                raise HTTPException(404, "bot not found")
            # The folder exists but its file is broken: say why, in one line.
            record = next((record for record in
                           bots.list_bots(config.home, models=models())
                           if record.id == bot_id), None)
            raise HTTPException(422, record.error if record and record.error else "bot not found")
        return bot

    def guard(request: Request, bot: BotDefinition) -> None:
        """A phone needs host:exec to give a bot anything that reaches the host."""
        allowed = pairing.request_capabilities(request)
        if allowed is None or "host:exec" in allowed:
            return
        if grants_host_access(bot, files_path=config.files_path, home=config.home,
                             memory_path=config.memory_path):
            device = request.app.state.devices.find(
                request.cookies.get(pairing.DEVICE_COOKIE))
            hint = (f" To allow it, on your computer run: "
                    f"kova devices allow {device['id']} host:exec") if device else ""
            raise HTTPException(
                403, "This phone is not allowed to give a bot commands, scheduled jobs, "
                     "MCP tools, Full access or a working folder outside Kova Files." + hint)

    def publish(bot_id: str, action: str) -> None:
        bus = getattr(app.state, "bus", None)
        if bus is not None:
            bus.publish({"type": "bot_changed", "id": bot_id, "action": action})

    def status(bot_id: str) -> str:
        """Whether this bot is answering somewhere right now (for a sidebar dot)."""
        try:
            chats = app.state.store.list_chats()
            states = app.state.conversation_states
        except AttributeError:  # before the lifespan built them (unit use)
            return "idle"
        state = "idle"
        for chat in chats:
            if chat.get("kind") == "bot" and chat.get("bot_id") == bot_id:
                current = states.get(chat["id"])
                if current == "working":
                    return "working"
                if current == "blocked-on-approval":
                    state = "blocked-on-approval"
        return state

    def public(bot: BotDefinition, *, persona: bool = False) -> dict:
        data = {
            **bot.summary(),
            "state": "ok",
            "error": None,
            "warnings": list(bot.warnings),
            "status": status(bot.id),
        }
        if persona:
            data["persona"] = bot.persona
            data["definition"] = bot.to_data()
        return data

    def public_record(record) -> dict:
        if record.bot is None:
            return {"id": record.id, "state": "error", "error": record.error,
                    "status": "idle"}
        return public(record.bot)

    @router.get("/bots")
    async def list_bots() -> list[dict]:
        records = await asyncio.to_thread(bots.list_bots, config.home, models=models())
        return [public_record(record) for record in records]

    @router.get("/group-settings")
    async def group_settings_schema() -> dict:
        """Every group and member setting, and the presets, for every surface.

        Web, desktop, phone and terminal render their settings views from this
        one response, so labels, ranges and defaults cannot drift between them.
        """
        return group_settings.schema()

    @router.get("/bots/{bot_id}")
    async def get_bot(bot_id: str) -> dict:
        return public(load(bot_id), persona=True)

    @router.post("/bots", dependencies=write)
    async def create_bot(request: Request, body: dict = Body(...),
                         validate_only: bool = False) -> dict:
        payload = dict(body)
        persona = payload.pop("persona", None)
        bot_id = str(payload.get("id") or "")
        if bot_id and folder(bot_id).exists():
            raise HTTPException(409, f'A bot named "{bot_id}" already exists.')
        try:
            bot = bots.validate(payload, bot_folder=folder(bot_id), models=models(),
                                kova_home=config.home,
                                persona=persona if isinstance(persona, str) else "")
        except BotError as exc:
            raise HTTPException(422, str(exc)) from None
        guard(request, bot)
        if validate_only:
            return {"ok": True, "bot": public(bot)}
        try:
            saved = await asyncio.to_thread(bots.save_bot, config.home, bot,
                                            models=models(), persona=bot.persona)
        except BotError as exc:
            raise HTTPException(422, str(exc)) from None
        publish(saved.id, "created")
        return public(saved, persona=True)

    @router.patch("/bots/{bot_id}", dependencies=write)
    async def update_bot(bot_id: str, request: Request, body: dict = Body(...)) -> dict:
        current = load(bot_id)
        payload = dict(body)
        persona = payload.pop("persona", None)
        if "id" in payload and payload["id"] != current.id:
            raise HTTPException(422, "A bot's id is its folder name. Create a new bot instead.")
        try:
            merged = _merge(current.to_data(), payload)
            text = current.persona if persona is None else persona
            if not isinstance(text, str):
                raise BotError("Persona must be text.")
            others = [record.bot.handle for record in
                      bots.list_bots(config.home, models=models())
                      if record.bot is not None and record.bot.id != current.id]
            bot = bots.validate(merged, bot_folder=folder(bot_id), models=models(),
                                handles=others, kova_home=config.home, persona=text)
        except BotError as exc:
            raise HTTPException(422, str(exc)) from None
        guard(request, bot)
        try:
            saved = await asyncio.to_thread(bots.save_bot, config.home, bot,
                                            models=models(), persona=bot.persona)
        except BotError as exc:
            raise HTTPException(422, str(exc)) from None
        publish(saved.id, "updated")
        return public(saved, persona=True)

    @router.delete("/bots/{bot_id}", dependencies=write)
    async def delete_bot(bot_id: str) -> dict:
        # A broken file must still be removable: never require it to load.
        if not folder(bot_id).is_dir():
            raise HTTPException(404, "bot not found")
        removed = await asyncio.to_thread(bots.delete_bot, config.home, bot_id)
        if not removed:
            raise HTTPException(422, "The bot folder could not be removed. Remove it yourself.")
        # Its private notes have no other owner, so they go with the definition.
        await asyncio.to_thread(app.state.memory.remove_bot_scope, bot_id)
        # Its chats and messages stay, with the author name they were written
        # under; a chat whose bot is gone answers with the default assistant.
        app.state.store.remove_bot_from_groups(bot_id)
        publish(bot_id, "deleted")
        return {"ok": True}

    @router.get("/bots/{bot_id}/avatar")
    async def get_avatar(bot_id: str) -> Response:
        bot = load(bot_id)
        path = bots.avatar_path(bot)
        if path is None:
            raise HTTPException(404, "This bot has no avatar image.")
        try:
            data = await asyncio.to_thread(path.read_bytes)
        except OSError:
            raise HTTPException(404, "This bot has no avatar image.") from None
        media = _AVATAR_MEDIA.get(path.suffix.lstrip(".").lower(), "application/octet-stream")
        return Response(content=data, media_type=media, headers={"Cache-Control": "no-store"})

    @router.put("/bots/{bot_id}/avatar", dependencies=write)
    async def set_avatar(bot_id: str, file: UploadFile = File(...)) -> dict:
        # An avatar changes how the bot looks, never what it may do, so the
        # host:exec rule for a privileged bot's tools does not apply here.
        bot = load(bot_id)
        suffix = Path(file.filename or "").suffix.lstrip(".").lower()
        media = (file.content_type or "").lower()
        if media not in AVATAR_TYPES:
            media = next((mime for ext, mime in _AVATAR_MEDIA.items() if ext == suffix), "")
        try:
            data = await file.read(bots.MAX_AVATAR_BYTES + 1)
            if len(data) > bots.MAX_AVATAR_BYTES:
                raise HTTPException(413, f"Avatar must be at most {bots.MAX_AVATAR_BYTES // 1024} KB.")
            updated = await asyncio.to_thread(bots.save_avatar, bot, data, media)
        except BotError as exc:
            raise HTTPException(422, str(exc)) from None
        finally:
            await file.close()
        publish(updated.id, "updated")
        return {"ok": True, "image": updated.avatar.image, "bot": public(updated)}

    @router.delete("/bots/{bot_id}/avatar", dependencies=write)
    async def clear_avatar(bot_id: str) -> dict:
        bot = load(bot_id)
        updated = await asyncio.to_thread(bots.clear_avatar, bot)
        publish(updated.id, "updated")
        return {"ok": True, "bot": public(updated)}

    app.include_router(router)



def actor_for_chat(app, config: Config, chat: dict | None) -> Actor | None:
    """Who answers in this chat: a bot's Actor, or None for today's behaviour.

    A bot chat whose bot was deleted or whose file is broken answers with the
    default assistant instead of failing: its history stays readable, and its
    author names are already stored on every message.
    """
    chat = chat or {}
    if chat.get("kind") != "bot" or not chat.get("bot_id"):
        return None
    bot = bots.get_bot(config.home, chat["bot_id"],
                       models=[entry.name for entry in config.models])
    if bot is None:
        return None
    return Actor.for_chat(chat, bot, files_path=config.files_path)


__all__ = ["actor_for_chat", "grants_host_access", "install_bot_routes"]
