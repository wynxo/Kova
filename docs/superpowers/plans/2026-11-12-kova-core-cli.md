# Kova core + CLI Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A Python server (`kova serve`) plus a CLI (`kova`) that chats with any OpenAI-compatible model, with Markdown memory, web/file/reminder tools, and reminders that fire while the CLI is closed.

**Architecture:** One FastAPI server on `127.0.0.1:7700` owns all state (SQLite + a memory folder). An agent loop streams the model and runs tools. The CLI is a thin HTTP client that starts the server when needed.

**Tech Stack:** Python 3.12+, uv, FastAPI, uvicorn, httpx, Typer, Rich, pydantic, PyYAML, tomli-w, ddgs, pytest, pytest-asyncio.

**Spec:** `docs/superpowers/specs/2026-11-12-kova-core-cli-design.md`

## Global Constraints

- Python `>=3.12`; package `kova` in `src/kova/`; tests in `tests/`; run everything through `uv run`.
- Data home: `KOVA_HOME` env var, else `~/.kova/`. Files there: `config.toml`, `kova.db`, `memory/`, `token`, `server.pid`, `server.log`.
- Server binds `127.0.0.1`, default port `7700`. Every route except `GET /health` requires `Authorization: Bearer <token>`; `token` file mode `600`.
- Default model on first run: name `tiel`, base URL `http://192.168.178.29:8080/v1`, model `cyber-tiel-coder-35b-a3b-mtp-ud-iq3_xxs`, context `131072`.
- Default files folder: `~/Kova Files`. Default memory folder: `<home>/memory`.
- Max 10 tool steps per turn. Memory: up to 5 notes injected. History trimmed at 4 chars/token, keeping 25% of the context free.
- Tool limits: web_search 5 results, 15 s timeout; web_fetch 1 MB download cap, 8,000 chars returned; read_file 100 KB cap.
- Reminder loop interval 15 s. Desktop notification via `notify-send` only when it exists.
- Kova never names or compares itself to other AI products in user-facing text. Its identity is "Kova".
- No secrets in the repo. Tests never need a GPU or network (except the `live` test, skipped unless `KOVA_LIVE=1`).
- Every new AI tool used while building gets a line in `AI-TOOLS.md`.

## Review Focus

1. A `<think>` tag split across two stream chunks (`"<thi"` + `"nk>"`) must not leak into the visible text. → Task 4 test `test_think_tag_split_across_chunks`.
2. Model server down in the middle of a chat: user message stays saved, client gets an `error` event, the server keeps serving. → Task 6 `test_model_down_yields_error_and_keeps_user_message`, Task 8 `test_health_ok_when_model_down`.
3. File tool paths that escape the folder (`../x`, absolute `/etc/passwd`, a symlink pointing outside) are refused. → Task 5 `test_files_refuse_escape`.
4. Reminder times without a timezone are local time; past times are refused. → Task 5 `test_reminder_add_naive_is_local_and_past_refused`.
5. Obsidian mode never writes or deletes outside `<vault>/Kova/`, even when a user note has the same title. → Task 3 `test_obsidian_mode_writes_only_in_kova_dir`.

## File map

```
pyproject.toml                 uv project, deps, entry point kova = "kova.cli:app"
src/kova/__init__.py           __version__ = "0.1.0"
src/kova/__main__.py           python -m kova → cli.app()
src/kova/config.py             Config, ModelEntry, load/save, token
src/kova/store.py              SQLite chats/messages/reminders
src/kova/memory.py             Markdown notes
src/kova/models.py             streaming OpenAI-compatible client
src/kova/tools/__init__.py     Tool, ToolContext, ToolError, build_tools, openai_schemas
src/kova/tools/files.py        list_files, read_file, write_file
src/kova/tools/web.py          web_search, web_fetch
src/kova/tools/reminders.py    reminder_add, reminder_list
src/kova/tools/memory.py       memory_save, memory_search
src/kova/agent.py              one chat turn
src/kova/events.py             EventBus
src/kova/reminders.py          ReminderLoop, desktop_notify
src/kova/server.py             FastAPI app
src/kova/client.py             HTTP client + ensure_server
src/kova/cli.py                Typer CLI
tests/fake_model.py            scripted OpenAI-style SSE via httpx.MockTransport
tests/test_*.py                one file per module
README.md                      install + usage
```

