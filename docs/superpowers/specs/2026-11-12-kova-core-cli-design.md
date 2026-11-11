# Kova core + CLI — design

Date: 2026-11-12 · Sub-project 1 of Kova (GenHacks 2026)

## Goal
Kova is a private AI that lives on your own computer. This first piece is the **core server** and the **CLI**. Later pieces (desktop app, web/phone UI, one-click engine install) are separate specs and only talk to the core over its API.

Success for this piece: on the laptop, `kova chat` talks to Cyber Tiel on the homelab, remembers facts about the user across chats, can search the web, read/write files in its own folder, and set reminders that fire even when the CLI is closed.

## Decisions
- **Language:** Python 3.12+, managed with `uv`. Package `kova`, `src/` layout.
- **Shape:** one always-running server (`kova serve`); the CLI is a client of it. The CLI starts the server if it isn't running.
- **Brain:** Kova connects to any OpenAI-compatible server (PolyStrata, Ollama, LM Studio…). Bundling its own engine is a later spec; the model layer is built so it can slot in.
- **Data home:** `~/.kova/` (override with `KOVA_HOME`): `config.toml`, `kova.db` (SQLite), `memory/`, `token`, `server.pid`, `server.log`.
- **Libraries:** FastAPI + uvicorn (server), httpx (model + web calls), Typer + Rich (CLI), pydantic (types), `ddgs` (web search, no API key), stdlib `sqlite3`.

## Components
Each module has one job and is tested on its own.

| Module | Job |
|---|---|
| `config.py` | Load/save `config.toml`: models list, active model, port (7700), memory path, files folder. Creates defaults on first run. |
| `store.py` | SQLite: chats, messages, reminders. Plain functions, no ORM. |
| `models.py` | `ModelClient`: streams `/v1/chat/completions` with tools. Yields events: `text`, `reasoning`, `tool_call`, `done`. Handles both a separate `reasoning_content` field and `<think>…</think>` inside the text. |
| `memory.py` | Markdown notes with YAML frontmatter (`title`, `tags`, `updated`). Save, delete, list, search (keyword score over title + tags + body). |
| `tools/` | One file per tool, each exposing a JSON schema + `run(args) -> str`. Registry in `tools/__init__.py`. |
| `agent.py` | One chat turn: builds the prompt (identity + time + relevant memories + history), streams the model, runs tool calls, loops until a final answer (max 10 tool steps). |
| `reminders.py` | Background loop (every 15 s) that fires due reminders: pushes an event and shows a desktop notification (`notify-send` if present). |
| `server.py` | FastAPI app wiring it all together. |
| `cli.py` | Typer app: `chat`, `ask`, `memory`, `reminders`, `model`, `serve`, `stop`, `status`. |
| `client.py` | Small HTTP client the CLI uses (and that tests reuse); starts the server when needed. |

## API (all JSON, bearer token required)
- `GET /health` → version, active model, model reachable yes/no. (No token needed.)
- `GET /chats`, `POST /chats`, `GET /chats/{id}`, `DELETE /chats/{id}`
- `POST /chats/{id}/messages` `{text}` → **SSE stream** of events: `reasoning`, `text`, `tool_call {name,args}`, `tool_result {name,ok,summary}`, `done {message_id}`, `error {message}`.
- `GET /memory?q=`, `POST /memory` `{title,body,tags}`, `DELETE /memory/{slug}`
- `GET /reminders`, `POST /reminders` `{when,text}`, `DELETE /reminders/{id}`
- `GET /models`, `POST /models` (add), `POST /models/active` `{name}`
- `GET /events` → SSE stream of `reminder {id,text}` events.

Token: random, made on first run, stored in `~/.kova/token` (mode 600). The server binds `127.0.0.1` only. Phone/LAN access is the web spec's job.

## Chat flow
1. CLI sends the user's text. Server saves it.
2. `agent.py` builds messages: system prompt (below) + up to 5 best-matching memory notes + the chat history, trimmed from the oldest so it fits the model's context (estimate 4 chars per token, keep 25% free for the answer).
3. Streams the model. Text and reasoning go straight to the client.
4. If the model calls tools, run each, save the call and result, send the result back, repeat (max 10 steps, then answer with what it has).
5. Save the final answer, send `done`.

**System prompt (identity):** "You are Kova, a private AI that runs on the user's own computer…" plus current date/time and timezone, the tool list, and the rule to save lasting facts about the user with `memory_save`. Kova never claims to be another assistant (Cyber Tiel says it is Claude without this).

## Memory
- Built-in mode: notes live in `~/.kova/memory/`, file name = slug of the title.
- **Obsidian mode:** `memory.path` points at a vault. Kova **reads the whole vault** for search but **only writes inside `<vault>/Kova/`**, so it never edits the user's own notes.
- Model tools: `memory_save(title, body, tags)`, `memory_search(query)`.
- CLI: `kova memory list | search <q> | show <slug> | add | rm <slug> | use <path>` (switch folder).

## Tools (v1)
| Tool | What it does | Limits |
|---|---|---|
| `web_search(query)` | DuckDuckGo via `ddgs`, top 5 results (title, url, snippet) | 15 s timeout |
| `web_fetch(url)` | Download a page, return readable text | 1 MB download cap, 8,000 chars returned |
| `list_files(path)`, `read_file(path)`, `write_file(path, content)` | Work inside the Kova files folder (`~/Kova Files` by default) | Paths outside that folder are refused; read cap 100 KB |
| `reminder_add(when, text)` | `when` is ISO 8601 local time (the model knows the current time from the prompt) | Must be in the future |
| `reminder_list()` | Upcoming reminders | |
| `memory_save`, `memory_search` | See Memory | |

## CLI
- `kova chat [--new | --chat ID]`: interactive chat, streams replies; shows reasoning dimmed (toggle `--no-think`), tool calls as one-line badges; reminder events pop in while chatting. `/new`, `/model`, `/exit` inside chat.
- `kova ask "…"`: one-shot, prints the answer, good for scripts.
- `kova model list | add NAME URL MODEL [--key] | use NAME`
- `kova reminders list | add "<when>" "<text>" | rm ID`
- `kova serve` (foreground), `kova stop`, `kova status`.
- First run makes a default config with one model, `tiel` → `http://192.168.178.29:8080/v1`, `cyber-tiel-coder-35b-a3b-mtp-ud-iq3_xxs`. (The public README says to run `kova model add`; the homelab address is only the dev default.)

## Errors
- Model unreachable or HTTP error → `error` event: "Can't reach model 'tiel' at <url>". Server stays up. `kova status` shows it.
- Tool raises → the error text goes back to the model as the tool result; the turn continues.
- Bad tool arguments (invalid JSON) → same, with a short hint.
- Server not running → CLI starts it in the background, waits up to 10 s for `/health`, else prints the log path.
- Client disconnects mid-stream → the turn finishes and is saved anyway.

## Testing
- `pytest`. A **fake model server** (scripted OpenAI-style streaming replies, including tool calls and `<think>` tags) drives agent and API tests, so tests need no GPU.
- Unit tests per module (config, store, memory, each tool, reminders timing with an injected clock).
- One `@pytest.mark.live` smoke test against the real Tiel, skipped unless `KOVA_LIVE=1`.

## Hackathon housekeeping
- `AI-TOOLS.md` in the repo lists every AI tool and model used to build Kova (GenHacks rule), updated as we go.
- The repo must be public by submission; no secrets in it.

## Not in this piece
Desktop app, web/phone UI, LAN access, one-click engine install, Hive, voice, images in chat, multiple users.
