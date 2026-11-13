# Kova architecture

Kova is a private AI assistant that runs on the user's own machine. One local
server owns the chats, memory and reminders; every client is a thin view over
its HTTP API. There is no Kova cloud service, Kova account or database server to
install. Model inference can run locally or through an explicitly connected provider.

The import package is `kova`; the distribution on GitHub is `kova-ai`.

## The shape of it

```
            ┌──────────────────────────────────────────────────────┐
            │  kova serve · FastAPI · 127.0.0.1:7700               │
            │                                                      │
  CLI  ────▶│  /health   /chats   /memory   /reminders   /models   │
 (cli.py)   │  /events (SSE)   /app + /app/assets                  │
            │                                                      │
  TUI  ────▶│   Agent ──▶ ModelClient ──▶ OpenAI-compatible model   │
 (ui.py)    │     │                                                 │
            │     ├── Store ──▶ kova.db (SQLite + FTS5)            │
 Desktop ──▶│     ├── Memory ──▶ Markdown notes                    │
  (/app)    │     ├── Tools ──▶ web, files, reminders, memory, system│
            │     └── ReminderLoop ──▶ EventBus ──▶ /events         │
            └──────────────────────────────────────────────────────┘
```

Everything in the box is one Python process. The CLI, the terminal chat and
the desktop/web app are all clients of the same server, so a chat started in
one shows up in the others.

## The server

`src/kova/server.py` builds the FastAPI app with `create_app(config)`. It is
served by `kova serve` (`src/kova/cli.py`), which calls `uvicorn.run` on
`127.0.0.1` and `config.port` (7700 by default).

You rarely start it by hand. `ensure_server` in `src/kova/client.py` probes
`/health`; if nothing answers, it launches `python -m kova serve` detached,
writes the pid to `~/.kova/server.pid`, sends its output to
`~/.kova/server.log`, and waits up to 10 seconds for health. The process then
keeps running in the background, which is why reminders can fire after you
close the chat. `kova stop` sends it `SIGTERM`.

The `_lifespan` handler creates the process-wide singletons:

- `Store` for `~/.kova/kova.db`
- `Memory` for the configured notes folder
- `ToolContext`, then the tool dict from `build_tools`
- `Agent`
- `EventBus`
- one `ReminderLoop` task

Shutdown cancels the reminder loop and any in-flight turns, and closes open
`/events` streams. Each chat has its own `asyncio.Lock`, so two turns cannot
write to the same chat at once, and `app.state.chat_turns` tracks turn tasks
so `POST /chats/{id}/stop` can cancel them. A turn is driven by its own task:
if the HTTP client disconnects, the turn still finishes and is saved.

### Routes

| Route | Auth | Purpose |
| --- | --- | --- |
| `GET /health` | public | version, active model name, whether the model answers |
| `GET /app` | public | the packaged web app shell |
| `GET /app/assets/*` | public | CSS, JS, icon and vendored libraries |
| `POST /app/session` | bearer token | mint a single-use login ticket |
| `POST /app/login` | ticket | exchange it for a session cookie |
| `POST /app/logout` | browser session | revoke the cookie |
| `GET/POST /chats` | token or session | list / create chats |
| `GET /chats/search?q=…` | token or session | full-text chat search |
| `GET/PATCH/DELETE /chats/{id}` | token or session | read, rename/mode/web, delete |
| `POST /chats/{id}/messages` | token or session | run one turn, stream SSE events |
| `POST /chats/{id}/stop` | token or session | cancel that chat's server-side turn |
| `GET/POST /memory` | token or session | list/search / save notes |
| `DELETE /memory/{slug}` | token or session | delete one note |
| `POST /memory/location` | token or session | switch memory folder at runtime |
| `GET/POST /reminders` | token or session | list / add reminders |
| `DELETE /reminders/{id}` | token or session | delete a reminder |
| `GET/POST /models` | token or session | list / add model servers |
| `POST /models/active` | token or session | choose the active model |
| `GET /events` | token or session | SSE stream of background events (reminders) |

`/health` and the app shell are deliberately public; neither contains chat
data or secrets. Every route in the API router goes through `_require_token`,
which accepts either `Authorization: Bearer <token>` (the CLI) or a browser
session cookie (the desktop app); the `/app` login and logout endpoints check
their own ticket or cookie. `_require_bearer` compares with
`hmac.compare_digest` against `app.state.token`, read from `~/.kova/token`.