## Parallel waves (for running helpers side by side)
- Wave 1: Task 1.
- Wave 2 (parallel): Tasks 2, 3, 4.
- Wave 3 (parallel): Tasks 5, 7.
- Wave 4: Task 6. Wave 5: Task 8. Wave 6: Task 9. Wave 7: Task 10.

---

### Task 1: Project scaffold + config

**Files:**
- Create: `pyproject.toml`, `src/kova/__init__.py`, `src/kova/__main__.py`, `src/kova/config.py`, `tests/test_config.py`, `.gitignore` (`.venv/`, `__pycache__/`, `.pytest_cache/`)

**Interfaces:**
- Produces:
  - `@dataclass ModelEntry(name: str, base_url: str, model: str, api_key: str | None = None, context: int = 131072)`
  - `@dataclass Config(home: Path, models: list[ModelEntry], active: str, memory_path: Path, files_path: Path, port: int = 7700, memory_write_subdir: str | None = None)`; method `active_model() -> ModelEntry` (raises `KeyError` if missing).
  - `kova_home() -> Path`
  - `load_config(home: Path | None = None) -> Config` (creates the home dir and a default `config.toml` if missing)
  - `save_config(cfg: Config) -> None`
  - `ensure_token(home: Path) -> str` (creates a `secrets.token_urlsafe(32)` file with mode 600 once, then returns the same value)

- [ ] **Step 1: `uv init --package --name kova`, set `requires-python = ">=3.12"`, add deps** (`fastapi uvicorn httpx typer rich pydantic pyyaml tomli-w ddgs`; dev: `pytest pytest-asyncio`), set `[tool.pytest.ini_options] asyncio_mode = "auto"` and `markers = ["live: needs a real model"]`, entry point `kova = "kova.cli:app"`. Make `cli.py` a stub `app = typer.Typer()` so the entry point imports.

- [ ] **Step 2: Write failing tests in `tests/test_config.py`**

```python
def test_default_config_created(tmp_path):
    cfg = load_config(tmp_path)
    assert (tmp_path / "config.toml").exists()
    m = cfg.active_model()
    assert (m.name, m.base_url, m.model, m.context) == (
        "tiel", "http://192.168.178.29:8080/v1",
        "cyber-tiel-coder-35b-a3b-mtp-ud-iq3_xxs", 131072)
    assert cfg.port == 7700
    assert cfg.memory_path == tmp_path / "memory"

def test_roundtrip(tmp_path):
    cfg = load_config(tmp_path)
    cfg.models.append(ModelEntry("ollama", "http://localhost:11434/v1", "qwen3"))
    cfg.active = "ollama"
    save_config(cfg)
    assert load_config(tmp_path).active_model().model == "qwen3"

def test_kova_home_env(monkeypatch, tmp_path):
    monkeypatch.setenv("KOVA_HOME", str(tmp_path))
    assert kova_home() == tmp_path

def test_token_stable_and_private(tmp_path):
    t = ensure_token(tmp_path)
    assert ensure_token(tmp_path) == t and len(t) >= 32
    assert (tmp_path / "token").stat().st_mode & 0o777 == 0o600
```

- [ ] **Step 3: Run** `uv run pytest tests/test_config.py -v` → FAIL (import error).
- [ ] **Step 4: Implement `config.py`.** Read TOML with `tomllib`, write with `tomli_w`; paths stored as strings; `~` expanded on load.
- [ ] **Step 5: Run** `uv run pytest -v` → all PASS.
- [ ] **Step 6: Commit** `feat: project scaffold and config`.

### Task 2: SQLite store

**Files:** Create `src/kova/store.py`, `tests/test_store.py`

