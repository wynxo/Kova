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

from kova import __version__, checkpoints
from kova.agent import Agent, Steering, derive_title
from kova.config import Config, ModelEntry, ensure_token, load_config, save_config
from kova.events import EventBus, ConversationStates
from kova.memory import Memory, Note
from kova.models import ModelClient, ModelError
from kova.modes import DEFAULT_MODE, MODES, capabilities as mode_capabilities
from kova.reminders import ReminderLoop
from kova.scheduler import Scheduler, parse_schedule
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


class PersonaBody(BaseModel):
    name: str
    tone: str


class ApprovalBody(BaseModel):
    decision: Literal["allow", "allow_session", "allow_always", "deny"]
    message: str | None = Field(default=None, max_length=4000)


class QueuedBody(BaseModel):
    """A message to run after the chat's current turn ends."""
    text: str = Field(min_length=1, max_length=100_000)
    model: str | None = None


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


class MemoryEditBody(BaseModel):
    """An in-place edit of one memory note; omitted fields stay as they are."""
    title: str | None = None
    body: str | None = None
    tags: list[str] | None = None


class MemoryUndoBody(BaseModel):
    """Undo the most recent background memory changes, or one named revision."""
    revision_id: int | None = None


class MemorySettingsBody(BaseModel):
    """Opt-in switches and gates for the background memory passes; omitted fields stay."""

    review: bool | None = None
    review_every: int | None = None
    consolidate: bool | None = None
    consolidate_hour: int | None = None
    consolidate_min_confidence: float | None = None
    consolidate_max_loss: float | None = None


class ConsolidateBody(BaseModel):
    """Plan (and by default apply) one consolidation run."""
    dry_run: bool = False


class MemoryLocationBody(BaseModel):
    """The root and write mode for memory."""
    path: str
    obsidian: bool = False


class ReminderBody(BaseModel):
    """A reminder to schedule."""
    when: str
    text: str


class JobBody(BaseModel):
    """A recurring job: a shell command, or one agent turn, on a repeat schedule."""
    text: str
    schedule: str
    command: str | None = None
    prompt: str | None = None
    first_run: str | None = None
    idle_only: bool = False


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
    """The JSON shape of a memory note, including where the fact came from."""
    return {"slug": note.slug, "title": note.title, "body": note.body,
            "tags": note.tags, "updated": note.updated,
            "origin": note.origin, "source": note.source, "created": note.created}


def _memory_settings(config: Config) -> dict:
    """The background memory passes' switches and gates, as the CLI shows them."""
    return {"review": config.memory_review, "review_every": config.memory_review_every,
            "consolidate": config.memory_consolidate,
            "consolidate_hour": config.memory_consolidate_hour,
            "consolidate_min_confidence": config.memory_consolidate_min_confidence,
            "consolidate_max_loss": config.memory_consolidate_max_loss}


