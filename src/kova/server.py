"""Kova's FastAPI server: the single owner of chats, memory and reminders."""

from __future__ import annotations

import asyncio
import hmac
import json
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager, suppress
from datetime import datetime
from pathlib import Path
from typing import Literal

import httpx
from fastapi import APIRouter, Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from kova import __version__
from kova.agent import Agent
from kova.config import Config, ModelEntry, ensure_token, save_config
from kova.events import EventBus
from kova.memory import Memory, Note
from kova.models import ModelClient
from kova.modes import DEFAULT_MODE, MODES
from kova.reminders import ReminderLoop
from kova.store import Store
from kova.tools import ToolContext, ToolError, build_tools
from kova.tools.reminders import parse_when
from kova.web import install_web, require_browser_session


class TurnBody(BaseModel):
    """A user message that starts one chat turn, optionally on a named model."""
    text: str
    model: str | None = None


class ChatBody(BaseModel):
    """Optional fields for a new or updated chat; every field may be omitted."""
    title: str | None = None
    mode: str | None = None
    web: bool | None = None
    permission: Literal["manual", "auto", "full"] | None = None
    cwd: str | None = None


class ApprovalBody(BaseModel):
    decision: Literal["allow", "allow_session", "deny"]
    message: str | None = Field(default=None, max_length=4000)