**Interfaces:**
- Produces `class Store(path: Path)` (creates tables on open; `check_same_thread=False`, one `threading.Lock` around writes):
  - `create_chat(title: str | None = None) -> dict` → `{"id", "title", "created"}`; id = `uuid4().hex[:12]`
  - `list_chats() -> list[dict]` newest first
  - `get_chat(chat_id: str) -> dict | None` → chat dict plus `"messages"`
  - `set_title(chat_id: str, title: str) -> None`
  - `delete_chat(chat_id: str) -> bool` (deletes its messages too)
  - `add_message(chat_id: str, role: str, content: str | None, tool_calls: list[dict] | None = None, tool_call_id: str | None = None) -> int`
  - `get_messages(chat_id: str) -> list[dict]` in OpenAI format: keys `role`, `content`, plus `tool_calls` / `tool_call_id` only when set
  - `add_reminder(when: datetime, text: str) -> int` (`when` must be tz-aware; stored ISO 8601)
  - `list_reminders(include_done: bool = False) -> list[dict]` → `{"id", "when" (ISO str), "text", "done"}` sorted by time
  - `due_reminders(now: datetime) -> list[dict]` (not done, `when <= now`)
  - `mark_reminder_done(rid: int) -> None`, `delete_reminder(rid: int) -> bool`

