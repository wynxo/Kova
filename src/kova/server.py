"""Kova's FastAPI server: the single owner of chats, memory and reminders."""

from __future__ import annotations

import asyncio
import hmac
import json
import time
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager, suppress
from datetime import datetime
from pathlib import Path
from typing import Literal

import httpx
from fastapi import APIRouter, Depends, FastAPI, HTTPException, Query, Request, UploadFile, File
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from kova import __version__, checkpoints, pairing, plugins, egress
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
from kova.voice import TranscriptionError, build_transcriber, MAX_AUDIO_BYTES


class PrivacyBody(BaseModel):
    local_only: bool = Field(strict=True)


class TurnBody(BaseModel):
    """A user message that starts one chat turn, optionally on a named model."""
    text: str
    model: str | None = None
    attachments: list[str] = Field(default_factory=list, max_length=3)
    #: Send this queued message (its id), bound to the limits of whoever queued it.
    queued: int | None = None


class ChatBody(BaseModel):
    title: str | None = None
    mode: str | None = None
    web: bool | None = None
    permission: Literal["manual", "auto", "full"] | None = None
    cwd: str | None = None
    ephemeral: bool | None = None


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


def memory_folder(value: str) -> Path:
    """Validate and normalise a memory root so a restart reads the same folder (#90)."""
    if not value.strip() or "\0" in value:
        raise HTTPException(status_code=422, detail="memory folder must be a non-empty path")
    try:
        path = Path(value.strip()).expanduser()
        if not path.is_absolute():
            raise ValueError("memory folder must be absolute")
        path = path.resolve(strict=False)
        path.mkdir(parents=True, exist_ok=True)
        if not path.is_dir():
            raise ValueError("memory folder is not a directory")
        return path
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
    vision: bool = False


class ModelPatch(BaseModel):
    """Per-model settings to change; omitted fields stay as they are."""
    name: str
    vision: bool | None = None


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
            "vision": entry.vision,
            "provider": entry.provider, "provider_name": entry._provider.name if entry._provider else entry.provider}


EVENTS_KEEPALIVE = 15.0  # seconds between /events pings; the web UI reconnects after 3 missed
HEALTH_TTL = 15.0  # seconds a cached model reachability result stays fresh


class _ModelHealth:
    """Cached reachability of the active model, refreshed off the request path.

    A warm value answers immediately. A stale one is returned as-is while one
    background task probes, so any number of windows share a single provider
    request per TTL. The first request for a new active model waits for that
    one probe so a freshly opened window starts from a known value, while
    ``probe=0`` never waits (server startup only needs to know the process is
    up) and ``probe=1`` forces a live check for ``kova status``/``doctor``/setup.
    """

    def __init__(self, config: Config, transport: httpx.AsyncBaseTransport | None,
                 now: Callable[[], datetime]) -> None:
        self._config = config
        self._transport = transport
        self._now = now
        self._key: tuple | None = None
        self._ok = False
        self._checked: float | None = None
        self._checked_at: datetime | None = None
        self._task: asyncio.Task | None = None
        self._lock = asyncio.Lock()

    def _entry(self) -> ModelEntry | None:
        try:
            return self._config.active_model()
        except KeyError:
            return None

    @staticmethod
    def _signature(entry: ModelEntry) -> tuple:
        kind = entry._provider.kind if entry._provider else ""
        return (entry.name, entry.base_url, entry.model, kind)

    def _fresh(self, entry: ModelEntry) -> bool:
        return (self._signature(entry) == self._key and self._checked is not None
                and time.monotonic() - self._checked < HEALTH_TTL)

    def snapshot(self) -> dict:
        """The last known value, without touching the provider."""
        entry = self._entry()
        if entry is None or self._signature(entry) != self._key:
            return {"model_ok": False, "model_checked_at": None}
        return {"model_ok": self._ok,
                "model_checked_at": self._checked_at.isoformat() if self._checked_at else None}

    async def get(self, *, force: bool = False, wait: bool = True) -> dict:
        """Return the cached value; only a cold cache (or ``force``) waits."""
        entry = self._entry()
        if entry is None:
            return self.snapshot()
        key = self._signature(entry)
        if force:
            await self._refresh(entry, key, force=True)
        elif key != self._key:
            if wait:
                await self._refresh(entry, key, force=False)
            else:
                self._start(entry, key)
        elif not self._fresh(entry):
            self._start(entry, key)
        return self.snapshot()

    def _start(self, entry: ModelEntry, key: tuple) -> None:
        if self._task is not None and not self._task.done():
            return
        self._task = asyncio.create_task(self._background(entry, key))

    async def _background(self, entry: ModelEntry, key: tuple) -> None:
        await self._refresh(entry, key, force=False)

    async def _refresh(self, entry: ModelEntry, key: tuple, *, force: bool) -> None:
        async with self._lock:
            # Waiters queued behind a probe reuse its result instead of repeating it.
            if not force and self._fresh(entry):
                return
            try:
                ok = await ModelClient(entry, transport=self._transport, config=self._config).ping()
            except Exception:  # a broken probe must never kill the health route
                ok = False
            self._key, self._ok = key, ok
            self._checked = time.monotonic()
            self._checked_at = self._now()

    async def close(self) -> None:
        task = self._task
        if task is not None and not task.done():
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task