def working_folder(value: str) -> str:
    if not value.strip() or "\0" in value:
        raise HTTPException(status_code=422, detail="working folder must be a non-empty path")
    try:
        path = Path(value).expanduser()
        if not path.is_absolute():
            raise ValueError("working folder must be absolute")
        path = path.resolve(strict=True)
        if not path.is_dir():
            raise ValueError("working folder is not a directory")
        return str(path)
    except (ValueError, OSError, RuntimeError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


class NoteBody(BaseModel):
    """A memory note to save."""
    title: str
    body: str
    tags: list[str] = Field(default_factory=list)


class MemoryLocationBody(BaseModel):
    """The root and write mode for memory."""
    path: str
    obsidian: bool = False


class ReminderBody(BaseModel):
    """A reminder to schedule."""
    when: str
    text: str


class ModelBody(BaseModel):
    """A model server entry to register (same name overwrites)."""
    name: str
    base_url: str
    model: str
    api_key: str | None = None
    context: int = 131072


class ActiveBody(BaseModel):
    """The name of the model to activate."""
    name: str


def _note_dict(note: Note) -> dict:
    """The JSON shape of a memory note."""
    return {"slug": note.slug, "title": note.title, "body": note.body,
            "tags": note.tags, "updated": note.updated}


def _model_dict(entry: ModelEntry) -> dict:
    """The JSON shape of a model entry."""
    return {"name": entry.name, "base_url": entry.base_url, "model": entry.model,
            "api_key": "***" if entry.api_key else None, "context": entry.context,
            "provider": entry.provider, "provider_name": entry._provider.name if entry._provider else entry.provider}


def _sse(event: dict) -> str:
    """One SSE frame for an event."""
    return f"event: {event['type']}\ndata: {json.dumps(event)}\n\n"


async def _require_bearer(request: Request) -> None:
    """Reject anything without the home's bearer token."""
    expected = request.app.state.token
    given = request.headers.get("authorization", "")
    if not hmac.compare_digest(given.encode(), f"Bearer {expected}".encode()):
        raise HTTPException(status_code=401, detail="missing or invalid token")


async def _require_token(request: Request) -> None:
    """Keep CLI bearer auth; browsers use a local, same-origin session."""
    if request.headers.get("authorization"):
        await _require_bearer(request)
    else:
        require_browser_session(request)


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Build the app's singletons and run the reminder loop for its lifetime."""
    config: Config = app.state.config
    now: Callable[[], datetime] = app.state.now
    store = Store(config.home / "kova.db", default_cwd=str(config.files_path.resolve()))
    memory = Memory(config.memory_path, config.memory_write_subdir)
    tool_context = ToolContext(config, store, memory, now)
    tools = build_tools(tool_context)
    agent = Agent(config, store, memory, tools, transport=app.state.transport, now=now)
    bus = EventBus()
    app.state.store = store
    app.state.memory = memory
    app.state.tool_context = tool_context
    app.state.agent = agent
    app.state.bus = bus
    reminders = asyncio.create_task(ReminderLoop(store, bus, now).run())
    try:
        yield
    finally:
        bus.publish(None)  # end every open /events stream
        reminders.cancel()
        for turn in list(app.state.turns):
            turn.cancel()
        for task in [reminders, *app.state.turns]:
            with suppress(asyncio.CancelledError):
                await task


def create_app(
    config: Config,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
    now: Callable[[], datetime] | None = None,
) -> FastAPI:
    """Build the Kova app for one config home.

    API routes need a bearer token or a same-origin browser session.
    The app shell and health endpoint are public; neither contains secrets.
    """
    now = now or (lambda: datetime.now().astimezone())
    app = FastAPI(lifespan=_lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.config = config
    app.state.transport = transport
    app.state.now = now
    app.state.token = ensure_token(config.home)
    app.state.turns = set()
    app.state.chat_locks = {}
    app.state.chat_turns = {}
    install_web(app, _require_bearer)
    from kova.provider_routes import install_provider_routes
    config.bind_providers()
    install_provider_routes(app, config, _require_token)

    def chat_lock(chat_id: str) -> asyncio.Lock:
        """One turn at a time per chat; deleting a chat waits for its turn."""
        return app.state.chat_locks.setdefault(chat_id, asyncio.Lock())

    @app.get("/health")
    async def health() -> dict:
        try:
            entry = config.active_model()
        except KeyError:
            return {"version": __version__, "model": config.active, "model_ok": False}
        reachable = await ModelClient(entry, transport=transport).ping()
        return {"version": __version__, "model": config.active, "model_ok": reachable}

    api = APIRouter(dependencies=[Depends(_require_token)])

    @api.get("/chats")
    async def list_chats() -> list[dict]:
        return app.state.store.list_chats()

    @api.post("/chats")
    async def create_chat(body: ChatBody | None = None) -> dict:
        mode = body.mode if body else None
        if mode is not None and mode not in MODES:
            raise HTTPException(status_code=422, detail=f"unknown mode '{mode}'")
        return app.state.store.create_chat(
            body.title if body else None,
            mode=mode or DEFAULT_MODE,
            web=body.web if body is not None and body.web is not None else True,
            permission=body.permission if body and body.permission else "manual",
            cwd=working_folder(body.cwd) if body and body.cwd is not None else str(config.files_path.resolve()),
        )

    @api.get("/chats/search")
    async def search_chats(q: str = "", limit: int = Query(20, ge=1, le=100)) -> list[dict]:
        """Chats with a user/assistant message matching ``q``, newest first."""
        return await asyncio.to_thread(app.state.store.search_chats, q, limit)

    @api.get("/chats/{chat_id}")
    async def get_chat(chat_id: str) -> dict:
        chat = app.state.store.get_chat(chat_id)
        if chat is None:
            raise HTTPException(status_code=404, detail="chat not found")
        return chat

    @api.patch("/chats/{chat_id}")
    async def update_chat(chat_id: str, body: ChatBody) -> dict:
        changes: dict[str, str | bool] = {}
        if body.title is not None:
            title = body.title.strip()
            if not title:
                raise HTTPException(status_code=422, detail="title cannot be empty")
            changes["title"] = title
        if body.mode is not None:
            if body.mode not in MODES:
                raise HTTPException(status_code=422, detail=f"unknown mode '{body.mode}'")
            changes["mode"] = body.mode
        if body.web is not None:
            changes["web"] = body.web
        if body.permission is not None:
            changes["permission"] = body.permission
        if body.cwd is not None:
            changes["cwd"] = working_folder(body.cwd)
        if not changes:
            raise HTTPException(status_code=422, detail="nothing to update")
        if app.state.store.get_chat(chat_id) is None:
            raise HTTPException(status_code=404, detail="chat not found")
        if any(not task.done() for task in app.state.chat_turns.get(chat_id, ())):
            raise HTTPException(status_code=409, detail="Stop the active turn before changing chat settings")
        app.state.store.update_chat(chat_id, **changes)
        if body.permission is not None or body.cwd is not None:
            app.state.agent.approvals.sessions.discard(chat_id)
        chat = app.state.store.get_chat(chat_id)
        return {key: chat[key] for key in ("id", "title", "mode", "web", "permission", "cwd")}

    @api.delete("/chats/{chat_id}")
    async def delete_chat(chat_id: str) -> dict:
        async with chat_lock(chat_id):
            deleted = app.state.store.delete_chat(chat_id)
        app.state.chat_locks.pop(chat_id, None)
        if not deleted:
            raise HTTPException(status_code=404, detail="chat not found")
        app.state.agent.approvals.sessions.discard(chat_id)
        for key in list(app.state.agent.file_sessions):
            if key[0] == chat_id:
                del app.state.agent.file_sessions[key]
        return {"ok": True}

    @api.post("/chats/{chat_id}/approvals/{approval_id}")
    async def answer_approval(chat_id: str, approval_id: str, body: ApprovalBody) -> dict:
        try:
            app.state.agent.approvals.answer(chat_id, approval_id, body.decision, body.message)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc.args[0])) from exc
        return {"ok": True}

    @api.post("/chats/{chat_id}/compact")
    async def compact_chat(chat_id: str) -> dict:
        from kova.compact import compact
        from kova.models import ModelClient
        async with chat_lock(chat_id):
            if app.state.store.get_chat(chat_id) is None:
                raise HTTPException(status_code=404, detail="chat not found")
            entry = config.active_model()
            if not entry.model:
                raise HTTPException(status_code=422, detail="No model is configured")
            event = await compact(app.state.store, chat_id, ModelClient(entry, transport=app.state.agent.transport),
                                  context_tokens=entry.context, threshold=config.compact_threshold, force=True)
            return event or {"type": "compact_skipped", "message": "Nothing to compact, or the summary failed."}

    @api.post("/chats/{chat_id}/messages")
    async def post_message(chat_id: str, body: TurnBody) -> StreamingResponse:
        if app.state.store.get_chat(chat_id) is None:
            raise HTTPException(status_code=404, detail="chat not found")
        if body.model is not None and not any(m.name == body.model for m in config.models):
            raise HTTPException(status_code=404, detail=f"unknown model '{body.model}'")
        queue: asyncio.Queue[dict | None] = asyncio.Queue()

        async def run_turn() -> None:
            """Drive the turn to completion even if the client disconnects."""
            try:
                async with chat_lock(chat_id):
                    if app.state.store.get_chat(chat_id) is None:
                        queue.put_nowait({"type": "error", "message": "chat was deleted"})
                        return
                    async for event in app.state.agent.run_turn(chat_id, body.text, model=body.model):
                        queue.put_nowait(event)
            except asyncio.CancelledError:
                queue.put_nowait({"type": "stopped"})
                raise
            except Exception as exc:  # never leave the stream without an ending
                queue.put_nowait({"type": "error", "message": str(exc)})
            finally:
                queue.put_nowait(None)

        task = asyncio.create_task(run_turn())
        app.state.turns.add(task)
        tasks = app.state.chat_turns.setdefault(chat_id, set())
        tasks.add(task)

        def finished(done):
            app.state.turns.discard(done)
            tasks.discard(done)
            if not tasks:
                app.state.chat_turns.pop(chat_id, None)
            queue.put_nowait(None)  # also handles cancellation before task startup

        task.add_done_callback(finished)

        async def drain() -> AsyncIterator[str]:
            while True:
                event = await queue.get()
                if event is None:
                    return
                yield _sse(event)

        return StreamingResponse(drain(), media_type="text/event-stream")


    @api.post("/chats/{chat_id}/stop")
    async def stop_chat(chat_id: str) -> dict:
        """Cancel this chat's server-side turns, not merely its HTTP stream.

        Commands terminate their process trees; pending approvals are denied.
        A synchronous file operation already in a worker cannot be undone.
        """
        if app.state.store.get_chat(chat_id) is None:
            raise HTTPException(status_code=404, detail="chat not found")
        tasks = [task for task in app.state.chat_turns.get(chat_id, ()) if not task.done()]
        for task in tasks:
            if not task.cancelling():
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        return {"stopped": bool(tasks)}


    @api.post("/memory/location")
    async def memory_use(body: MemoryLocationBody) -> dict:
        memory = Memory(Path(body.path), "Kova" if body.obsidian else None)
        config.memory_path = memory.root
        config.memory_write_subdir = memory.write_subdir
        save_config(config)
        # No await: requests and new tool calls see the replacement together.
        app.state.memory = memory
        app.state.agent.memory = memory
        app.state.tool_context.memory = memory
        return {"ok": True}
    @api.get("/memory")
    async def memory_list(q: str | None = None) -> list[dict]:
        mem = app.state.memory
        notes = await asyncio.to_thread(mem.search, q) if q else await asyncio.to_thread(mem.list)
        return [_note_dict(note) for note in notes]

    @api.post("/memory")
    async def memory_add(body: NoteBody) -> dict:
        try:
            note = await asyncio.to_thread(app.state.memory.save, body.title, body.body, body.tags)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return _note_dict(note)

    @api.delete("/memory/{slug}")
    async def memory_delete(slug: str) -> dict:
        if not app.state.memory.delete(slug):
            raise HTTPException(status_code=404, detail="note not found")
        return {"ok": True}

    @api.get("/reminders")
    async def reminders_list() -> list[dict]:
        return app.state.store.list_reminders()

    @api.post("/reminders")
    async def reminder_add(body: ReminderBody) -> dict:
        try:
            when = parse_when(body.when, now())
        except ToolError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        rid = app.state.store.add_reminder(when, body.text)
        return {"id": rid, "when": when.isoformat(), "text": body.text, "done": False}

    @api.delete("/reminders/{rid}")
    async def reminder_delete(rid: int) -> dict:
        if not app.state.store.delete_reminder(rid):
            raise HTTPException(status_code=404, detail="reminder not found")
        return {"ok": True}

    @api.get("/models")
    async def models_list() -> list[dict]:
        config.bind_providers()
        return [_model_dict(entry) for entry in config.models]

    @api.post("/models")
    async def model_add(body: ModelBody) -> dict:
        entry = ModelEntry(name=body.name, base_url=body.base_url, model=body.model,
                           api_key=body.api_key, context=body.context)
        config.models[:] = [m for m in config.models if m.name != entry.name]
        config.models.append(entry)
        save_config(config)
        return _model_dict(entry)

    @api.post("/models/active")
    async def model_use(body: ActiveBody) -> dict:
        if not any(m.name == body.name for m in config.models):
            raise HTTPException(status_code=404, detail=f"unknown model '{body.name}'")
        config.active = body.name
        save_config(config)
        return {"active": body.name}

    @api.get("/events")
    async def events() -> StreamingResponse:
        queue = app.state.bus.subscribe()

        async def stream() -> AsyncIterator[str]:
            try:
                while True:
                    event = await queue.get()
                    if event is None:  # server shutting down
                        return
                    yield _sse(event)
            finally:
                app.state.bus.unsubscribe(queue)

        return StreamingResponse(stream(), media_type="text/event-stream")

    app.include_router(api)
    return app