### Browser login

The desktop launcher never puts the bearer token in a URL. `kova-app`
(`src/kova/app.py`) asks the server for a ticket with the token, then opens:

```
http://127.0.0.1:7700/app#ticket=<one-use-ticket>
```

The fragment is not sent to the server or written to access logs. `app.js`
removes it immediately and `POST`s it to `/app/login`, which checks that the
ticket is unused and younger than 60 seconds, then sets an `HttpOnly`,
`SameSite=Strict` cookie valid for 12 hours. Ticket and session stores live in
memory, so restarting the server logs everyone out. `src/kova/web.py` also
rejects non-loopback hosts, cross-origin requests and wrong ports. Every
response gets `nosniff`, `no-referrer` and `no-store`; `/app` also gets a
strict CSP. `Disconnect` in the app calls `/app/logout`.

## Clients

### CLI (`src/kova/cli.py`)

A Typer app. Top-level commands are `ask`, `setup`, `serve`, `stop`, `status`,
`update`, `uninstall`, `export` and `import`; the groups `model`, `memory`,
`reminders` and `chats` hold the rest. `./cli` is a small wrapper that runs
`uv run kova "$@"` from the checkout. Commands talk to the server through
`KovaClient` (`src/kova/client.py`), a synchronous `httpx` client; errors
become `KovaError` and are rendered as one red line by `_fail`.

`kova ask` streams a turn without the interactive UI, prints the answer to
stdout, and can emit `--json`. Its chats are titled with the
`ASK_TITLE_PREFIX` (`[ask] `), which `KovaClient.list_chats` filters out so
they stay out of the CLI's chat lists.

### Terminal chat (`src/kova/ui.py`)

`_run_chat` is the interactive loop behind plain `kova`. It picks the latest
chat (or a new one), prints the header, and uses `prompt_toolkit` for input,
history and Tab completion (`_CommandCompleter`). Slash commands are listed in
`_COMMANDS` and handled in the loop; `/mode`, `/web`, `/model` and `/new` call
the same API the desktop app does.

`_render_turn` consumes the SSE stream and repaints at most ten times a second
with Rich `Live`. When the reply ends, the whole turn is printed to normal
scrollback, so long answers are not cropped. Reasoning is collapsed by default;
`/think` expands it. In a pipe, `Live` is skipped and only the final rendering
is written. A daemon thread (`_watch_reminders`) listens on `/events` and
prints reminder pop-ups while you chat.

### Desktop and web app

`src/kova/app.py` starts (or reuses) the server, mints the ticket, and opens a
pywebview window when the optional `app` extra is installed. If pywebview is
missing or the system webview fails, it falls back to the default browser;
`kova-app --browser` (and `./desktop-app --browser`) skips the native window.
The webview runs with private mode, so cookies do not persist between
launches.

The UI itself is `src/kova/web_assets/`: `index.html`, `app.js`, `app.css`,
and vendored copies of marked, DOMPurify and highlight.js (no CDN). `app.js`
is a client only — it has no agent logic and no permanent token. It renders
Markdown through DOMPurify, streams turns with `fetch` + a hand-rolled SSE
reader, shows collapsible reasoning and live tool rows, and listens to
`/events` with `EventSource` for reminders. Light/dark choice is a
`data-theme` attribute stored in `localStorage`.

## One turn

`POST /chats/{id}/messages` runs `Agent.run_turn` (`src/kova/agent.py`) and
forwards its events to the client as SSE.

1. The user message is written to the store, and the chat's `mode` and `web`
   flag are read.
2. `select_tools(mode, web, tools)` chooses the tools this turn may use. Work
   mode gets all of them; chat mode gets only web tools, and `web = false`
   removes those too.
3. `memory.search(text, 5)` picks up to five relevant notes. A memory error is
   swallowed so it can never block a chat.
4. `build_system_prompt` assembles the identity for the mode, the current
   time, the mode name, the available tool names, and the chosen memories.
5. The model is the explicit per-turn `model` override or `config.active_model()`.
6. Up to eleven model calls are made. Before each call the history is reloaded
   from SQLite, `repair_tool_pairs` fills in missing tool replies for an
   interrupted turn, and `trim_history` drops old messages to fit
   `entry.context` tokens (budget ≈ `context * 4 * 0.75 - prompt length`,
   keeping the latest user message and never starting with a stray tool
   message). The system prompt is prepended.