def _sse(event: dict) -> str:
    """One SSE frame for an event."""
    return f"event: {event['type']}\ndata: {json.dumps(event)}\n\n"


async def _require_bearer(request: Request) -> None:
    """Reject anything without the home's bearer token."""
    if request.scope.get("kova_lan"):
        raise HTTPException(status_code=403, detail="The Kova token works only on this computer.")
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
async def _lifespan(app):
    with egress.scope(config=app.state.config):
        async with _scoped_lifespan(app):
            yield


@asynccontextmanager
async def _scoped_lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Build the app's singletons and run the reminder loop for its lifetime."""
    config: Config = app.state.config
    now: Callable[[], datetime] = app.state.now
    store = Store(config.home / "kova.db", default_cwd=str(config.files_path.resolve()))
    config._egress_store = store
    memory = Memory(config.memory_path, config.memory_write_subdir, config.memory_budget)
    from kova.mcp import McpManager
    mcp = McpManager(config.home)
    tool_context = ToolContext(config, store, memory, now, mcp=mcp)
    tools = build_tools(tool_context)
    agent = Agent(config, store, memory, tools, transport=app.state.transport, now=now)
    bus = EventBus()
    app.state.store = store
    # Share tokens live only in memory. Any draft a previous process journaled
    # is therefore unreachable now: remove the empty conversation it left.
    for draft_chat in store.reap_share_drafts():
        store.delete_chat(draft_chat)
    app.state.memory = memory
    app.state.tool_context = tool_context
    app.state.agent = agent
    app.state.mcp = mcp
    # MCP servers connect in the background (npx may download first), so the
    # server answers at once; their tools join the registry when ready.
    mcp_connect = asyncio.create_task(app.state.mcp_reload()) if not config.privacy_local_only and (config.mcp or plugins.plugin_mcp_entries(config.home)) else None
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
    from kova import attachments
    # A sweep before the first request makes expiry deterministic after a
    # restart; the loop then bounds abandoned uploads in a long-lived server.
    with suppress(Exception):
        attachments.reap(store, now=now)

    async def sweep_attachments() -> None:
        while True:
            await asyncio.sleep(attachments.SWEEP_INTERVAL.total_seconds())
            try:
                await asyncio.to_thread(attachments.reap, store, now=now)
            except Exception:
                pass  # cleanup must never stop the server

    cleanup = asyncio.create_task(sweep_attachments())
    from kova.managed_engine import manager
    engine = manager(config)
    # Recover an owned orphan from a previous server, then load only on a chat.
    await engine.stop()
    engine_idle = asyncio.create_task(engine.idle_loop())
    if config.bind == "lan":
        await pairing.start_lan(app)
    try:
        yield
    finally:
        bus.publish(None)  # end every open /events stream
        await pairing.stop_lan(app)
        reminders.cancel()
        cleanup.cancel()
        engine_idle.cancel()
        if mcp_connect is not None:
            mcp_connect.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await mcp_connect
        await asyncio.to_thread(mcp.close)
        for turn in list(app.state.turns):
            turn.cancel()
        for task in [reminders, cleanup, engine_idle, *app.state.turns]:
            with suppress(asyncio.CancelledError):
                await task
        await engine.stop()
        await app.state.model_health.close()
        store.drop_ephemeral()

