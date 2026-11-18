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

### Starting at login (`src/kova/autostart.py`)

`kova autostart on` keeps the server alive across logins, for reminders,
scheduled jobs and phone access. It writes exactly one per-user entry, with no
elevation: a systemd user unit at `~/.config/systemd/user/kova.service`
(`ExecStart=<python> -m kova serve`, `Environment=KOVA_HOME=…`,
`Restart=on-failure`) when `systemctl --user` answers, otherwise an autostart
`.desktop` file at `~/.config/autostart/kova.desktop`; a LaunchAgent
`~/Library/LaunchAgents/ai.kova.server.plist` with `RunAtLoad` on macOS; or the
`Kova` value in the `HKCU\Software\Microsoft\Windows\CurrentVersion\Run` key,
written through the standard-library `winreg`, on Windows.

Each writer replaces only Kova's own file or value. `status` compares it with
what the current Python and `KOVA_HOME` would write and reports a stale entry
without changing it. `off` removes the entry (`systemctl --user disable --now`
or `launchctl bootout` where available). `kova uninstall` calls the same removal.
When the platform has no usable mechanism the command exits non-zero with one
plain sentence instead of a traceback.

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

The app exposes 114 endpoints: 113 route handlers plus the static asset mount.
`tests/test_architecture_docs.py` walks the live app and fails when this table
and the code drift apart in either direction, so add or delete a row in the
same commit as the route.

**Public** — no credential. Neither of these contains chat data or secrets.

| Route | Auth | Purpose |
| --- | --- | --- |
| `GET /health` | public | version, active model name, whether the model answers |
| `GET /app` | public | the packaged web app shell |
| `GET /app/assets/*` | public | CSS, JS, icons and vendored libraries (a `StaticFiles` mount) |
| `GET /app/manifest.webmanifest` | public | persona-named install manifest |
| `GET /app/sw.js` | public | app-shell-only service worker |
| `GET /app/gallery` | public | dev-only component gallery, served only with `KOVA_GALLERY=1` |
| `POST /app/share` | browser session | bounded multipart share: text/link draft and native attachments |
| `POST /app/share/{token}` | browser session | consume an owner-bound share once |

**Browser login and phone pairing** (`src/kova/web.py`). The launcher holds the
bearer token; the browser never sees it.

| Route | Auth | Purpose |
| --- | --- | --- |

| `POST /app/session` | bearer token | mint a single-use login ticket |
| `POST /app/login` | ticket | exchange the ticket for a 12-hour session cookie |
| `POST /app/logout` | browser session | revoke the cookie |
| `POST /app/pair` | pairing claim | claim a pairing code from the phone page |

**API router** (`src/kova/server.py`, `src/kova/provider_routes.py`). Every
route here sits behind `_require_token`, which accepts either
`Authorization: Bearer <token>` (the CLI) or a same-origin browser session
cookie, so a route added to the router is authenticated by construction.