7. `ModelClient.stream` (`src/kova/models.py`) sends an OpenAI-compatible
   `chat/completions` request with `stream: true` and yields text, reasoning
   and accumulated tool calls. Inline `<think>` tags are split out by
   `ThinkSplitter`.
8. If there are no tool calls, the assistant message is saved, a title is
   derived from the first user message if needed, and a `done` event carries
   the message id and model name.
9. If there are tool calls, the assistant message (text plus
   `tool_calls`) is saved, each tool runs (sync tools in a worker thread,
   awaitables awaited), errors become tool output rather than crashes, and
   each result is stored as a `tool` message. Results are cut at
   `MAX_TOOL_RESULT_CHARS` (32,000).
10. The last of the eleven calls is made without tool schemas, so the model has
    to answer in text instead of looping forever.

Event types on the turn stream: `text`, `reasoning`, `tool_call`,
`tool_result`, `done`, `error`, `stopped`.

## Store

`src/kova/store.py` wraps one SQLite file, `config.home / "kova.db"`, opened
with `check_same_thread=False` and guarded by one re-entrant lock. Tables:

- `chats` — id, title, created, mode, web
- `messages` — role, content, tool_calls (JSON), tool_call_id, created
- `reminders` — reminder_at, text, done

Startup applies small migrations (message timestamps, `mode`, `web`) so older
databases keep working. Search uses an FTS5 virtual table `messages_fts`, kept
in sync by triggers and back-filled once; only `user` and `assistant` text is
indexed, so tool output never pollutes results. If the local SQLite lacks
FTS5, `search_chats` falls back to a case-insensitive `LIKE` scan. Queries are
split into at most 12 de-duplicated words, and each chat contributes only its
best hit, with a short snippet.

## Memory

`src/kova/memory.py` treats memory as plain Markdown files with YAML
frontmatter:

```markdown
---
title: Coffee order
tags: [preferences]
updated: 2026-11-13T09:30:00+01:00
---
Oat flat white, no sugar.
```

`Memory.save` slugifies the title (lowercase, dashes, 60 chars), writes
`<write dir>/<slug>.md` through a temp file and rename, and refuses symlinks or
paths outside the root. `list` walks the root recursively, skipping hidden
directories such as `.obsidian`; a broken file is skipped instead of failing
the list. `search` scores each query word: title ×3, tag ×2, body ×1.

The root is `memory.path` (default `~/.kova/memory`). In normal mode the write
directory is the root. In Obsidian mode (`kova memory use <vault> --obsidian`,
or `POST /memory/location`) the root becomes the vault, so Kova can read all
your notes, but `memory.write_subdir` is `Kova` and every write and delete
stays in `<vault>/Kova/`. `POST /memory/location` swaps the in-memory `Memory` object on
the server, agent and tool context without a restart.

## Tools

A tool is a `Tool` dataclass (`src/kova/tools/__init__.py`) with a name,
description, JSON Schema parameters, and a `run` callable that takes a dict and
returns a string (or an awaitable). `build_tools` collects them and
`openai_schemas` converts them for the model.

| Tool | Module | What it does |
| --- | --- | --- |
| `web_search` | `tools/web.py` | up to five DuckDuckGo results |
| `web_fetch` | `tools/web.py` | readable text from a public page |
| `list_files` | `tools/files.py` | list a folder in the Kova files folder |
| `read_file` | `tools/files.py` | read a text file (up to 100 KB) |
| `write_file` | `tools/files.py` | create or overwrite a text file |
| `reminder_add` | `tools/reminders.py` | schedule one reminder |
| `reminder_list` | `tools/reminders.py` | list upcoming reminders |
| `memory_save` | `tools/memory.py` | save or overwrite a memory note |
| `memory_search` | `tools/memory.py` | search memory notes |
| `system_info` | `tools/system_info.py` | OS, CPU, RAM, disk, GPU and uptime (read-only) |

Safety lives in the tools, not the prompt. `files.resolve_in` confines every
path to `config.files_path` (default `~/Kova Files`). `web._check_public`
rejects anything but HTTP(S) and refuses hosts that resolve to non-global
addresses, checking every redirect hop; downloads stop at 1 MB and the text
returned is capped at 8,000 characters.