- [ ] **Step 1: Tests:** `test_chat_roundtrip` (create, add user + assistant-with-tool_calls + tool message, `get_messages` equals the exact OpenAI dicts, no `tool_calls` key on plain messages); `test_list_newest_first`; `test_delete_chat_removes_messages`; `test_reminders_due_and_done` (two reminders at now-1min and now+1h → `due_reminders(now)` returns only the first; after `mark_reminder_done` it's empty and `list_reminders()` excludes it); `test_add_reminder_requires_tz` (naive datetime → `ValueError`).
- [ ] **Step 2: Run** → FAIL. **Step 3: Implement.** **Step 4: Run** → PASS. **Step 5: Commit** `feat: sqlite store`.

### Task 3: Markdown memory

**Files:** Create `src/kova/memory.py`, `tests/test_memory.py`

**Interfaces:**
- Produces:
  - `slugify(title: str) -> str` (lowercase, non-alphanumerics → `-`, trimmed, max 60 chars, `"note"` if empty)
  - `@dataclass Note(slug: str, title: str, body: str, tags: list[str], updated: str, path: Path)`
  - `class Memory(root: Path, write_subdir: str | None = None)`; write dir = `root / write_subdir` if set, else `root`.
    - `save(title: str, body: str, tags: list[str] | None = None) -> Note` (writes `<write dir>/<slug>.md` with YAML frontmatter `title`, `tags`, `updated` ISO; same slug overwrites)
    - `get(slug: str) -> Note | None` (write dir first, then the rest of root)
    - `delete(slug: str) -> bool` (only inside write dir)
    - `list() -> list[Note]` (all `*.md` under root, recursive, skipping any `.obsidian`/hidden dir; files without frontmatter use the file stem as title)
    - `search(query: str, limit: int = 5) -> list[Note]`

- [ ] **Step 1: Tests:** `test_save_get_roundtrip`; `test_slugify` (`"My Dog's Name!" → "my-dog-s-name"`, `"" → "note"`); `test_search_ranks_title_over_body` (scoring: each lowercase query word of length ≥2 counts 3 per hit in title, 2 in tags, 1 in body; zero-score notes excluded; ties → newer `updated` first); `test_plain_markdown_without_frontmatter_is_listed`; `test_hidden_dirs_skipped`; **`test_obsidian_mode_writes_only_in_kova_dir`**: vault has `Dog.md` (user note); `Memory(vault, "Kova").save("Dog", "...")` writes `vault/Kova/dog.md`, `vault/Dog.md` unchanged byte for byte; `delete("dog")` removes only the Kova copy; deleting a slug that only exists outside `Kova/` returns `False` and the file remains.
- [ ] **Step 2: Run** → FAIL. **Step 3: Implement** (PyYAML `safe_load`/`safe_dump`). **Step 4: Run** → PASS. **Step 5: Commit** `feat: markdown memory`.

### Task 4: Streaming model client + fake model

**Files:** Create `src/kova/models.py`, `tests/fake_model.py`, `tests/test_models.py`

**Interfaces:**
- Consumes: `ModelEntry` (Task 1).
- Produces:
  - `class ModelError(Exception)`
  - `@dataclass ModelEvent(type: str, text: str = "", tool_call: dict | None = None)`; `type` ∈ `"text" | "reasoning" | "tool_call" | "done"`; `tool_call` = `{"id": str, "name": str, "arguments": str}`
  - `class ThinkSplitter`: `feed(chunk: str) -> list[tuple[str, str]]` (kind `"text"`/`"reasoning"`), `flush() -> list[tuple[str, str]]`; holds back any trailing partial `<think>`/`</think>` prefix until the next chunk.
  - `class ModelClient(entry: ModelEntry, transport: httpx.AsyncBaseTransport | None = None, timeout: float = 300)`:
    - `async def stream(messages: list[dict], tools: list[dict] | None = None) -> AsyncIterator[ModelEvent]`: POST `{base_url}/chat/completions` with `stream: true`; `delta.reasoning_content` or `delta.reasoning` → `reasoning`; `delta.content` → through `ThinkSplitter`; `delta.tool_calls` accumulated by `index`, emitted as `tool_call` events after the stream ends, then `done`. Missing call ids get `call_<n>`.
    - `async def ping() -> bool` (GET `{base_url}/models`, True on 200, False on any error)
    - Connection error or HTTP ≥400 → `ModelError(f"Can't reach model '{name}' at {base_url}")` (HTTP case adds `: HTTP <code>`).
  - `tests/fake_model.py`: `class FakeModel(scripts: list[list[dict]])` — each script is a list of OpenAI `delta` dicts for one request; `.transport` is an `httpx.MockTransport` that answers request *n* with script *n* as SSE (`data: {"choices":[{"delta": ...}]}` lines, then `data: [DONE]`) and answers `GET /models` with 200; `.requests` holds parsed request bodies. Helpers: `text(s) -> list[dict]` splits a string into 3-char deltas; `tool(name, args: dict, id="call_1") -> list[dict]` emits a tool call split over two deltas.

- [ ] **Step 1: Tests:** `test_streams_text`; `test_reasoning_field`; `test_think_tags_become_reasoning` (`"<think>hmm</think>Hi"` → reasoning `"hmm"`, text `"Hi"`); **`test_think_tag_split_across_chunks`** (deltas `"<thi"`, `"nk>a</th"`, `"ink>b"` → reasoning `"a"`, text `"b"`, no `<` in any text event); `test_tool_call_accumulated` (args split over deltas arrive whole, valid JSON); `test_unreachable_raises_model_error` (transport raises `httpx.ConnectError` → `ModelError` whose message contains `"Can't reach model 'tiel'"`); `test_ping`.
- [ ] **Step 2: Run** → FAIL. **Step 3: Implement.** **Step 4: Run** → PASS. **Step 5: Commit** `feat: streaming model client`.

### Task 5: Tools

**Files:** Create `src/kova/tools/__init__.py`, `files.py`, `web.py`, `reminders.py`, `memory.py`; `tests/test_tools.py`

**Interfaces:**
- Consumes: `Config` (T1), `Store` (T2), `Memory` (T3).
- Produces:
  - `class ToolError(Exception)`
  - `@dataclass Tool(name: str, description: str, parameters: dict, run: Callable[[dict], str | Awaitable[str]])`
  - `@dataclass ToolContext(config: Config, store: Store, memory: Memory, now: Callable[[], datetime])` (`now` returns tz-aware local time)
  - `build_tools(ctx: ToolContext) -> dict[str, Tool]` with exactly: `web_search`, `web_fetch`, `list_files`, `read_file`, `write_file`, `reminder_add`, `reminder_list`, `memory_save`, `memory_search`
  - `openai_schemas(tools: dict[str, Tool]) -> list[dict]` → `[{"type": "function", "function": {"name", "description", "parameters"}}]`
  - `files.resolve_in(root: Path, path: str) -> Path` → resolved path (symlinks followed) or `ToolError("Path is outside the Kova files folder")`; creates root if missing.
  - `web.search(query: str) -> list[dict]` (module-level so tests can monkeypatch; uses `ddgs.DDGS().text(query, max_results=5)`, returns `{"title","url","snippet"}`)
- Tool results are strings: JSON for lists, plain text otherwise. `web_fetch` strips `script`/`style` and tags with stdlib `html.parser`, collapses whitespace, cuts at 8,000 chars, refuses non-`http(s)` URLs. `reminder_add(when, text)`: parse with `datetime.fromisoformat`; naive → `when.replace(tzinfo=ctx.now().tzinfo)`; `<= now` → `ToolError("Reminder time must be in the future")`; returns `"Reminder #<id> set for <when>"`.

- [ ] **Step 1: Tests:** `test_build_tools_names_and_schemas` (9 names; every schema has `"type": "object"`); `test_files_roundtrip` (write then read then list inside `files_path`); **`test_files_refuse_escape`** (`"../x"`, `"/etc/passwd"`, and a symlink inside the folder pointing to `tmp_path/"outside"` all raise `ToolError`); `test_read_file_cap` (200 KB file → result ≤ 100 KB + a `"[truncated]"` marker); `test_web_search_formats` (monkeypatch `web.search`); `test_web_fetch_strips_html` (monkeypatch the httpx call via a `MockTransport` on a module-level `_transport` var; `<script>` content absent; `"file:///etc/passwd"` refused); **`test_reminder_add_naive_is_local_and_past_refused`** (fixed `now` = 2026-11-12 10:00 +01:00; `"2026-11-12T11:00"` stored as `2026-11-12T11:00:00+01:00`; `"2026-11-12T09:00"` → `ToolError`); `test_memory_tools`.
- [ ] **Step 2: Run** → FAIL. **Step 3: Implement.** **Step 4: Run** → PASS. **Step 5: Commit** `feat: tools`.

### Task 6: Agent turn

**Files:** Create `src/kova/agent.py`, `tests/test_agent.py`

**Interfaces:**
- Consumes: `Store`, `Memory`, `Tool`/`openai_schemas`/`ToolError`, `ModelClient`/`ModelError`/`ModelEvent`, `Config`.
- Produces:
  - `build_system_prompt(now: datetime, tool_names: list[str], memories: list[Note]) -> str`
  - `trim_history(messages: list[dict], context_tokens: int, reserved_chars: int) -> list[dict]` (drops oldest messages until total content chars ≤ `context_tokens * 4 * 0.75 - reserved_chars`; always keeps the newest user message; never leaves a `tool` message without its assistant call at the start)
  - `class Agent(config: Config, store: Store, memory: Memory, tools: dict[str, Tool], transport: httpx.AsyncBaseTransport | None = None, now: Callable[[], datetime] = <local now>)`
  - `async def run_turn(chat_id: str, text: str) -> AsyncIterator[dict]` yielding: `{"type":"reasoning","text"}`, `{"type":"text","text"}`, `{"type":"tool_call","name","args"}`, `{"type":"tool_result","name","ok": bool,"summary": first 200 chars}`, `{"type":"done","message_id": int}`, `{"type":"error","message"}`
- System prompt starts with this exact copy, then `Current time: <ISO>` and the memory notes as `## <title>\n<body>` under `What you remember about the user:`:

```
You are Kova, a private AI that runs on the user's own computer. Everything stays on their machine.
Be warm, direct and genuinely helpful. Use tools when they help: search the web for current facts, work with files in the Kova files folder, and set reminders.
When you learn a lasting fact about the user (name, preferences, projects, people), save it with memory_save.
Never say you are a different assistant or made by another company. You are Kova.
```

- Flow: save user message (and set chat title to its first 60 chars if the chat has none) → memories = `memory.search(text, 5)` → loop up to 10 steps: stream with tools, forward reasoning/text, collect tool calls; no tool calls → save assistant message, `done`; else save assistant message with `tool_calls`, run each tool (unknown tool → `"Error: unknown tool <name>"`; invalid JSON → `"Error: arguments were not valid JSON"`; `ToolError`/any exception → `"Error: <msg>"`, `ok=False`), save `tool` messages, continue. After 10 steps one final call with `tools=None`. `ModelError` → yield `error` and stop (user message stays saved).

- [ ] **Step 1: Tests** (all with `FakeModel`): `test_plain_answer` (events end with `done`; store has user + assistant); `test_tool_loop` (script 1 = `tool("memory_save", {...})`, script 2 = text → note exists, second request contains the `tool` message); `test_bad_tool_args_reported_to_model`; `test_unknown_tool`; `test_max_steps_then_final_answer` (11 tool-call scripts then text → exactly 11 requests, the last has no `tools` key); **`test_model_down_yields_error_and_keeps_user_message`**; `test_system_prompt_identity` (first request's system message starts with `"You are Kova"` and contains a matching memory note); `test_trim_history_keeps_latest_user` (context 100 tokens, 20 long messages → last user message kept, first kept message isn't `tool`).
- [ ] **Step 2: Run** → FAIL. **Step 3: Implement.** **Step 4: Run** → PASS. **Step 5: Commit** `feat: agent turn loop`.

### Task 7: Events + reminder loop

**Files:** Create `src/kova/events.py`, `src/kova/reminders.py`, `tests/test_reminders.py`

**Interfaces:**
- Consumes: `Store`.
- Produces:
  - `class EventBus`: `subscribe() -> asyncio.Queue[dict]`, `unsubscribe(q) -> None`, `publish(event: dict) -> None` (non-blocking, to every subscriber)
  - `desktop_notify(text: str) -> None` (runs `notify-send Kova <text>` if `shutil.which("notify-send")`, never raises)
  - `class ReminderLoop(store: Store, bus: EventBus, now: Callable[[], datetime], notify: Callable[[str], None] = desktop_notify, interval: float = 15)`: `tick() -> list[dict]` (fires each due reminder once: publish `{"type":"reminder","id","text"}`, call `notify`, mark done); `async def run() -> None` (tick every `interval` until cancelled; exceptions logged, loop continues)

- [ ] **Step 1: Tests:** `test_tick_fires_due_once` (injected clock; due reminder fires on first tick, not on second; future one fires only after the clock moves past it); `test_bus_fanout` (two subscribers both receive); `test_notify_failure_does_not_stop_tick` (notify raises → still marked done and published).
- [ ] **Step 2: Run** → FAIL. **Step 3: Implement.** **Step 4: Run** → PASS. **Step 5: Commit** `feat: reminder loop`.

### Task 8: Server API

**Files:** Create `src/kova/server.py`, `tests/test_server.py`

**Interfaces:**
- Consumes: everything above.
- Produces: `create_app(config: Config, *, transport: httpx.AsyncBaseTransport | None = None, now: Callable[[], datetime] | None = None) -> FastAPI`. The lifespan builds `Store(home/"kova.db")`, `Memory(config.memory_path, config.memory_write_subdir)`, tools, `Agent`, `EventBus`, and starts `ReminderLoop.run()` as a task. Routes exactly as the spec's API section. SSE frames: `event: <type>\ndata: <json>\n\n`. `POST /chats/{id}/messages` runs `run_turn` in a background task that feeds an `asyncio.Queue`; the response streams from the queue, so a disconnect doesn't stop the turn. Unknown chat → 404. `GET /health` → `{"version", "model": active name, "model_ok": bool}`. `POST /models/active` with an unknown name → 404. `POST /models` and `/models/active` save the config.

- [ ] **Step 1: Tests** (FastAPI `TestClient`, `KOVA_HOME=tmp_path`, `FakeModel` transport): `test_auth_required` (no token → 401 everywhere but `/health`); **`test_health_ok_when_model_down`** (200, `model_ok: false`); `test_chat_stream_events` (parse SSE; types end with `done`; `GET /chats/{id}` has both messages); `test_memory_crud`; `test_reminders_crud`; `test_models_switch` (add + activate → `/health` shows it; config file updated); `test_unknown_chat_404`.
- [ ] **Step 2: Run** → FAIL. **Step 3: Implement.** **Step 4: Run** → PASS. **Step 5: Commit** `feat: server api`.

### Task 9: Client + CLI

**Files:** Create `src/kova/client.py`, replace stub `src/kova/cli.py`, `tests/test_cli.py`

**Interfaces:**
- Consumes: server API (Task 8), `load_config`, `ensure_token`.
- Produces:
  - `class KovaClient(base_url: str, token: str, http: httpx.Client | None = None)`: `health() -> dict`, `create_chat() -> dict`, `list_chats() -> list[dict]`, `stream_message(chat_id: str, text: str) -> Iterator[dict]`, `events() -> Iterator[dict]`, `memory_list(q: str | None = None)`, `memory_add(title, body, tags)`, `memory_delete(slug)`, `reminders_list()`, `reminder_add(when, text)`, `reminder_delete(rid)`, `models_list()`, `model_add(name, base_url, model, api_key=None)`, `model_use(name)`
  - `ensure_server(cfg: Config, timeout: float = 10) -> KovaClient`: if `/health` fails, spawn `[sys.executable, "-m", "kova", "serve"]` detached (`start_new_session=True`, output to `server.log`), write `server.pid`, poll `/health` until `timeout`, else raise `RuntimeError(f"Kova server didn't start, see {log}")`.
  - CLI commands exactly as the spec's CLI section. `kova serve` runs uvicorn on `127.0.0.1:<port>`. `kova stop` kills the pid in `server.pid`. `kova chat` streams with Rich: reasoning dim (hidden with `--no-think`), tool calls as `⚙ name` badges, reminder events from a background thread printed as `⏰ <text>`; `/new`, `/model <name>`, `/exit`. `kova memory use <path> [--obsidian]` sets `memory_path` (and `memory_write_subdir="Kova"` with `--obsidian`).
  - A module-level `get_client() -> KovaClient` in `cli.py` (calls `ensure_server`) so tests can monkeypatch it.

- [ ] **Step 1: Tests** (Typer `CliRunner`; `get_client` monkeypatched to a `KovaClient` whose `http` is a `TestClient` of `create_app` with `FakeModel`): `test_ask_prints_answer`; `test_model_list_and_use`; `test_memory_add_list_rm`; `test_reminders_add_list`; `test_ensure_server_timeout_message` (port with nothing listening, `timeout=0.5`, spawn monkeypatched to a no-op → `RuntimeError` mentioning `server.log`).
- [ ] **Step 2: Run** → FAIL. **Step 3: Implement.** **Step 4: Run** → PASS. **Step 5: Commit** `feat: cli`.

### Task 10: Live check + README

**Files:** Create `tests/test_live.py`, `README.md`; modify `AI-TOOLS.md` if new tools were used.

- [ ] **Step 1: `tests/test_live.py`** marked `@pytest.mark.live`, skipped unless `KOVA_LIVE=1`: with the real default config, `create_app` + `TestClient`, ask "What's your name? One word." → text contains `"Kova"`; ask "Remember that my dog is called Rex." then in a **new chat** ask "What's my dog called?" → answer contains `"Rex"`.
- [ ] **Step 2: Run** `KOVA_LIVE=1 uv run pytest -m live -v` against Tiel → PASS (if Tiel says another name, adjust only the system prompt copy and rerun).
- [ ] **Step 3: Manual check:** `uv run kova chat` → ask for a web search, write a file, set a reminder 1 minute ahead, `/exit`, confirm the desktop notification arrives with the CLI closed.
- [ ] **Step 4: README** (what Kova is in one paragraph without naming other AI products, install with `uv tool install .`, `kova model add`, command list).
- [ ] **Step 5: Full suite** `uv run pytest -v` → all PASS. **Commit** `docs: readme and live test`.