| Route | Auth | Purpose |
| --- | --- | --- |
| `GET/PATCH /privacy` | token or session | metadata-only outbound report and immediate Local only policy |
| `GET /capabilities` | token or session | what a model's provider declares (`?model=`), and what each mode offers |
| `GET/POST /chats` | token or session | list / create chats |
| `POST /chats/{chat_id}/attachments` | token or session | upload a local attachment |
| `DELETE /chats/{chat_id}/attachments/{ident}` | token or session | remove a local attachment |
| `GET /chats/search` | token or session | full-text chat search (`?q=`) |
| `GET/PATCH/DELETE /chats/{chat_id}` | token or session | read, rename/mode/web/permission/cwd/pin/colour/icon, delete |
| `GET /chats/{chat_id}/approvals` | token or session | approvals and questions still waiting in this chat |
| `POST /chats/{chat_id}/approvals/{approval_id}` | token or session | answer a waiting approval or question |
| `GET /chats/{chat_id}/checkpoints` | token or session | this chat's change sets, newest first |
| `GET /chats/{chat_id}/checkpoints/{checkpoint_id}` | token or session | one change set's files |
| `GET /chats/{chat_id}/checkpoints/{checkpoint_id}/diff` | token or session | one changed file's diff and both sides |
| `POST /chats/{chat_id}/checkpoints/{checkpoint_id}/restore` | token or session | put the working folder back to this checkpoint |
| `POST /chats/{chat_id}/compact` | token or session | summarize older turns now (`compact_skipped` if there is nothing to do) |
| `POST /chats/{chat_id}/fork` | token or session | copy this chat, messages and compacted context, into a new one |
| `POST /chats/{chat_id}/messages` | token or session | run one turn, stream SSE events; `queued: <id>` sends that queued message under the limits of whoever queued it |
| `GET/POST /chats/{chat_id}/queue` | token or session | list / queue a message to run after the current turn |
| `POST /chats/{chat_id}/queue/next` | token or session | take the oldest queued message as the next turn; a paired phone's comes back `remote` and stays queued until sent with `queued: <id>` |
| `DELETE /chats/{chat_id}/queue/{message_id}` | token or session | drop one queued message |
| `POST /chats/{chat_id}/steer` | token or session | put a message into the turn running now |
| `POST /chats/{chat_id}/stop` | token or session | cancel that chat's server-side turns |
| `POST /transcribe` | token or session | transcribe audio to text (`[voice] stt = "local"`); `browser` transcribes client-side |
| `GET /events` | token or session | SSE stream of background events (reminders, jobs, conversation state) |
| `GET/POST /jobs` | token or session | list / add scheduled recurring jobs |
| `DELETE /jobs/{jid}` | token or session | delete a job |
| `GET/POST /memory` | token or session | list/search / save notes |
| `POST /memory/consolidate` | token or session | plan one consolidation run, and apply it unless asked to dry-run |
| `GET /memory/diary` | token or session | what the background memory passes did, oldest first |
| `POST /memory/location` | token or session | switch memory folder at runtime |
| `GET /memory/revisions` | token or session | recent background memory writes with their undo state |
| `GET/PATCH /memory/settings` | token or session | read / change the background passes' switches and gates |
| `POST /memory/undo` | token or session | reverse recorded review/consolidation writes |
| `PATCH/DELETE /memory/{slug}` | token or session | rewrite one note in place / delete it |
| `GET/POST /models` | token or session | list / add model servers |
| `PATCH /models` | token or session | update one model's settings, such as vision |
| `POST /models/active` | token or session | choose the active model |
| `POST /compare` | token or session | run one prompt on several models at once, side by side |
| `GET /mcp` | token or session | list MCP servers with their tools and state |
| `POST /mcp` | token or session, local only | add an MCP server and connect it (never from a paired phone) |
| `POST /mcp/reload` | token or session | reconnect every MCP server now, including any from plugins |
| `PATCH/DELETE /mcp/{name}` | token or session | switch an MCP server on or off / remove it and its grants |
| `POST /mcp/{name}/test` | token or session | reconnect one MCP server now and report its tools |
| `PATCH /mcp/{name}/tools/{tool:path}` | token or session | switch one MCP tool on or off, or always allow it |
| `GET/PUT /permission-rules` | token or session | read / replace the rules that pre-approve tools |
| `GET /autostart` | token or session | inspect start-at-login availability and state |
| `POST /autostart/{action}` | token or session | enable or disable start at login (`on` or `off`) |
| `GET/POST /persona` | token or session | read / set the assistant's name and tone |
| `GET/PUT /preferences` | token or session | read every UI preference / merge validated changes into them |
| `GET /themes` | token or session | list the built-in themes and the user's theme files, with any file's error |
| `GET /themes/{theme_id}` | token or session | resolve one theme's tokens over the base it extends |
| `GET/POST /providers` | token or session | list connected providers / connect one |
| `GET /providers/catalog` | token or session | the providers and models offered for selection |
| `GET /extensions/catalog` | token or session | the extensions catalog (configured index or the bundled one), with install state |
| `POST /extensions/catalog/{name}/preview` | token or session, local only | download and verify one extension; return what it adds and a one-use ticket |
| `DELETE /extensions/catalog/preview/{ticket}` | token or session | drop a verified download the user did not confirm |
| `POST /extensions/install` | token or session, local only | install a previewed extension once the user confirms |
| `GET /extensions/plugins` | token or session | installed plugins with exactly what each adds |
| `PATCH/DELETE /extensions/plugins/{name}` | token or session | enable (local only) or disable a plugin / remove it and everything it added |
| `GET /extensions/skills` | token or session | every skill the model sees, with its origin |
| `GET/PUT/DELETE /extensions/skills/{skill_id:path}` | token or session | read, edit or delete one skill (plugin skills are read-only) |
| `GET /local-models` | token or session | opt-in hardware/backend check and rated local-model catalog |
| `POST /local-models/install` | token or session | pull and warm up a local model, stream SSE, then register it |
| `DELETE /local-models/{provider_id}` | token or session | remove a managed local model and its connection |
| `POST /providers/validate` | token or session | check a provider's address and credentials before saving |
| `DELETE /providers/{identifier}` | token or session | remove a provider connection |
| `POST /providers/{identifier}/login` | token or session | sign in to a connected provider again |
| `POST /providers/{identifier}/logout` | token or session | sign out of a provider connection |
| `POST /providers/auth/start` | token or session | start a browser/harness sign-in flow |
| `GET/DELETE /providers/auth/{key}` | token or session | poll / cancel that sign-in |
| `GET/POST /reminders` | token or session | list / add reminders |
| `DELETE /reminders/{rid}` | token or session | delete a reminder |
| `GET /usage` | token or session | tokens, estimated spend and speed per day and model (`?days=`), plus the monthly budget |

**Phone routes** (`src/kova/pairing.py`). These also carry `require_local`, so
they are refused on the LAN listener: a paired phone can chat, but never manage
devices or pairing.

| Route | Auth | Purpose |
| --- | --- | --- |
| `GET /devices` | token or session, local only | phones paired with this server |
| `DELETE /devices` | token or session, local only | unpair every phone |
| `DELETE /devices/{device_id}` | token or session, local only | unpair one phone |
| `PATCH /devices/{device_id}` | token or session, local only | replace what one phone may do (its capabilities) |
| `GET/POST /phone` | token or session, local only | read / turn the phone listener on or off |
| `POST /phone/pair` | token or session, local only | start a pairing code |
| `GET/DELETE /phone/pair/{code_id}` | token or session, local only | poll / cancel a pairing code |

`/health` and the app shell are deliberately public; neither contains chat
data or secrets. Every route in the API router goes through `_require_token`,
which accepts either `Authorization: Bearer <token>` (the CLI) or a browser
session cookie (the desktop app); the `/app` login and logout endpoints check
their own ticket or cookie. `_require_bearer` compares with
`hmac.compare_digest` against `app.state.token`, read from `~/.kova/token`.

### Streaming events

Two routes answer `text/event-stream`: `POST /chats/{chat_id}/messages` for one
turn, and `GET /events` for what happens while no turn is running. `_sse`
(`src/kova/server.py`) writes the JSON `type` as the SSE event name and the
whole object as its `data`, so a client can switch on either.