def _build_transcriber(config) -> "voice.Transcriber | None":
    """Build the transcriber, tolerating a missing backend.

    ``[voice] stt = "browser"`` means the client transcribes, so the server
    holds no transcriber. ``local`` builds a whisper.cpp backend; if the
    binary is missing we return ``None`` so the server still starts and the
    route reports that transcription is off.
    """
    try:
        return build_transcriber(config)
    except TranscriptionError:
        return None


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
    from kova.attachments import AttachmentBodyLimit
    app.add_middleware(AttachmentBodyLimit)
    app.add_middleware(egress.PolicyMiddleware, config=config)
    from fastapi.responses import JSONResponse

    @app.exception_handler(egress.LocalOnlyError)
    async def local_only_error(request, exc):
        return JSONResponse(status_code=409, content={'detail': str(exc)})

    app.state.config = config
    app.state.transport = transport
    app.state.now = now
    app.state.model_health = _ModelHealth(config, transport, now)
    app.state.token = ensure_token(config.home)
    app.state.turns = set()
    app.state.chat_locks = {}
    app.state.chat_turns = {}
    app.state.steering = {}
    app.state.transcriber = _build_transcriber(config)
    install_web(app, _require_bearer)
    pairing.install_phone(app, _require_token)
    from kova.provider_routes import install_provider_routes
    config.bind_providers()
    install_provider_routes(app, config, _require_token)
    from kova.mcp_routes import install_mcp_routes
    install_mcp_routes(app, config, _require_token)
    from kova.local_model_routes import install_local_model_routes
    install_local_model_routes(app, config, _require_token)
    from kova.extension_routes import install_extension_routes
    install_extension_routes(app, config, _require_token)

    def chat_lock(chat_id: str) -> asyncio.Lock:
        """One turn at a time per chat; deleting a chat waits for its turn."""
        return app.state.chat_locks.setdefault(chat_id, asyncio.Lock())

    @app.get("/health")
    async def health(probe: bool | None = Query(None)) -> dict:
        status = await app.state.model_health.get(force=probe is True, wait=probe is not False)
        if config.privacy_local_only:
            with suppress(KeyError):
                if not egress.is_loopback_url(config.active_model().base_url):
                    status = {**status, 'model_ok': False}
        return {"version": __version__, "model": config.active, "local_only": config.privacy_local_only, **status}

    api = APIRouter(dependencies=[Depends(_require_token)])
    # A paired phone needs these on top of chat (pairing.CAPABILITIES).
    settings_write = [Depends(pairing.require_capability("settings:write"))]
    providers_write = [Depends(pairing.require_capability("providers:write"))]
    host_exec = [Depends(pairing.require_capability("host:exec"))]
    memory_write = [Depends(pairing.require_capability("memory:write"))]

    def wrote_in(chat_id: str, request: Request) -> frozenset[str] | None:
        """Record that the caller put text or files into a chat; return its limits.

        A chat a phone wrote in keeps that phone's limits for every later turn,
        whoever sends it, because the model reads the whole history.
        """
        allowed = pairing.request_capabilities(request)
        app.state.store.limit_chat(chat_id, allowed)
        return allowed

    def limit_chat_settings(request: Request, permission: str | None, cwd: str | None) -> None:
        """A phone without host:exec cannot pick Full access or a folder outside Kova Files."""
        allowed = pairing.request_capabilities(request)
        if allowed is None or "host:exec" in allowed:
            return
        if permission == "full" or (cwd is not None and not pairing.folder_is_shared(
                cwd, config.files_path, config.home, config.memory_path)):
            raise HTTPException(403, "This phone is not allowed to choose Full access or a working "
                                     "folder outside Kova Files. Do this on your computer.")

    @api.get('/privacy')
    async def privacy_report(days: int = Query(30, ge=1, le=365), chat_id: str | None = None) -> dict:
        report = app.state.store.egress_report(days, chat_id)
        return {**report, 'local_only': config.privacy_local_only}

    privacy_lock = asyncio.Lock()

    @api.patch('/privacy', dependencies=settings_write)
    async def privacy_update(body: PrivacyBody) -> dict:
        async with privacy_lock:
            previous = config.privacy_local_only
            config.privacy_local_only = body.local_only
            from filelock import FileLock, Timeout
            def persist():
                # A refresh started before this toggle may still be saving its
                # old config in a worker thread. Serialize so the policy wins
                # without discarding newly rotated credentials.
                with FileLock(str(config.home / '.provider-auth.lock'), timeout=30):
                    save_config(config)
            try:
                await asyncio.to_thread(persist)
            except (OSError, Timeout):
                config.privacy_local_only = previous
                raise HTTPException(503, 'Could not save Privacy settings. Please retry.') from None
            if body.local_only:
                # Stop in-flight turns and external workers; no new request can pass
                # the shared dynamic policy while cancellation unwinds.
                turns = list(app.state.turns)
                for turn in turns:
                    turn.cancel()
                await asyncio.gather(*turns, return_exceptions=True)
                await app.state.scheduler.shutdown()
                await asyncio.to_thread(app.state.mcp.close)
                from kova.mcp import McpManager
                app.state.mcp = McpManager(config.home)
                app.state.tool_context.mcp = app.state.mcp
                app.state.agent.tools = build_tools(app.state.tool_context)
            return {'local_only': config.privacy_local_only}

    def autostart_result(action: str) -> dict:
        from kova import autostart
        try:
            if action == "on":
                autostart.enable()
            elif action == "off":
                autostart.disable()
            result = autostart.status()
            return {"available": result.available, "enabled": result.enabled,
                    "stale": result.stale}
        except (autostart.AutostartError, OSError):
            raise HTTPException(503, "Start at login is unavailable. Check your system startup settings.") from None

    @api.get("/autostart")
    def autostart_status() -> dict:
        return autostart_result("status")

    @api.post("/autostart/{action}", dependencies=settings_write)
    def autostart_update(action: str) -> dict:
        if action not in {"on", "off"}:
            raise HTTPException(400, "Choose on or off.")
        return autostart_result(action)

    @api.get("/usage")
    async def usage(days: int = Query(30, ge=1, le=366)) -> dict:
        """Tokens, estimated spend and speed: today, this month and the last ``days`` days."""
        from kova.usage import summarize, window_start
        rows = await asyncio.to_thread(app.state.store.usage_rows, window_start(days))
        return summarize(rows, days=days, budget_usd=config.usage_monthly_budget_usd)

    @api.get("/persona")
    async def persona() -> dict:
        current = load_config(config.home)
        return {"name": current.persona_name, "tone": current.persona_tone,
                "configured": current.persona_configured}

    @api.post("/persona", dependencies=settings_write)
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
        return [{**chat, "state": app.state.conversation_states.get(chat["id"])} for chat in app.state.store.list_chats(include_ephemeral=True)]

    @api.post("/chats")
    async def create_chat(request: Request, body: ChatBody | None = None) -> dict:
        mode = body.mode if body else None
        if mode is not None and mode not in MODES:
            raise HTTPException(status_code=422, detail=f"unknown mode '{mode}'")
        cwd = working_folder(body.cwd) if body and body.cwd is not None else None
        limit_chat_settings(request, body.permission if body else None, cwd)
        chat = app.state.store.create_chat(
            body.title if body else None,
            ephemeral=bool(body and body.ephemeral),
            mode=mode or DEFAULT_MODE,
            web=body.web if body is not None and body.web is not None else True,
            permission=body.permission if body and body.permission else "manual",
            cwd=cwd if cwd is not None else str(config.files_path.resolve()),
        )
        # A phone chose its title and settings: the chat keeps that phone's limits.
        wrote_in(chat["id"], request)
        return chat

    @api.get("/chats/search")
    async def search_chats(q: str = "", limit: int = Query(20, ge=1, le=100)) -> list[dict]:
        """Chats with a user/assistant message matching ``q``, newest first."""
        chats = await asyncio.to_thread(app.state.store.search_chats, q, limit)
        return [{**chat, "state": app.state.conversation_states.get(chat["id"])} for chat in chats]

    @api.post("/chats/{chat_id}/fork")
    async def fork_chat(chat_id: str) -> dict:
        if app.state.store.is_ephemeral(chat_id):
            raise HTTPException(409, "Incognito chats cannot be forked or exported.")
        async with chat_lock(chat_id):
            chat = app.state.store.fork_chat(chat_id)
            if chat is None:
                raise HTTPException(status_code=404, detail="chat not found")
            return chat

    @api.get("/chats/{chat_id}")
    async def get_chat(chat_id: str) -> dict:
        chat = app.state.store.get_chat(chat_id, include_usage=True)
        if chat is None:
            raise HTTPException(status_code=404, detail="chat not found")
        return {**chat, "state": app.state.conversation_states.get(chat_id)}

    @api.patch("/chats/{chat_id}")
    async def update_chat(chat_id: str, body: ChatBody, request: Request) -> dict:
        if body.ephemeral is not None:
            raise HTTPException(409, "Incognito is fixed when a chat starts. Create a new chat instead.")
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
        limit_chat_settings(request, body.permission, changes.get("cwd"))
        if app.state.store.get_chat(chat_id) is None:
            raise HTTPException(status_code=404, detail="chat not found")
        if any(not task.done() for task in app.state.chat_turns.get(chat_id, ())):
            raise HTTPException(status_code=409, detail="Stop the active turn before changing chat settings")
        # A phone's title, mode or permission is the phone writing in the chat, so
        # it keeps that phone's limits; no setting a phone picks can widen a turn.
        wrote_in(chat_id, request)
        app.state.store.update_chat(chat_id, **changes)
        if body.permission is not None or body.cwd is not None:
            app.state.agent.approvals.sessions.discard(chat_id)
        chat = app.state.store.get_chat(chat_id)
        return {key: chat[key] for key in ("id", "title", "mode", "web", "permission", "cwd")}

    @api.post("/chats/{chat_id}/attachments")
    async def upload_attachment(chat_id: str, request: Request, file: UploadFile = File(...)) -> dict:
        from kova import attachments
        async with chat_lock(chat_id):
            if app.state.store.get_chat(chat_id) is None:
                raise HTTPException(404, "Chat not found.")
            wrote_in(chat_id, request)
            try:
                data = await file.read(attachments.MAX_BYTES + 1)
                await asyncio.to_thread(attachments.reap, app.state.store,
                                        now=app.state.now, chat_id=chat_id)
                return await asyncio.to_thread(attachments.save, config.home, app.state.store,
                                               chat_id, file.filename or 'file', data,
                                               now=app.state.now)
            except attachments.AttachmentQuotaError as exc:
                raise HTTPException(409, str(exc)) from None
            except attachments.AttachmentError as exc:
                raise HTTPException(400, str(exc)) from None
            finally:
                await file.close()

    @api.post("/transcribe")
    async def transcribe(audio: UploadFile = File(...)) -> dict:
        from kova import voice
        if app.state.transcriber is None:
            raise HTTPException(409, "Voice transcription is off. Set [voice] stt = \"local\" to turn it on.")
        try:
            raw = await audio.read(MAX_AUDIO_BYTES + 1)
            if len(raw) > MAX_AUDIO_BYTES:
                raise HTTPException(413, "The audio is too large.")
            fmt = (audio.filename or "").rsplit(".", 1)[-1].lower()
            result = await asyncio.to_thread(app.state.transcriber.transcribe, raw, fmt=fmt)
        except voice.TranscriptionError as exc:
            raise HTTPException(422, str(exc)) from None
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(500, "Transcription failed.") from exc
        finally:
            await audio.close()
        return result.as_dict()

    @api.delete("/chats/{chat_id}/attachments/{ident}")
    async def remove_attachment(chat_id: str, ident: str) -> dict:
        async with chat_lock(chat_id):
            if not app.state.store.delete_attachment(chat_id, ident):
                raise HTTPException(404, "Attachment not found.")
        return {"ok": True}

    @api.delete("/chats/{chat_id}")
    async def delete_chat(chat_id: str) -> dict:
        if app.state.store.is_ephemeral(chat_id):
            # Closing a private chat cancels its work instead of retaining its
            # content until a disconnected provider eventually finishes.
            for task in list(app.state.chat_turns.get(chat_id, ())):
                task.cancel()
        async with chat_lock(chat_id):
            deleted = app.state.store.delete_chat(chat_id)
        app.state.chat_locks.pop(chat_id, None)
        # A second window or a restarted server may already have discarded
        # this RAM-only chat. Let clients leave it without getting stuck.
        if not deleted and not app.state.store.is_ephemeral(chat_id):
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

    @api.put("/permission-rules", dependencies=settings_write)
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

    @api.get("/chats/{chat_id}/approvals")
    async def pending_approvals(chat_id: str) -> list[dict]:
        """Approvals and questions still waiting for an answer in this chat.

        The card exists only in the turn's SSE stream; a reloaded page or a
        second window rebuilds it from here and answers through the POST below.
        """
        if app.state.store.get_chat(chat_id) is None:
            raise HTTPException(status_code=404, detail="chat not found")
        return app.state.agent.approvals.pending_for(chat_id)

    @api.post("/chats/{chat_id}/approvals/{approval_id}")
    async def answer_approval(chat_id: str, approval_id: str, body: ApprovalBody,
                              request: Request) -> dict:
        chat = app.state.store.get_chat(chat_id)
        if chat is None:
            raise HTTPException(status_code=404, detail="chat not found")
        allowed = pairing.request_capabilities(request)
        waiting = app.state.agent.approvals.pending.get(approval_id)
        if allowed is not None and waiting is not None and waiting.chat_id == chat_id:
            # A phone may always refuse without a message. It may allow only what it
            # could have run itself, and put words (an answer, a reason) only into a
            # turn that can do no more than it may.
            if body.decision != "deny" and not waiting.needs <= allowed:
                missing = ", ".join(sorted(waiting.needs - allowed))
                raise HTTPException(403, f"This phone is not allowed to approve this ({missing}). "
                                         "Answer it on your computer.")
            if body.decision in {"allow_session", "allow_always"} and not (
                    "settings:write" in allowed and pairing.within(waiting.authority, allowed)):
                raise HTTPException(403, "This phone can allow one action at a time. "
                                         "Grant more on your computer.")
            words = (body.message or "").strip() or (waiting.tool == "ask_user" and body.decision != "deny")
            if words and not pairing.within(waiting.authority, allowed):
                raise HTTPException(403, "This reply can do more than this phone may, so it cannot take "
                                         "an answer or reason from it. Answer on your computer, or "
                                         "decline without a message.")
            if words:
                app.state.store.limit_chat(chat_id, allowed)
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
            try:
                entry = config.active_model()
            except KeyError as exc:
                raise HTTPException(status_code=422,
                                    detail="No model is set up yet. Run: kova setup") from exc
            if not entry.model:
                raise HTTPException(status_code=422, detail="No model is set up yet. Run: kova setup")
            event = await compact(app.state.store, chat_id,
                                  ModelClient(entry, transport=app.state.agent.transport,
                                              privacy=config.privacy_secrets, config=config),
                                  context_tokens=entry.context, threshold=config.compact_threshold, force=True)
            return event or {"type": "compact_skipped", "message": "Nothing to compact, or the summary failed."}

    @api.post("/chats/{chat_id}/messages")
    async def post_message(chat_id: str, body: TurnBody, request: Request) -> StreamingResponse:
        if app.state.store.get_chat(chat_id) is None:
            raise HTTPException(status_code=404, detail="chat not found")
        queued = None
        if body.queued is not None:
            queued = app.state.store.get_queued_message(chat_id, body.queued)
            if queued is None:
                raise HTTPException(404, "That queued message was already sent or removed.")
            if body.text != queued["text"] or (queued["remote"] and body.attachments):
                raise HTTPException(409, "Send a queued message as it was queued.")
        if body.model is not None and not any(m.name == body.model for m in config.models):
            raise HTTPException(status_code=404, detail=f"unknown model '{body.model}'")
        rows = {r['id']: r for r in app.state.store.attachments(chat_id)}
        if len(set(body.attachments)) != len(body.attachments) or any(i not in rows for i in body.attachments):
            raise HTTPException(400, "Choose attachments from this chat only.")
        config.bind_providers()
        entry = next((m for m in config.models if m.name == (body.model or config.active)), None)
        from kova import providers
        if entry is not None:
            egress.check_url(entry.base_url, config)
        if any(rows[i]['mime'].startswith('image/') for i in body.attachments):
            if entry is None or not entry.vision or not providers.supports(entry._provider, 'vision'):
                raise HTTPException(400, "This model cannot read images; turn on Vision for it in Model settings, or attach OCR text.")
        # Validation is over: only now take the queued row (once) and record who wrote.
        if queued is not None:
            if not app.state.store.take_queued_message(chat_id, queued["id"]):
                raise HTTPException(404, "That queued message was already sent or removed.")
            app.state.store.limit_chat(chat_id, queued.get("capabilities"))
        wrote_in(chat_id, request)
        # The caller's limits, the chat's (anyone limited who wrote in it, including
        # whoever queued this message) and nothing else.
        capabilities = pairing.narrow(pairing.request_capabilities(request),
                                      app.state.store.chat_capabilities(chat_id))
        queue: asyncio.Queue[dict | None] = asyncio.Queue()

        async def run_turn() -> None:
            """Drive the turn to completion even if the client disconnects."""
            steering = Steering(capabilities)
            records = []
            with egress.scope(config=config, store=app.state.store, chat_id=chat_id, items=records):
                try:
                    async with chat_lock(chat_id):
                        if app.state.store.get_chat(chat_id) is None:
                            queue.put_nowait({"type": "error", "message": "chat was deleted"})
                            return
                        app.state.conversation_states.transition(chat_id, "turn_start")
                        app.state.steering[chat_id] = steering
                        try:
                            async for event in app.state.agent.run_turn(chat_id, body.text, model=body.model,
                                                                      steering=steering, attachment_ids=body.attachments,
                                                                      capabilities=capabilities):
                                app.state.conversation_states.transition(chat_id, event.get("type", ""))
                                if event.get('type') in {'done', 'error'} and records:
                                    queue.put_nowait({'type': 'egress', 'items': list(records)})
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
                        app.state.store.queue_message(chat_id, message, capabilities=steering.capabilities)
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
    async def queue_message(chat_id: str, body: QueuedBody, request: Request) -> dict:
        """Queue a message; it survives restarts until a client runs it."""
        if app.state.store.get_chat(chat_id) is None:
            raise HTTPException(status_code=404, detail="chat not found")
        if body.model is not None and not any(m.name == body.model for m in config.models):
            raise HTTPException(status_code=404, detail=f"unknown model '{body.model}'")
        row = app.state.store.queue_message(chat_id, body.text, body.model,
                                            capabilities=wrote_in(chat_id, request))
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
        """Take the oldest queued message, for a client to send as its next turn.

        A message a phone queued comes back with ``remote: true`` and stays
        queued: the client sends it as-is with ``queued: <id>`` on
        ``POST /chats/{id}/messages``, which takes it and runs it under that
        phone's limits. Clients never treat its text as their own input.
        """
        if app.state.store.get_chat(chat_id) is None:
            raise HTTPException(status_code=404, detail="chat not found")
        return app.state.store.pop_queued_message(chat_id) or {"message": None}

    @api.post("/chats/{chat_id}/steer")
    async def steer(chat_id: str, body: QueuedBody, request: Request) -> dict:
        """Put a message into the turn that is running now, not the queue."""
        if app.state.store.get_chat(chat_id) is None:
            raise HTTPException(status_code=404, detail="chat not found")
        steering = app.state.steering.get(chat_id)
        if steering is None:
            raise HTTPException(status_code=409,
                                detail="No reply is running. Queue the message or send it as a new one.")
        allowed = pairing.request_capabilities(request)
        if allowed is not None and (steering.capabilities is None or not steering.capabilities <= allowed):
            raise HTTPException(403, "This reply can do more than this phone may. "
                                     "Queue the message instead; it runs after the reply.")
        wrote_in(chat_id, request)
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
    async def restore_checkpoint(chat_id: str, checkpoint_id: int, request: Request) -> dict:
        """Put the working folder back to this checkpoint.

        Only the paths the reverted turns changed are touched; the user's git
        repository is never read or written.
        """
        row = checkpoint_or_404(chat_id, checkpoint_id)
        allowed = pairing.request_capabilities(request)
        if allowed is not None and "host:exec" not in allowed and \
                not pairing.folder_is_shared(row["root"], config.files_path, config.home,
                                             config.memory_path):
            raise HTTPException(403, "This phone may restore files only inside Kova Files. "
                                     "Restore this on your computer.")
        if any(not task.done() for task in app.state.chat_turns.get(chat_id, ())):
            raise HTTPException(status_code=409, detail="Stop the active turn before restoring files")
        rows = await asyncio.to_thread(app.state.store.checkpoints_from, chat_id, checkpoint_id)
        plan = checkpoints.revert_plan(rows, checkpoint_id)
        if allowed is not None and "host:exec" not in allowed and \
                any(pairing.protected_path(path) for path, _ in plan):
            raise HTTPException(403, "This phone may not restore KOVA.md, AGENTS.md or git data. "
                                     "Restore this on your computer.")
        result = await asyncio.to_thread(checkpoints.apply_restore, Path(row["root"]), plan,
                                         app.state.agent.blobs)
        return {"ok": not result["failed"], "checkpoint_id": checkpoint_id, "root": row["root"], **result}


    @api.post("/memory/location", dependencies=settings_write)
    async def memory_use(body: MemoryLocationBody) -> dict:
        memory = Memory(memory_folder(body.path), "Kova" if body.obsidian else None, config.memory_budget)
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

    @api.post("/memory", dependencies=memory_write)
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

    @api.patch("/memory/settings", dependencies=settings_write)
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

    @api.delete("/memory/{slug}", dependencies=memory_write)
    async def memory_delete(slug: str) -> dict:
        if not app.state.memory.delete(slug):
            raise HTTPException(status_code=404, detail="note not found")
        return {"ok": True}

    @api.patch("/memory/{slug}", dependencies=memory_write)
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

    @api.post("/memory/undo", dependencies=memory_write)
    async def memory_undo(body: MemoryUndoBody | None = None) -> dict:
        """Reverse recorded review/consolidation writes from their preimages."""
        from kova.review import undo
        undone = await asyncio.to_thread(undo, app.state.memory, app.state.store,
                                         revision_id=body.revision_id if body else None)
        return {"undone": undone}

    @api.post("/memory/consolidate", dependencies=memory_write)
    async def memory_consolidate(body: ConsolidateBody | None = None) -> dict:
        """Plan one consolidation run and, unless asked to dry-run, apply it."""
        from kova.review import gate_operations, plan_consolidation, apply_operations
        try:
            entry = config.active_model()
        except KeyError as exc:
            raise HTTPException(status_code=422,
                                detail="No model is set up yet. Run: kova setup") from exc
        if not entry.model:
            raise HTTPException(status_code=422, detail="No model is set up yet. Run: kova setup")
        client = ModelClient(entry, transport=app.state.agent.transport,
                             privacy=config.privacy_secrets, config=config)
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

    @api.post("/jobs", dependencies=host_exec)
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

    @api.delete("/jobs/{jid}", dependencies=host_exec)
    async def job_delete(jid: int) -> dict:
        if not app.state.store.delete_job(jid):
            raise HTTPException(status_code=404, detail="job not found")
        return {"ok": True}

    @api.get("/models")
    async def models_list() -> list[dict]:
        config.bind_providers()
        return [_model_dict(entry) for entry in config.models]

    @api.post("/models", dependencies=providers_write)
    async def model_add(body: ModelBody) -> dict:
        entry = ModelEntry(name=body.name, base_url=body.base_url, model=body.model,
                           api_key=body.api_key, context=body.context, vision=body.vision)
        config.models[:] = [m for m in config.models if m.name != entry.name]
        config.models.append(entry)
        save_config(config)
        return _model_dict(entry)

    @api.patch("/models", dependencies=providers_write)
    async def model_update(body: ModelPatch) -> dict:
        """Change one model's per-model settings, such as image support."""
        entry = next((m for m in config.models if m.name == body.name), None)
        if entry is None:
            raise HTTPException(status_code=404, detail=f"unknown model '{body.name}'")
        if body.vision is None:
            raise HTTPException(status_code=422, detail="nothing to update")
        entry.vision = body.vision
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
        keepalive = app.state.events_keepalive

        async def stream() -> AsyncIterator[str]:
            try:
                # A ping right away and then every ``keepalive`` seconds lets a
                # browser tell a quiet stream from a dead one (a sleeping phone,
                # a changed Wi-Fi, a server that vanished without closing).
                yield _sse({"type": "ping"})
                while True:
                    try:
                        event = await asyncio.wait_for(queue.get(), keepalive)
                    except TimeoutError:
                        yield _sse({"type": "ping"})
                        continue
                    if event is None:  # server shutting down
                        return
                    yield _sse(event)
            finally:
                app.state.bus.unsubscribe(queue)

        return StreamingResponse(stream(), media_type="text/event-stream")

    app.include_router(api)
    return app