def _refresh_review(store: Store, config: Config):
    """Rebuild the agent's background passes after a settings change, or None when off."""
    from kova.review import MemoryReview, ReviewSettings
    settings = ReviewSettings.from_config(config)
    return MemoryReview(store, settings) if settings.enabled else None


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
    memory = Memory(config.memory_path, config.memory_write_subdir, config.memory_budget)
    tool_context = ToolContext(config, store, memory, now)
    tools = build_tools(tool_context)
    agent = Agent(config, store, memory, tools, transport=app.state.transport, now=now)
    bus = EventBus()
    app.state.store = store
    app.state.memory = memory
    app.state.tool_context = tool_context
    app.state.agent = agent
    app.state.bus = bus
    app.state.conversation_states = ConversationStates(bus)

    async def run_job_prompt(job: dict) -> str:
        """Run one prompt job's turn, in the job's own chat, and return its reply."""
        chat_id = job.get("chat_id")
        if not chat_id or app.state.store.get_chat(chat_id) is None:
            chat = app.state.store.create_chat(title=derive_title(f"Job: {job['text']}"),
                                               cwd=str(config.files_path.resolve()))
            chat_id = chat["id"]
            app.state.store.set_job_chat(job["id"], chat_id)
        parts: list[str] = []
        async with app.state.chat_locks.setdefault(chat_id, asyncio.Lock()):
            app.state.conversation_states.transition(chat_id, "turn_start")
            try:
                async for event in app.state.agent.run_turn(chat_id, job["prompt"]):
                    if event.get("type") == "text":
                        parts.append(event["text"])
                    elif event.get("type") == "error":
                        raise RuntimeError(event.get("message") or "the job's turn failed")
            finally:
                app.state.conversation_states.transition(chat_id, "turn_end")
        return "".join(parts).strip() or "(the turn ended without a reply)"

    def is_idle() -> bool:
        """A heartbeat waits for the user's work to finish, and for other jobs."""
        return (not app.state.turns and not app.state.scheduler.busy
                and not app.state.agent.approvals.pending)

    scheduler = Scheduler(store, bus, now, cwd=config.files_path,
                          run_prompt=run_job_prompt, is_idle=is_idle)
    app.state.scheduler = scheduler
    reminders = asyncio.create_task(ReminderLoop(store, bus, now, scheduler=scheduler).run())
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
    app.state.steering = {}
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

    @api.get("/persona")
    async def persona() -> dict:
        current = load_config(config.home)
        return {"name": current.persona_name, "tone": current.persona_tone,
                "configured": current.persona_configured}

    @api.post("/persona")
    async def set_persona(body: PersonaBody) -> dict:
        from kova.persona import Persona
        try:
            identity = Persona(body.name.strip(), body.tone)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        current = load_config(config.home)
        current.persona_name, current.persona_tone, current.persona_configured = identity.name, identity.tone, True
        save_config(current)
        return {"name": identity.name, "tone": identity.tone, "configured": True}

    @api.get("/chats")
    async def list_chats() -> list[dict]:
        return [{**chat, "state": app.state.conversation_states.get(chat["id"])} for chat in app.state.store.list_chats()]

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
        chats = await asyncio.to_thread(app.state.store.search_chats, q, limit)
        return [{**chat, "state": app.state.conversation_states.get(chat["id"])} for chat in chats]

    @api.post("/chats/{chat_id}/fork")
    async def fork_chat(chat_id: str) -> dict:
        async with chat_lock(chat_id):
            chat = app.state.store.fork_chat(chat_id)
            if chat is None:
                raise HTTPException(status_code=404, detail="chat not found")
            return chat

    @api.get("/chats/{chat_id}")
    async def get_chat(chat_id: str) -> dict:
        chat = app.state.store.get_chat(chat_id)
        if chat is None:
            raise HTTPException(status_code=404, detail="chat not found")
        return {**chat, "state": app.state.conversation_states.get(chat_id)}

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

    @api.get("/permission-rules")
    async def permission_rules() -> list[dict]:
        from dataclasses import asdict
        return [asdict(r) for r in app.state.agent.approvals.rules]

    @api.put("/permission-rules")
    async def replace_permission_rules(body: list[dict]) -> dict:
        from kova.permissions import Rule
        try:
            rules = [Rule(**r) for r in body]
        except (TypeError, ValueError) as exc:
            raise HTTPException(422, str(exc)) from exc
        approvals = app.state.agent.approvals
        previous = approvals.rules
        approvals.rules = rules
        try:
            approvals.save()
        except OSError:
            approvals.rules = previous
            raise
        return {"ok": True}

    @api.post("/chats/{chat_id}/approvals/{approval_id}")
    async def answer_approval(chat_id: str, approval_id: str, body: ApprovalBody) -> dict:
        chat = app.state.store.get_chat(chat_id)
        if chat is None:
            raise HTTPException(status_code=404, detail="chat not found")
        if "approvals" not in mode_capabilities(chat.get("mode") or DEFAULT_MODE):
            raise HTTPException(status_code=409, detail="This chat's mode does not ask for approvals")
        pending = app.state.agent.approvals.pending.get(approval_id)
        if pending and pending.chat_id == chat_id and pending.tool == "ask_user":
            if body.decision not in {"allow", "deny"} or (body.decision == "allow" and not (body.message or "").strip()):
                raise HTTPException(status_code=422, detail="A question requires a non-empty answer")
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
            steering = Steering()
            try:
                async with chat_lock(chat_id):
                    if app.state.store.get_chat(chat_id) is None:
                        queue.put_nowait({"type": "error", "message": "chat was deleted"})
                        return
                    app.state.conversation_states.transition(chat_id, "turn_start")
                    app.state.steering[chat_id] = steering
                    try:
                        async for event in app.state.agent.run_turn(chat_id, body.text, model=body.model,
                                                                  steering=steering):
                            app.state.conversation_states.transition(chat_id, event.get("type", ""))
                            queue.put_nowait(event)
                    finally:
                        app.state.conversation_states.transition(chat_id, "turn_end")
            except asyncio.CancelledError:
                queue.put_nowait({"type": "stopped"})
                raise
            except Exception as exc:  # never leave the stream without an ending
                queue.put_nowait({"type": "error", "message": str(exc)})
            finally:
                if app.state.steering.get(chat_id) is steering:
                    app.state.steering.pop(chat_id, None)
                # A steer that arrived after the last model round must not be
                # lost: it becomes the next message in the chat's queue.
                for message in steering.take():
                    app.state.store.queue_message(chat_id, message)
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


    @api.get("/chats/{chat_id}/queue")
    async def list_queue(chat_id: str) -> list[dict]:
        """Messages waiting to run after this chat's current turn."""
        if app.state.store.get_chat(chat_id) is None:
            raise HTTPException(status_code=404, detail="chat not found")
        return app.state.store.queued_messages(chat_id)

    @api.post("/chats/{chat_id}/queue")
    async def queue_message(chat_id: str, body: QueuedBody) -> dict:
        """Queue a message; it survives restarts until a client runs it."""
        if app.state.store.get_chat(chat_id) is None:
            raise HTTPException(status_code=404, detail="chat not found")
        if body.model is not None and not any(m.name == body.model for m in config.models):
            raise HTTPException(status_code=404, detail=f"unknown model '{body.model}'")
        row = app.state.store.queue_message(chat_id, body.text, body.model)
        message = next(entry for entry in app.state.store.queued_messages(chat_id)
                       if entry["id"] == row)
        return message

    @api.delete("/chats/{chat_id}/queue/{message_id}")
    async def delete_queued(chat_id: str, message_id: int) -> dict:
        if not app.state.store.delete_queued_message(chat_id, message_id):
            raise HTTPException(status_code=404, detail="queued message not found")
        return {"ok": True}

    @api.post("/chats/{chat_id}/queue/next")
    async def next_queued(chat_id: str) -> dict:
        """Take the oldest queued message, for a client to send as its next turn."""
        if app.state.store.get_chat(chat_id) is None:
            raise HTTPException(status_code=404, detail="chat not found")
        return app.state.store.pop_queued_message(chat_id) or {"message": None}

    @api.post("/chats/{chat_id}/steer")
    async def steer(chat_id: str, body: QueuedBody) -> dict:
        """Put a message into the turn that is running now, not the queue."""
        if app.state.store.get_chat(chat_id) is None:
            raise HTTPException(status_code=404, detail="chat not found")
        steering = app.state.steering.get(chat_id)
        if steering is None:
            raise HTTPException(status_code=409,
                                detail="No reply is running. Queue the message or send it as a new one.")
        steering.add(body.text)
        return {"ok": True, "text": body.text}

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

    def checkpoint_or_404(chat_id: str, checkpoint_id: int) -> dict:
        row = app.state.store.get_checkpoint(chat_id, checkpoint_id)
        if row is None:
            raise HTTPException(status_code=404, detail="checkpoint not found")
        return row

    @api.get("/chats/{chat_id}/checkpoints")
    async def list_checkpoints(chat_id: str) -> list[dict]:
        """This chat's change sets, newest first: what each turn changed."""
        if app.state.store.get_chat(chat_id) is None:
            raise HTTPException(status_code=404, detail="chat not found")
        return await asyncio.to_thread(app.state.store.list_checkpoints, chat_id)

    @api.get("/chats/{chat_id}/checkpoints/{checkpoint_id}")
    async def get_checkpoint(chat_id: str, checkpoint_id: int) -> dict:
        return checkpoint_or_404(chat_id, checkpoint_id)

    @api.get("/chats/{chat_id}/checkpoints/{checkpoint_id}/diff")
    async def checkpoint_diff(chat_id: str, checkpoint_id: int, path: str) -> dict:
        """One changed file's diff and both sides, to preview before restoring."""
        row = checkpoint_or_404(chat_id, checkpoint_id)
        entry = (row.get("files") or {}).get(path)
        if entry is None:
            raise HTTPException(status_code=404, detail="file is not part of this checkpoint")
        return await asyncio.to_thread(checkpoints.preview, app.state.agent.blobs, path, entry)

    @api.post("/chats/{chat_id}/checkpoints/{checkpoint_id}/restore")
    async def restore_checkpoint(chat_id: str, checkpoint_id: int) -> dict:
        """Put the working folder back to this checkpoint.

        Only the paths the reverted turns changed are touched; the user's git
        repository is never read or written.
        """
        row = checkpoint_or_404(chat_id, checkpoint_id)
        if any(not task.done() for task in app.state.chat_turns.get(chat_id, ())):
            raise HTTPException(status_code=409, detail="Stop the active turn before restoring files")
        rows = await asyncio.to_thread(app.state.store.checkpoints_from, chat_id, checkpoint_id)
        plan = checkpoints.revert_plan(rows, checkpoint_id)
        result = await asyncio.to_thread(checkpoints.apply_restore, Path(row["root"]), plan,
                                         app.state.agent.blobs)
        return {"ok": not result["failed"], "checkpoint_id": checkpoint_id, "root": row["root"], **result}


    @api.post("/memory/location")
    async def memory_use(body: MemoryLocationBody) -> dict:
        memory = Memory(Path(body.path), "Kova" if body.obsidian else None, config.memory_budget)
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
        # The same hybrid ranking the prompt recall uses: keyword hits ordered
        # by the deterministic lexical signal, so CLI and web searches agree.
        notes = await asyncio.to_thread(mem.search, q, 50, mode="hybrid") if q \
            else await asyncio.to_thread(mem.list)
        return [_note_dict(note) for note in notes]

    @api.post("/memory")
    async def memory_add(body: NoteBody) -> dict:
        try:
            note = await asyncio.to_thread(app.state.memory.save, body.title, body.body, body.tags)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return _note_dict(note)

    # Static routes must be declared before "/memory/{slug}", or FastAPI matches
    # PATCH /memory/settings against the note-edit route with slug="settings".
    @api.get("/memory/settings")
    async def memory_settings_get() -> dict:
        """The opt-in switches and gates of the background memory passes."""
        return _memory_settings(config)

    @api.patch("/memory/settings")
    async def memory_settings_set(body: MemorySettingsBody) -> dict:
        """Turn the background memory passes on or off and tune their gates."""
        updates = body.model_dump(exclude_unset=True)
        if "review_every" in updates and updates["review_every"] < 1:
            raise HTTPException(status_code=422, detail="review_every must be at least 1")
        if "consolidate_hour" in updates and not 0 <= updates["consolidate_hour"] <= 23:
            raise HTTPException(status_code=422, detail="consolidate_hour must be 0-23")
        for key in ("consolidate_min_confidence", "consolidate_max_loss"):
            if key in updates and not 0 <= updates[key] <= 1:
                raise HTTPException(status_code=422, detail=f"{key} must be 0-1")
        for key, value in updates.items():
            setattr(config, f"memory_{key}", value)
        save_config(config)
        app.state.agent.memory_review = _refresh_review(app.state.store, config)
        return _memory_settings(config)

    @api.delete("/memory/{slug}")
    async def memory_delete(slug: str) -> dict:
        if not app.state.memory.delete(slug):
            raise HTTPException(status_code=404, detail="note not found")
        return {"ok": True}

    @api.patch("/memory/{slug}")
    async def memory_edit(slug: str, body: MemoryEditBody) -> dict:
        """Rewrite one note in place, keeping its slug, provenance and creation date."""
        if body.title is None and body.body is None and body.tags is None:
            raise HTTPException(status_code=422, detail="nothing to update")
        try:
            note = await asyncio.to_thread(app.state.memory.update, slug,
                                           title=body.title, body=body.body, tags=body.tags)
        except ValueError as exc:
            status = 422 if "No note named" not in str(exc) else 404
            raise HTTPException(status_code=status, detail=str(exc)) from exc
        return _note_dict(note)

    @api.get("/memory/diary")
    async def memory_diary(limit: int = Query(50, ge=1, le=500)) -> list[dict]:
        """What the background memory passes did, oldest first, for a person to read."""
        return app.state.store.memory_diary(limit=limit)

    @api.get("/memory/revisions")
    async def memory_revisions(limit: int = Query(50, ge=1, le=500)) -> list[dict]:
        """Recent background memory changes, newest first, with their undo state."""
        return app.state.store.memory_revisions(limit=limit)

    @api.post("/memory/undo")
    async def memory_undo(body: MemoryUndoBody | None = None) -> dict:
        """Reverse recorded review/consolidation writes from their preimages."""
        from kova.review import undo
        undone = await asyncio.to_thread(undo, app.state.memory, app.state.store,
                                         revision_id=body.revision_id if body else None)
        return {"undone": undone}

    @api.post("/memory/consolidate")
    async def memory_consolidate(body: ConsolidateBody | None = None) -> dict:
        """Plan one consolidation run and, unless asked to dry-run, apply it."""
        from kova.review import gate_operations, plan_consolidation, apply_operations
        entry = config.active_model()
        if not entry.model:
            raise HTTPException(status_code=422, detail="No model is configured")
        client = ModelClient(entry, transport=app.state.agent.transport)
        memory, store = app.state.memory, app.state.store
        try:
            operations = await plan_consolidation(memory, client)
        except ModelError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        accepted, refused = await asyncio.to_thread(
            gate_operations, operations, memory.list(),
            min_confidence=config.memory_consolidate_min_confidence,
            max_loss=config.memory_consolidate_max_loss)
        applied = [] if (body and body.dry_run) else await asyncio.to_thread(
            apply_operations, store, memory, accepted)
        return {
            "planned": [{"op": op.kind, "slugs": op.slugs, "title": op.title,
                         "body": op.body, "confidence": op.confidence,
                         "reason": op.reason} for op in operations],
            "accepted": [{"op": op.kind, "slugs": op.slugs} for op in accepted],
            "refused": refused,
            "applied": applied,
            "dry_run": bool(body and body.dry_run),
        }

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

    @api.get("/jobs")
    async def jobs_list() -> list[dict]:
        return app.state.store.list_jobs()

    @api.post("/jobs")
    async def job_add(body: JobBody) -> dict:
        if not body.text.strip():
            raise HTTPException(status_code=422, detail="a job needs a short description")
        command, prompt = (body.command or "").strip(), (body.prompt or "").strip()
        if bool(command) == bool(prompt):
            raise HTTPException(status_code=422, detail="give exactly one of command or prompt")
        try:
            schedule = parse_schedule(body.schedule, now())
            first = parse_when(body.first_run, now()) if body.first_run else schedule.next_after(now())
        except ToolError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        jid = app.state.store.add_job(body.text.strip(), schedule.spec, first, command=command or None,
                                      prompt=prompt or None, idle_only=body.idle_only)
        return next(job for job in app.state.store.list_jobs(include_done=True) if job["id"] == jid)

    @api.delete("/jobs/{jid}")
    async def job_delete(jid: int) -> dict:
        if not app.state.store.delete_job(jid):
            raise HTTPException(status_code=404, detail="job not found")
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