## Reminders

`parse_when` in `src/kova/tools/reminders.py` understands ISO 8601 and natural
local times such as `in 10 min`, `tomorrow 5pm`, `monday 9am`, `tonight`,
`morgen 9 Uhr`, `übermorgen`. It returns a timezone-aware datetime in the
future. `ReminderLoop` (`src/kova/reminders.py`) wakes every 15 seconds, asks
the store for due reminders, publishes `{"type": "reminder", ...}` on the
`EventBus`, shows a desktop toast via `notify-send` when available, and marks
the reminder done. `GET /events` fans those events out to the terminal chat
thread and the desktop `EventSource`.

## Modes

`src/kova/modes.py` has `MODES = ("chat", "work")` and
`DEFAULT_MODE = "work"`. A mode is stored per chat. `agent.select_tools` is the
only place that decides what a mode may do, and `agent._IDENTITIES` holds one
system-prompt identity per mode. Chat mode is plain conversation: web tools
only, no memory writes, but remembered facts are still injected into the
prompt. Work mode gets everything. The per-chat web flag applies to both.

## Theme

`src/kova/theme.py` is the single palette: `ACCENT`, `ACCENT_SOFT`, `USER`,
`MUTED`, `BORDER`, `SUCCESS`, `WARNING`, `ERROR`, plus `CODE_THEME` for Pygments
code blocks. The CLI, setup wizard and terminal chat use these constants
directly with Rich. `ui._CHAT_STYLE` mirrors them for prompt_toolkit, and the
web app mirrors them again as CSS variables in `web_assets/app.css` (`:root`
for dark, `:root[data-theme=light]` for light). Change a colour in both places
to keep the terminal and the app in step.

## Config and data on disk

Config is `~/.kova/config.toml`, loaded by `load_config` and written by
`save_config` (mode 0600, because it may hold API keys). `KOVA_HOME` moves the
whole folder.

```toml
active = "local"
port = 7700
files_path = "/home/you/Kova Files"

[memory]
path = "/home/you/.kova/memory"
# write_subdir = "Kova"   # set in Obsidian mode

[[models]]
name = "local"
base_url = "http://127.0.0.1:8080/v1"    # ModelClient appends /chat/completions
model = "some-model"
context = 32768                          # token budget for history trimming
# api_key = "..."                        # only if the server needs one
```

`ensure_token` creates `~/.kova/token` once, with mode 0600.

```
~/.kova/                  KOVA_HOME moves this
├── config.toml           settings and model servers
├── token                 bearer token for local API calls
├── kova.db               chats, messages, reminders, search index
├── memory/               Markdown notes (default location)
├── server.pid            pid of the background server
└── server.log            its stdout/stderr

~/Kova Files/             the only folder the file tools may touch
```

Backups (`src/kova/backup.py`, `kova export` / `kova import`) are ordinary
zips: `manifest.json`, chat JSON plus Markdown, memory notes, reminders, and
`config.toml` with API keys stripped. The token is never included. Update and
uninstall logic lives in `src/kova/lifecycle.py` and only manages the `uv`
tool install; it never touches chats or notes unless you ask it to purge.

## Tests

`tests/` is pytest-based. `tests/conftest.py` gives most tests a configured
stand-in model; `tests/fake_model.py` serves scripted OpenAI-compatible SSE
responses through an `httpx.MockTransport`, so no model server or network is
needed. Server tests use FastAPI's `TestClient` and a temporary `KOVA_HOME`.
The live smoke test against a real model server is marked `live` and only runs
with `KOVA_LIVE=1`.

## Where to start

| File | Job |
| --- | --- |
| `src/kova/server.py` | routes, auth, lifespan, turn plumbing |
| `src/kova/agent.py` | prompt, tools per mode, turn loop, trimming |
| `src/kova/models.py` | OpenAI-compatible streaming client |
| `src/kova/store.py` | SQLite schema, chats, search, reminders |
| `src/kova/memory.py` | Markdown notes and Obsidian mode |
| `src/kova/tools/` | the tools and their limits |
| `src/kova/cli.py` | Typer commands and rendering |
| `src/kova/ui.py` | terminal chat and slash commands |
| `src/kova/web.py` | browser sessions and static assets |
| `src/kova/web_assets/` | desktop/web client |
| `src/kova/config.py` | config file and token |
| `src/kova/theme.py` | the palette |


