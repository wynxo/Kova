"""Kova's FastAPI server: the single owner of chats, memory and reminders."""

from __future__ import annotations

import asyncio
import hmac
import json
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager, suppress
from datetime import datetime

import httpx
from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from kova import __version__
from kova.agent import Agent
from kova.config import Config, ModelEntry, ensure_token, save_config
from kova.events import EventBus
from kova.memory import Memory, Note
from kova.models import ModelClient
from kova.reminders import ReminderLoop
from kova.store import Store
from kova.tools import ToolContext, ToolError, build_tools
from kova.tools.reminders import parse_when


class TurnBody(BaseModel):
    """A user message that starts one chat turn."""
    text: str


class NoteBody(BaseModel):
    """A memory note to save."""
    title: str
    body: str
    tags: list[str] = Field(default_factory=list)


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
            "api_key": "***" if entry.api_key else None, "context": entry.context}


def _sse(event: dict) -> str:
    """One SSE frame for an event."""
    return f"event: {event['type']}\ndata: {json.dumps(event)}\n\n"


async def _require_token(request: Request) -> None:
    """Reject anything without the home's bearer token."""
    expected = request.app.state.token
    given = request.headers.get("authorization", "")
    if not hmac.compare_digest(given.encode(), f"Bearer {expected}".encode()):
        raise HTTPException(status_code=401, detail="missing or invalid token")


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Build the app's singletons and run the reminder loop for its lifetime."""
    config: Config = app.state.config
    now: Callable[[], datetime] = app.state.now
    store = Store(config.home / "kova.db")
    memory = Memory(config.memory_path, config.memory_write_subdir)
    tools = build_tools(ToolContext(config, store, memory, now))
    agent = Agent(config, store, memory, tools, transport=app.state.transport, now=now)
    bus = EventBus()
    app.state.store = store
    app.state.memory = memory
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

    Every route except ``GET /health`` needs the home's bearer token.
    """
    now = now or (lambda: datetime.now().astimezone())
    app = FastAPI(lifespan=_lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.config = config
    app.state.transport = transport
    app.state.now = now
    app.state.token = ensure_token(config.home)
    app.state.turns = set()
    app.state.chat_locks = {}

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
    async def create_chat() -> dict:
        return app.state.store.create_chat()

    @api.get("/chats/{chat_id}")
    async def get_chat(chat_id: str) -> dict:
        chat = app.state.store.get_chat(chat_id)
        if chat is None:
            raise HTTPException(status_code=404, detail="chat not found")
        return chat

    @api.delete("/chats/{chat_id}")
    async def delete_chat(chat_id: str) -> dict:
        async with chat_lock(chat_id):
            deleted = app.state.store.delete_chat(chat_id)
        app.state.chat_locks.pop(chat_id, None)
        if not deleted:
            raise HTTPException(status_code=404, detail="chat not found")
        return {"ok": True}

    @api.post("/chats/{chat_id}/messages")
    async def post_message(chat_id: str, body: TurnBody) -> StreamingResponse:
        if app.state.store.get_chat(chat_id) is None:
            raise HTTPException(status_code=404, detail="chat not found")
        queue: asyncio.Queue[dict | None] = asyncio.Queue()

        async def run_turn() -> None:
            """Drive the turn to completion even if the client disconnects."""
            try:
                async with chat_lock(chat_id):
                    if app.state.store.get_chat(chat_id) is None:
                        queue.put_nowait({"type": "error", "message": "chat was deleted"})
                        return
                    async for event in app.state.agent.run_turn(chat_id, body.text):
                        queue.put_nowait(event)
            except Exception as exc:  # never leave the stream without an ending
                queue.put_nowait({"type": "error", "message": str(exc)})
            finally:
                queue.put_nowait(None)

        task = asyncio.create_task(run_turn())
        app.state.turns.add(task)
        task.add_done_callback(app.state.turns.discard)

        async def drain() -> AsyncIterator[str]:
            while True:
                event = await queue.get()
                if event is None:
                    return
                yield _sse(event)

        return StreamingResponse(drain(), media_type="text/event-stream")

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