Turn stream (`src/kova/agent.py`): `text`, `reasoning`, `privacy`,
`tool_call_delta`, `tool_call`, `tool_result`, `approval`, `approval_resolved`,
`question`, `question_resolved`, `plan`, `notice`, `steer`, `review`, `stopped`,
`done` and `error`. The stream stays open across tool calls and approvals; `done` closes a
successful turn with the stored message id, model name and `usage` (see
[Usage and cost](#usage-and-cost)), plus `budget_warning` at most once a day when
this month's estimated spend passes 80% of `[usage] monthly_budget_usd`; `stopped` closes a
turn that was cancelled, `error` closes a turn that failed. A turn keeps running
to completion even if the HTTP client disconnects, and `POST /chats/{chat_id}/stop`
cancels it server-side.

`GET /events`: `ping` at once and then every keepalive interval, so a browser can
tell a quiet stream from a dead one; `reminder` when a reminder is due; `job`
after a scheduled job ran; `conversation_state` when a chat moves between
`idle`, `working` and `blocked-on-approval`. Reminders and jobs also try a
desktop notification.

Payload shapes, and the HTTP round-trip that answers an approval, are in the
[README's client protocol section](../README.md#client-protocol).

### Browser login

The web launcher never puts the bearer token in a URL. `kova-web`
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

### Phone pairing (`src/kova/pairing.py`)

With `bind = "lan"` in `config.toml` (set by `kova pair` or *Connect your
phone*), the server's lifespan starts a second, in-process uvicorn listener for
the same app on the private LAN address only. Requests through it are marked
`scope["kova_lan"]` by an ASGI guard that refuses non-private peers, any `Host`
but the LAN address (DNS rebinding) and, outside the page and the pairing claim,
anything without a paired device's cookie. On those requests the bearer token,
browser sessions and the local-only routes (`/phone`, `/phone/pair`,
`/devices`) are refused. Pairing secrets are one-use and live for two minutes in
memory; device tokens are stored as SHA-256 hashes in `devices.json` and survive
restarts. Each device record also holds its capabilities (`pairing.CAPABILITIES`,
default `chat` and `models:read`); expensive or configuration-changing routes
declare `Depends(require_capability("models:write"))` and friends, which refuse
a LAN request whose device lacks it and never limit the computer itself. The
limits also reach the agent: `Agent.run_turn(capabilities=…)` drops tools whose
`pairing.action_capabilities` exceed the phone's set (commands, jobs and MCP
tools need `host:exec`, as do file tools outside Kova Files), each pending
approval records what allowing it needs and the asking turn's own limits (a
phone may put an answer or reason only into a turn no stronger than itself), and
a chat a phone wrote in keeps that phone's limits for every later turn
(`Store.limit_chat`, applied in `Agent.run_turn` for every caller). Memory
writes need `memory:write`. The matrix is in
[remote-access.md](remote-access.md#what-a-paired-phone-may-do).

## Clients

### CLI (`src/kova/cli.py`)

A Typer app. Top-level commands include `ask`, `compare`, `setup`, `serve`, `stop`,
`status`, `usage`, `privacy`, `config`, `update`, `uninstall`, `export`, `import`, `pair`, `devices`
and `doctor`; groups `model`, `providers`, `mcp`, `memory`, `reminders`, `jobs`,
`chats`, `plugin`, `catalog`, `skills`, `prefs`, `themes` and `autostart` hold their subcommands. `./cli` is a small wrapper that runs
`uv run kova "$@"` from the checkout. Commands talk to the server through
`KovaClient` (`src/kova/client.py`), a synchronous `httpx` client; errors
become `KovaError` and are rendered as one red line by `_fail`. `kova config` is
the exception: it reads and writes `config.toml` directly, validates every value
against a small schema, and never starts the server. `config show` redacts
provider keys, custom headers and OAuth data; [CONFIG.md](CONFIG.md) documents
every field.

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

`src/kova/app.py` holds both launchers. `kova-app` runs the Electron app in
`desktop/` — the checkout it runs from, or the copy the installers stage under
`KOVA_HOME/desktop`. Installers prepare Electron and register a normal OS launcher
by default (opt out with `--no-desktop`); those launch the binary directly.
The developer/terminal launcher can still run `npm ci` while
`node_modules/.bin/electron` is missing. Without Node.js/npm it says so and opens
the browser; for any other failure it exits non-zero. The Electron shell starts
(or reuses) the server itself, reads `~/.kova/token`, and mints its own one-use
ticket; the bearer token never reaches a command line or a URL. `kova-web` (and
the older `kova-app --browser`) starts (or reuses) the server, mints the ticket,
and opens the default browser. In a checkout, `./desktop-app` is a thin wrapper
over `uv run kova-app`.

The UI itself is `src/kova/web_assets/`: `index.html`, `tokens.css`, `app.css`, `app.js`,
`palette.css`, `palette.js`, `settings.css`, `settings.js`, `chat_list.js` and `chat_list.css`
(the sidebar's pins and date groups), `chat-appearance.js` and `chat-appearance.css`
(per-chat colour and icon), `token-sheet.html` (a standalone page that shows every
semantic token and re-measures the build gate's contrast pairs in the browser),
vendored copies of marked, DOMPurify and highlight.js, and the
variable fonts Inter and JetBrains Mono under `fonts/`. Nothing is fetched at runtime — the
CSP allows scripts, styles and fonts from `'self'` only. `app.js` is a client
only: it has no agent logic and no permanent token. It renders Markdown
through DOMPurify, streams turns with `fetch` + a hand-rolled SSE reader,
shows reasoning folded into "Thought for Ns" rows (hideable in Settings) and
live tool rows, and listens to `/events` with
`EventSource` for reminders. Light/dark choice is a `data-theme` attribute
stored in `localStorage`. The sidebar keeps pinned chats on top and groups the
rest into Today, Yesterday, Previous 7 days and Older by local calendar day;
`pinned` lives on the chat, so every client shows the same order. Optional sound cues (`web_assets/sounds.js`, off by
default, with a volume and per-event switches in the *Sounds* panel) are
synthesized with WebAudio, so no audio file ships and nothing is fetched; a
finished reply only chimes while the window is unfocused. The same toast stack
(`web_assets/toast.js`) forwards each message to one OS notification while the
window is hidden, with the toast's own title and text; the *Notify me* switch
and the service worker decide whether it appears.

The shell is deliberately minimal. The sidebar is the logo, **New chat**, one
search field that filters chats, the chat list and a single **Settings** row.
The message box shows **+** (attach files, slash commands, and an incognito chat
on the start screen), the text, one chip naming the model, and Send; the chip
opens one popover with the model, Work/Chat mode, permission mode, working
folder and web search. The header is the sidebar toggle, the chat title and,
once a chat is open, its actions menu. A healthy connection shows nothing:
reconnecting the event stream and polling `/health` are silent, the model's
state is a detail of the chip, and only an action that actually failed (Kova
unreachable, signed out, a used pairing code) shows one quiet line above the
message box with **Retry**.

`settings.js` layers one Settings view over the dialogs `app.js` still owns:
General (theme, sounds, shortcuts, startup, sign out), Models and providers,
Memory, Reminders, Extensions and MCP, Privacy, Phone, and Install and
notifications. A section nav and a search box filter individual settings; rows
activate the existing controls by id (kept, undrawn, in `index.html`'s
`#settings-actions`) rather than reimplementing them, so every dialog is also
reachable from the command palette.

The installed PWA handles a platform share with its service worker: files wait
briefly in CacheStorage, then the page uploads them through the normal
authenticated `/chats/{id}/attachments` API and shows ordinary attachment chips.
If the worker is not in control, `/app/share` saves the same native attachments
on the server before redirecting to a one-use composer. Text and link shares
stay editable drafts. Shared images obey the selected model's vision setting;
nothing invents a base64-in-text attachment format. A draft chat that is never
redeemed is deleted with its files on the next browser request after its five
minutes expire; the page also drops expired worker-staged file bytes. Share
tokens live only in memory, so a restart makes unredeemed shares unreachable:
the startup sweep removes the draft chats and files they journaled, while a
chat that already has messages is always kept.

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
   `ThinkSplitter`. When `privacy.secrets` is enabled and the provider is not
   loopback, `privacy.protect_messages` scans a deep copy of the outgoing
   messages first: `redact` sends placeholders and a safe count, while `block`
   raises before any request. The stored transcript and tool arguments are not
   changed.
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

Event types on the turn stream: `text`, `reasoning`, `privacy`, `tool_call`,
`tool_result`, `done`, `error`, `stopped`.

## Store

`src/kova/store.py` wraps one SQLite file, `config.home / "kova.db"`, opened
with `check_same_thread=False` and guarded by one re-entrant lock. Tables:

- `chats` — id, title, created, mode, web, permission, cwd, pinned, colour, icon
- `messages` — role, content, tool_calls (JSON), tool_call_id, created, and
  nullable usage columns (`USAGE_COLUMNS`) set only on a turn's final answer
- `reminders` — reminder_at, text, done

Startup applies small migrations (message timestamps, `mode`, `web`, `pinned`,
`colour`, `icon`)
so older databases keep working. Search uses an FTS5 virtual table
`messages_fts`, kept in sync by triggers and back-filled once; only `user` and
`assistant` text is indexed, so tool output never pollutes results. If the local SQLite lacks
FTS5, `search_chats` falls back to a case-insensitive `LIKE` scan. Queries are
split into at most 12 de-duplicated words, and each chat contributes only its
best hit, with a short snippet.

### Usage and cost

`src/kova/usage.py` measures each answer; nothing in it touches the network.
`ModelClient` asks OpenAI-compatible servers for `stream_options.include_usage`
(dropping the option once if a server rejects it), reads the Anthropic
`message_start`/`message_delta` and Responses `response.completed` usage in
`model_adapters.py`, and emits one `usage` model event before `done`.
`Agent.run_turn` feeds every model round of a turn into a `TurnMeter`: tokens
are summed, a round without a usage block is estimated at four characters a
token and flagged, `ms_first_token` counts from the start of the turn, and
`ms_generation` only counts time while a model streams, so tokens per second
excludes tool runs. The total is stored on the final assistant message with
`Store.record_usage` and sent in the `done` event. Forks copy no usage, so spend
is never counted twice.

Prices are USD per million tokens. `price_for` checks `[pricing]` overrides
first (by model name, `kind/model-id` or model id), then treats a `local`
provider, a managed engine or a loopback address as free, then the built-in
`PRICING` table for exact ids and their dated snapshots. Anything else stays
unpriced: the UI shows tokens and speed, never a guess. `GET /usage` and
`kova usage` aggregate `Store.usage_rows` by local day and by model in
`summarize`, which also reports this month against the optional budget. A
budget only warns; it never blocks a turn.

### Incognito conversations

`POST /chats {"ephemeral": true}` creates a conversation in a separate SQLite
`:memory:` connection, never a flagged row in the disk database. Its reserved
`incognito-` id namespace routes chat-scoped reads and writes (including queues,
steers, plans and compaction summaries) to RAM even after deletion, so a late
write cannot fall back to disk. SQLite temporary tables also use memory. The
normal store listing, full-text/LIKE search and backups only read the persistent
connection; the authenticated chat list explicitly includes live incognito chats
so clients can display them. Imports and forks cannot turn one into a saved chat.

Incognito is conversation-only: no tools, memory recall, writes, review,
consolidation, file sessions or checkpoints. Uploads are rejected before multipart
parsing can spool files to disk, including paste/drop uploads in the web client.
Compaction still works, but its summary lives only in the in-memory connection.
No transcript, reasoning, tool arguments or provider error bodies are logged by
these paths. A normal chat is unaffected.

Entry points are `kova --incognito`, `kova ask --incognito`, `/incognito` in the
terminal or web composer, and the **+** menu's **Incognito chat** (on a new chat),
the palette's **New incognito chat** or **Ctrl+Shift+N** in the web/desktop client. Each shows an Incognito marker and explains that the provider
still receives messages. Terminal input history is not saved once a session has
entered incognito, even after switching back to a saved chat. One-shot `ask`
discards its chat in `finally`, including on failure. Leaving an incognito chat
in a client deletes it; normal browser close/reload sends a keepalive delete.
Deleting an active incognito cancels its turn. Shutdown drops the entire RAM
store, and a fresh server cannot recover it, including after a crash.

This controls Kova's storage, not model-provider retention, terminal scrollback,
user-directed shell redirection, browser/OS crash dumps or OS swap. If a browser
or terminal is killed before it can send cleanup, the RAM-only chat can remain
in the current server until explicitly deleted or the server stops; it is never
recovered from disk.

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

The store has a character budget (`memory.budget`, default 8000) covering
titles, tags and bodies. Model-facing writes are `memory_add`, `memory_patch`
(one unique find/replace span inside one note), `memory_remove` and
`memory_list`; there is deliberately no whole-note replace. An over-budget
write raises `MemoryBudgetError` with the exact overflow instead of
truncating, and the current usage is shown in the system prompt memory header.
`Memory.save` keeps its upsert behaviour for the CLI and HTTP API, and backup
restores bypass the cap so old archives always import.

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

### MCP tools (`src/kova/mcp.py`)

Any [Model Context Protocol](https://modelcontextprotocol.io) server can add
tools. Each `[[mcp]]` entry in `config.toml` (see
[CONFIG.md](CONFIG.md#mcp-servers)) is either a **stdio** server (Kova starts
`command args…` and speaks newline-delimited JSON-RPC on its pipes) or a
**streamable HTTP** server (Kova POSTs to `url`, reads JSON or SSE replies, and
keeps the `Mcp-Session-Id`, initializing again when the server forgets it). The
client is hand-written on `subprocess` and `httpx`; there is no SDK dependency.

```
config.mcp ──▶ McpManager.reload ──▶ McpClient (one per enabled server)
                    │                    ├── _Stdio: Popen + reader thread
                    │                    └── _Http: httpx POST, JSON or SSE
                    ▼
            McpManager.tools() ──▶ build_tools ──▶ agent.tools  ("server__tool")
```

- **Startup.** The lifespan creates one `McpManager` (on `ToolContext.mcp`) and
  connects every enabled server in a background task, in parallel, so a slow
  `npx` download never delays `/health`. When it finishes, `build_tools`
  rebuilds the registry and the agent's dict is swapped in one step. The
  `/mcp` routes (`src/kova/mcp_routes.py`) do the same after every change, so
  `kova mcp add` and the settings switches apply without a restart.
- **Names.** A tool becomes `<server>__<tool>`, cleaned to the characters
  function names allow and cut at 64, so it can never shadow a built-in or
  another server's tool. Descriptions start with `[<server> MCP]`.
- **Permissions.** MCP tools are `Tool(external=True)`. `needs_approval` asks
  before every call in Manual and Auto; only Full access, or a permission rule
  (checked first, as for every tool), skips the card. The settings switch
  *Always allow* writes the rule `{"action": "allow", "tool": "<server>__<tool>"}`;
  removing a server removes every rule for its tools. Chat mode never offers MCP
  tools, like every non-web tool.
- **Crashes.** When a stdio server exits, the call in flight fails with the end
  of its stderr (it is never retried: tools can have side effects), and the
  next call starts it again, at most three starts a minute. A server that fails
  to connect is shown with its error and contributes no tools; the others are
  unaffected.
- **Sandbox.** `sandbox = true` starts a stdio server through `kova.sandbox`:
  it may write only to `KOVA_HOME/mcp/<name>` (its working folder) and temporary
  space. Without Landlock the server is not started. It is off by default
  because most `npx`/`uvx` servers write to their package caches.
- **Secrets.** `env` and `headers` can hold tokens. They are hidden from reprs,
  shown only by name in `kova config show`, never returned by `GET /mcp`, and
  dropped from backups.

The module's public API (`McpServer`, `McpClient`, `McpManager`,
`build_mcp_tools`, `servers_from_config`, `validate_server`, `McpError`) is kept
small on purpose: plugins reuse it to bring their own servers.

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
`EventBus`, shows an OS notification with the platform's notifier
(`notify-send`/`gdbus`, `osascript`, or PowerShell/`msg.exe`), and marks the
reminder done. `GET /events` fans those events out to the terminal chat
thread and the desktop `EventSource`.

## Modes

`src/kova/modes.py` has `MODES = ("chat", "work")` and
`DEFAULT_MODE = "work"`. A mode is stored per chat. Each mode declares the
capabilities it offers (`MODE_CAPABILITIES`); `agent.select_tools` reads that
declaration rather than the mode name, and `agent._IDENTITIES` holds one
system-prompt identity per mode. Chat mode is plain conversation: web tools
only, no memory writes, but remembered facts are still injected into the
prompt. Work mode gets everything. The per-chat web flag applies to both.
`providers.offered` combines a mode's declaration with the active provider's,
which is what the clients read (see *The capability contract* below).

## Theme

`src/kova/theme.py` is the single palette: `ACCENT`, `ACCENT_SOFT`, `USER`,
`MUTED`, `FAINT`, `BORDER`, `SUCCESS`, `WARNING`, `ERROR`, `BASH`, `MEMORY`,
`CODE`, the logo `GRADIENT`, plus `CODE_THEME` for Pygments code blocks. The
plain chat, branding, help and status resolve their hex colours through
`tui.style.colour` (the wordmark, `--help` styling and the status card live in
`src/kova/tui/brand.py`). `KOVA_THEME` overrides `[tui] theme`, with dark as
the default. The shared light translation darkens text and pales backgrounds;
reading the theme for styling never creates or migrates configuration. The
full-screen chat paints with raw escapes through `src/kova/tui/style.py`, which
builds each role from the theme's hex value and converts it for the terminal:
24-bit, the nearest of 256 colours, a hand-picked one of the 16 basic colours,
or none at all (`NO_COLOR`, `TERM=dumb`). `KOVA_COLORS=truecolor|256|16|none`
overrides the detection. `ui._chat_style()` resolves them for prompt_toolkit, and the
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
budget = 8000            # characters across all notes; 0 = unlimited
# write_subdir = "Kova"   # set in Obsidian mode

[voice]
stt = "local"          # "browser" for the desktop web UI
tts = "espeak-ng"      # "off" disables spoken replies
max_seconds = 60        # longest voice clip the server accepts

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
├── config.toml           settings, model servers and MCP servers
├── mcp/<name>/           working folder of each stdio MCP server
├── token                 bearer token for local API calls
├── kova.db               chats, messages, reminders, search index
├── memory/               Markdown notes (default location)
├── preferences.json      the clients' UI preferences
├── server.pid            pid of the background server
└── server.log            its stdout/stderr

~/Kova Files/             the only folder the file tools may touch
```

Backups (`src/kova/backup.py`, `kova export` / `kova import`) are ordinary
zips: `manifest.json`, chat JSON plus Markdown, memory notes, reminders, and
`config.toml` with API keys, provider headers and MCP `env`/`headers` stripped. The token is never included. Update and
uninstall logic lives in `src/kova/lifecycle.py` and only manages the `uv`
tool install; it never touches chats or notes unless you ask it to purge.

## Plugins

A plugin is one folder described by `kova-plugin.toml`, installed into
`KOVA_HOME/plugins/<name>/`. `src/kova/plugins.py` parses and validates the
manifest (`name`, `version`, `description`, `author`, `homepage`, `min_kova`,
`mcp`, `skills`, `commands`, `permissions`) and manages the
`kova plugin install|list|enable|disable|remove|update` lifecycle.

Nothing a plugin declares is trusted or run without the user confirming. The
CLI shows exactly what a plugin adds — MCP server command lines and URLs, skill
names, slash commands and permission rules — and asks before installing or
updating; MCP `env` values may hold secrets, so only their keys are shown.

What a plugin contributes, and how it is wired in:

- **Skills** are read-only SKILL.md-style Markdown loaded through the existing
  `kova.skills` loader (`plugin_skill_roots` feeds them in as extra roots), so
  they appear in the prompt and `skill_load` finds them, but `skill_write` and
  `skill_remove` never touch them — only Kova's own `skills/Kova/` writes.
- **Slash commands** are canned prompts sent as the user's turn; the terminal
  chat (both the simple loop and the full-screen app) lists and dispatches them
  like built-in commands. A plugin command runs nothing by itself.
- **MCP servers** are declared with the `{name, command, args, env, url,
  enabled}` shape. `plugin_mcp_entries()` turns the enabled plugins' servers into
  manager entries named `<plugin>_<server>` (lowercase letters, digits, `-`, `_`),
  so they cannot collide with the user's own servers and their `<server>__<tool>`
  names stay valid. The MCP manager loads them with the user's servers at start
  and on `POST /mcp/reload`; `kova plugin enable|disable|install|remove` calls
  that route when the server is running. `kova mcp list` marks them, and
  `kova mcp remove` refuses them and points to `kova plugin`. Disabling or
  removing a plugin also drops any saved always-allow rules for its tools.
- **Default permission rules** are merged into `permission-rules.json`, tagged
  with `source = "plugin:<name>"`, and go through the exact same permission
  policy and sandbox as built-in tools. Disabling or removing a plugin deletes
  its rules; removing also deletes its folder, skills and commands.

## Extensions catalog

`src/kova/catalog.py` reads a JSON index of plugins and skills from
`[catalog] url` (an https URL or a local file) and falls back to the index
bundled in `src/kova/catalog_data/`, which also holds the first-party
extensions. Every available entry pins one `version` and a `source` with a
`sha256`. The format, and how to publish an extension, is in
[EXTENSIONS.md](EXTENSIONS.md).

Installing is fetch, verify, show, confirm, write. `catalog.stage` downloads
(or copies a `bundled:` source) into a private temp folder, refuses a
checksum, name or version that differs from the index, and unpacks archives
with regular files only. The CLI then prints the same screen as
`kova plugin install`; the web app gets it from
`POST /extensions/catalog/{name}/preview` with a one-use ticket and only
`POST /extensions/install` writes. Plugins are installed through
`plugins.install` with the source recorded as `catalog:<name>`; skills land in
`skills/catalog/`. `kova catalog search|show|install` and `kova plugin install
<name>` use it.

`src/kova/extensions.py` lists every skill the model sees with its origin:
`learned` (Kova wrote it, `skills/Kova/`), `catalog`, `yours` (a file you put
in `skills/`) and `plugin` (read-only; disable or remove the plugin instead).
`kova skills list|show|rm` and the **Extensions** dialog (Installed · Plugins ·
Skills) use it; the dialog can also edit a skill in place.
`src/kova/extension_routes.py` refreshes the running agent's plugin skills,
permission rules and MCP servers after a change, so nothing needs a restart.

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
| `src/kova/privacy.py` | opt-in outbound scan, redact or block, loopback bypass |
| `src/kova/secrets.py` | bounded credential detectors and placeholders |
| `src/kova/store.py` | SQLite schema, chats, search, reminders |
| `src/kova/memory.py` | Markdown notes and Obsidian mode |
| `src/kova/tools/` | the tools and their limits |
| `src/kova/mcp.py` | MCP client: stdio/HTTP servers become tools |
| `src/kova/catalog.py` | extensions catalog: index, checksums, install |
| `src/kova/cli.py` | Typer commands and rendering |
| `src/kova/autostart.py` | per-user start-at-login entries |
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
terminal event before tools execute or a turn succeeds. OpenAI-compatible streams
must receive `[DONE]` or a nonempty `finish_reason`; an early connection close
is reported as an error and accumulated tool calls are not executed. The agent
sees only normalized events and keeps its existing tool loop.

`privacy.py` is the only outbound text guard. It applies the detectors in
`secrets.py` to a deep copy of each request when `[privacy] secrets` is
`redact` or `block`, for every adapter. A provider is bypassed only when its
parsed URL hostname is a literal loopback address or the exact name
`localhost`; LAN names and DNS results never count as local. Scan limits are
bounded, and an unscanned tail is removed or blocks the request rather than
being sent untouched.

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

### The capability contract

Every provider declares what it can do, once, so no UI layer branches on a
provider or mode name. `model_adapters.ADAPTER_CAPABILITIES` states what each
wire format carries (`tools`, `reasoning`); `providers.CATALOG` states what each
provider kind adds (an API key, a browser or installed-agent sign-in, an
editable address, custom headers, a reasoning stream).
`providers.CAPABILITY_GATES` names the layers that must agree before a UI may
offer something: `tools` needs the provider and the mode, `approvals` the mode
alone, everything else the provider alone. `providers.offered(provider, mode)`
combines them, and `providers.capabilities(provider)` is the provider's own
declaration, which the setup form and `kova providers list` show.

`GET /capabilities` publishes a model's declarations and the `offered` set for
each mode — the one thing the CLI, the terminal chat and the web app read. It
describes the active model by default, or the model named by `?model=`
(404 when unknown), so the composer can gate the model it actually sends. The
web provider form shows only the fields a kind declares, chat mode disables the
permission and folder pickers, and `/think` is refused where no reasoning stream
exists. The server enforces the same declarations, so a client that forces a
field a kind does not take (an API key for ChatGPT, custom headers for OpenAI,
an approval in a mode that never asks) is refused rather than silently obeyed.
A model with no provider entry — a legacy single-model config — declares
everything, so nothing is hidden from it.

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

### Local chat attachments

`attachments.py` stores chat-scoped uploads under `KOVA_HOME/attachments`, with
0600 files and metadata in SQLite. Extraction is local; install
`kova-ai[attachments]` for PDF, XLSX and PPTX readers. DOCX uses a bounded ZIP/XML
fallback. `read_attachment` accepts an attachment ID and line range and cannot
read another chat's uploads. Deleting or purging a chat removes its upload bytes.

An upload is provisional until a saved message references it. The server sweeps
at startup and hourly: unreferenced uploads older than 24 hours are deleted, while
`used` attachments, uploads a saved message links to (name-only metadata, with no
document text), and older rows whose transcript still names their id are kept
until the chat is deleted or the user removes them. A chat holds at most
three unsent uploads, 100 attachments and 500 MB; Kova stores at most 2 GB of
attachment bytes overall. An upload that would exceed a limit fails with 409 and
a message naming the limit, before any bytes are written.

Turn on Vision for one model in the web app's Model settings (or set
`vision = true` on that `[[models]]` entry in config.toml) only when the model
supports images. The adapter must also declare vision; the API and composer both
enforce it, and `GET /capabilities?model=…` reports the selected model's
contract. Non-vision models require an OCR/text alternative (local OCR is not
bundled). Extracted text, or image data for a declared vision model, is sent to
the configured model only as part of a requested chat turn; no external
extraction or file-upload service is used. Raw images are not stored in
transcript messages and must be attached again for a later visual turn.

Extracted text is request context, not transcript data. A turn stores only the
user's typed message; the extracted block is re-applied in memory to that
message for each model call, so search, Markdown/JSON exports, backups and
generated titles never contain document text. The message gets a small
metadata link to the uploaded files it used (name, id and order, with no
extracted text), so clients can show the file names after a reload instead of
an empty bubble, and a fork copies and remaps those links with its attachment
copies. The system prompt carries a turn-only index of the chat's attachment
names and ids, which keeps `read_attachment` usable on later turns. One
consequence: a title generated for an attachment-only turn uses the file name,
and an assistant answer that quotes the document is still stored like any
other reply.

### Getting a model on this PC (Ollama or managed llama.cpp)

`local_models.py` owns the curated Q4_K_M catalog, conservative fit estimates
and explicit Ollama lifecycle. `tools/system_info.hardware_profile()` reuses
the system tool's probes but returns structured data, without paths, CPU
identity or uptime. Unknown RAM, disk or GPU memory never becomes a confident
recommendation. The largest measured GPU is used, not summed VRAM. Estimates
reserve display/OS memory and use a bounded 4096-token context. Disk is an
estimate for the current account or `OLLAMA_MODELS` volume; a separately run
Ollama daemon may use another account/volume, which the UI explicitly notes.

Setup offers **Use a model on this PC** (also `kova providers add this-pc`).
The desktop offers the same step under **Settings → Models and providers**. Only choosing that
step probes the usual loopback Ollama, LM Studio and llama.cpp endpoints;
there is no LAN scan or automatic model download. Without measurable VRAM,
the user can explicitly choose the small 3B CPU model, with a speed warning.
The catalog shows download size, quantization, fit reasons and licence links.
A too-big/unknown fit is blocked before any download.

Authenticated `GET /local-models` returns the hardware card, rated catalog and
managed connections. `POST /local-models/install` streams SSE `progress`,
`ping`, `error` and terminal `done` events. One lifecycle operation runs at a
time per server. Disconnect/cancel closes the upstream request; retry calls
Ollama's pull again so Ollama can resume its cached layers. Kova never keeps
its own `.part` files or weights. Pull must explicitly succeed before an
Ollama alias is created with persistent `num_ctx` (and `num_gpu=0` in CPU mode),
so later OpenAI-compatible chats retain the chosen settings. Empty-prompt
warmup loads the model without sending a test chat. GPU selection additionally
requires Ollama's running-model response to confirm full GPU residency; a
silent CPU fallback does not produce a ready provider. Only then is a local
provider saved and selected, using the alias and the same context budget.

`DELETE /local-models/{provider_id}` unloads/deletes the managed alias and its
source tag, then removes the connection. The desktop Remove action warns that
other Ollama apps may use the source tag. A source tag referenced by another
Kova model/CPU variant is kept, and shared layers used by other Ollama tags
may remain. Unrelated tags/providers and the Ollama service are never stopped.
Interrupted/failed operations report a next step without upstream bodies,
credentials or stack traces. Successful download alone is not reported as a
ready model. Ollama HTTP, lifecycle, auth, fitting and UI behavior are tested
with synthetic hardware and mocked HTTP; tests never download models.

Without a running Ollama, the same explicit setup/desktop step offers Path B.
`engine_downloads.py` pins llama.cpp b12387 release assets with GitHub SHA-256
metadata and curated GGUFs by repository revision, LFS hash and exact length.
It keeps resumable `.part` files, validates Range responses, restarts safely
when Range is ignored, and promotes only verified downloads. Verified archives
are extracted into a staging directory with traversal/link/expansion checks;
the executable and its shared libraries are published together under
`KOVA_HOME/engine/`. Weights live under `KOVA_HOME/models/`.

`managed_engine.py` owns one engine per home, guarded by both an async lock and
a cross-process file lock. It binds llama-server to a free loopback port,
uses a private API key, fixes one inference slot and a bounded 4096-token
context (2048 for tight fits), and disables automatic fit changes. Full GPU
offload is requested only after the VRAM reserve check; readiness additionally
requires healthy status, authenticated model listing and a full GPU-layer loading log entry.
Explicit CPU mode passes zero GPU layers. Providers are saved only after that
readiness check; failed/cancelled starts are stopped and never registered.

`engine.pid`, `engine.log` and an ownership record allow crash recovery without
signalling a recycled PID: executable, full argv and process birth time must
match. Server startup/shutdown and `kova stop` stop owned engines, never Ollama
or another model service. `ModelClient.stream` takes a supervisor lease before
constructing request URLs, so the first real chat after restart/idle unload
reloads the engine and updates the live provider's dynamic loopback port.
Health probes never download or load a model. Active streams prevent removal,
switching and idle unload. A server task checks `[local] idle_unload_minutes`
(default 5, 0 disables) every 15 seconds. Removing a connection through the
local-model API, desktop Remove or `kova model remove-local` deletes its GGUF,
unless another managed variant references it; engine bundles stay cached.
Both paths share the existing authenticated SSE and lifecycle-operation lock.
All download and process tests are offline, using tiny synthetic artifacts,
HTTP transports and mocked process boundaries; real inference is not verified
by those tests.

### Privacy ledger and Local only

`egress.py` provides shared synchronous/asynchronous HTTP clients. Every Kova-owned
HTTP client (model adapters, web tools, provider catalog/auth, MCP HTTP, extensions,
engine/model downloads and diagnostics) checks each request and redirect before the
underlying transport can resolve or connect. Environment proxies are disabled.
Context-local attribution follows async tasks and copied worker threads; no global
HTTP monkeypatch is used. Provider credentials are resolved only after the model's
initial destination check. OAuth callbacks retain the same policy context.

The SQLite `egress` table contains only hosts, resolved public fetch IPs, kind,
provider/model identifiers, chat IDs, timestamps, HTTP body sizes, completion status
and provider-reported token counts (null when unknown). No paths, queries, headers,
message bodies, results or auth values are recorded. Outgoing bytes describe prepared
HTTP bodies, not proof of delivery on a failed request; incoming bytes count consumed
body data. Headers, query strings and TLS overhead are not counted. Incognito rows
route to the existing RAM store and are excluded from persistent reports.
`GET /privacy?days=30&chat_id=…` gives complete grouped totals and the newest 1000
requests; the app's Privacy panel (Settings → Privacy) and `kova privacy --days 30 --json` share it.
An `egress` SSE event before `done` summarizes the current turn's requests.

`PATCH /privacy` and `kova privacy --local-only on|off` persist the boolean
`[privacy] local_only` immediately. Only literal loopback IPs and the exact hostname
`localhost` are trusted, never DNS resolution or a LAN name. Cloud turns return 409
before saving the user's message, probes cannot contact cloud endpoints, and browser
provider sign-in is disabled. Enabling stops active turns/jobs, disconnects MCP and
withholds web, shell and MCP tools. Scheduled shell jobs are refused at execution; model downloads (including pulls delegated to
Ollama) and the self-update installer are refused too. Policy saves share the auth
refresh lock, so a refresh already in flight cannot undo the persisted switch.
Installed extensions stay on disk; reconnect MCP explicitly after turning the policy
off. This is a policy for Kova-owned traffic, not an OS firewall. Shell/stdio-plugin
traffic from before enabling it is not observable and is not included in the ledger;
other applications are outside its scope. Historical traffic is never reconstructed.

### Inline file diffs

Successful `write_file` and `edit_file` SSE tool results may carry `file_change`:
a bounded checkpoint preview (`path`, `status`, `before`, `after`, `diff`,
`truncated`). It compares the first pre-write checkpoint image to the current
contents, so repeated writes show changes **so far this turn**, not just the
last operation. It is presentation metadata, not model context or a new file
read endpoint. Existing tool and phone capability checks still apply. No new
route or restore permission is introduced.

The web client renders this in a collapsed per-file tool card with old/new line
numbers; saved change sets reuse the same `diff.js` renderer through the existing
checkpoint diff route above. `diff.css` uses semantic status colours. Source and
paths are inserted only as text. Per-hunk accept/undo, word highlights and a
side-panel review are deferred; existing whole-checkpoint restore is unchanged.