## Providers and model adapters

`config.py` stores schema v2: `[[providers]]` owns an id, display name, kind,
base URL, wire adapter, optional API key/custom headers and OAuth metadata.
`[[models]]` selects model IDs from those providers and keeps stable CLI aliases.
Legacy URL/key model entries migrate without changing aliases or the active
selection, deduplicating identical endpoint/credential pairs. Writes use a
same-directory 0600 temporary file and atomic replacement. Loading a legacy
config also tightens its permissions. Provider secrets are hidden from reprs,
API responses, validation errors and backup exports.

`providers.py` defines the preset catalog, explicit model listing and connection
lifecycle. Setup never invokes network discovery or sends a test completion.
Local addresses still use `server_finder.connect`; discovery remains a separate
explicit library operation, not a setup step. OpenRouter's public catalog is
paired with its authenticated, read-only `/key` endpoint for key validation.

`ModelClient` dispatches wire formats through `model_adapters.py`: existing
OpenAI-compatible streaming, Anthropic Messages (system content, tool schemas,
tool-use/results, thinking deltas), or Responses (instructions, function calls
and outputs, text/reasoning deltas). Native streams must receive a successful
terminal event before tools execute or a turn succeeds. The agent sees only
normalized events and keeps its existing tool loop.

`provider_auth.py` implements the documented public ChatGPT-plan OAuth flow:
a loopback listener is bound before opening the browser, PKCE/S256, random state
and nonce, issued-client tracking, JWT signature/issuer/audience/expiry/nonce
validation and an explicit plan-use scope. Owned tokens and rotating refreshes
remain in config; refresh is serialized with a cross-process file lock. A stable
host ID is stored with the config. Sign-out attempts renewable-session revocation
before forgetting tokens and reports unconfirmed revocation.

Installed-login reuse is deliberately separate: after consent, only a source
reference is saved. Requests read the local Codex auth file without copying,
refreshing or revoking its credentials. It uses the installed harness's Responses
backend; fresh Kova-owned ChatGPT sign-ins use the public Responses API. An
expired borrowed token requires the user to log back into the owning CLI.
No installed-login discovery touches the network.

`provider_routes.py` exposes authenticated `/providers` management, catalog
validation and bounded browser-login attempts to all clients. Browser credentials
are transient form/in-memory data, never browser storage. CLI setup writes
through the running server when available; no restart is needed. The desktop
model select uses optgroups; terminal chat's filtered picker renders provider
headings while keeping stable model aliases as the selection value.

### Authentication research

- [Public ChatGPT registration and sign-in](https://developers.openai.com/siwc/token-sharing-open-source/sign-in)
- [Public model catalog and Responses inference](https://developers.openai.com/siwc/token-sharing-open-source/models-and-inference)
- [Refresh, account sessions and revocation](https://developers.openai.com/siwc/token-sharing-open-source/profiles-and-sessions)
- [Open-source Codex device flow](https://github.com/openai/codex/blob/main/codex-rs/login/src/device_code_auth.rs)
- [Gemini OpenAI compatibility](https://ai.google.dev/gemini-api/docs/openai)
- [OpenRouter authenticated key information](https://openrouter.ai/docs/api/api-reference/api-keys/get-current-api-key)

Fresh browser authorization and hosted API-key inference require actual user
accounts and are covered by mocked HTTP/JWT tests, not automatic paid calls.

### Packaged Electron shell

`desktop/main.js` owns a single-instance Electron window and tray around the
unchanged /app client. It reads the configured KOVA_HOME and port, reuses an
authenticated server or spawns the installed CLI (uv from source as fallback),
then mints /app/session tickets for the existing fragment/login flow.
The bearer token never enters renderer code. Context isolation, sandboxing,
disabled Node integration, blocked remote resources/navigation and denied
permissions keep the shell local. Startup/error pages are bundled files.

Window bounds and Electron storage stay inside KOVA_HOME. Closing hides to
tray; Quit terminates only the shell's own server child. Logs use server.log.
electron-builder targets AppImage/deb, NSIS and DMG; the Python runtime/CLI
remains a separately installed prerequisite. See desktop/README.md.
