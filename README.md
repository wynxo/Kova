# Kova

Your own private AI that lives on your computer.

Want to work on it? Read [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md), then [CONTRIBUTING.md](CONTRIBUTING.md) to run it from source and make it yours.

## Install

Linux / macOS (as your normal user, no sudo):

```sh
curl -fsSL https://raw.githubusercontent.com/wynxo/Kova/main/install.sh | sh
```

Windows (PowerShell, not Administrator):

```powershell
powershell -ExecutionPolicy ByPass -c "irm https://raw.githubusercontent.com/wynxo/Kova/main/install.ps1 | iex"
```

Then set up a model server and chat:

```sh
kova setup
kova
```

The installer adds the `kova` command using [uv](https://docs.astral.sh/uv/) (installed automatically if needed). On Linux/macOS, follow its printed PATH line or add it to your shell profile. On Windows it updates your user PATH and adds Kova to Settings → Apps. No clone needed; installs currently come from GitHub as **kova-ai**, not PyPI.

Update: `kova update` (or rerun the installer). Check first: `kova update --check`. Pin a tag/commit with `kova update --ref v0.2.0`, or `KOVA_REF=v0.2.0` for the installer (`$env:KOVA_REF='v0.2.0'` on Windows). Tags shown here are examples, not published releases.

Uninstall: `kova uninstall` (asks before deleting data), `kova uninstall --keep-data`, or `kova uninstall --purge`. Without the command:

```sh
curl -fsSL https://raw.githubusercontent.com/wynxo/Kova/main/uninstall.sh | sh
```

```powershell
powershell -ExecutionPolicy ByPass -c "irm https://raw.githubusercontent.com/wynxo/Kova/main/uninstall.ps1 | iex"
```

Piped/noninteractive uninstalls keep data by default; to purge, use `sh -s -- --purge` on the shell pipeline, or download `uninstall.ps1` and run it with `--purge` on Windows. uv and external memory folders/files are kept.

## Update, reinstall, uninstall

| Do this | Installed command | From a checkout |
| --- | --- | --- |
| Update | `kova update` | `./update` (`update.bat`) pulls, refreshes dependencies, and updates the installed command too |
| Reinstall | | `./reinstall` (`reinstall.bat`) removes the installed command and installs again from the checkout; chats, memory, and settings are kept |
| Uninstall | `kova uninstall` | `./uninstall` (`uninstall.bat`); add `--keep-data` or `--purge` |

`./install` (`install.bat`) installs the `kova` command from the checkout.

## Features

Kova is an assistant you run yourself, so it's private, free, and has no usage limits. Your chats and its memory of you stay on your machine, not on someone else's server. It can search the web, work with files, set reminders that fire even when the CLI is closed, and remember what it learns about you across chats. Chat in the terminal or the desktop app, both connected to the same local server.

- Two modes per chat: **work** (default) with every tool, and **chat** for plain conversation with nothing but web search
- Live Markdown replies with syntax-highlighted code, compact tool status, and collapsible reasoning
- Remembers facts about you and brings the relevant ones into every chat
- Web search and page reading, with a per-chat on/off toggle
- Reads, searches and atomically edits project files in a per-chat working folder (`~/Kova Files` by default)
- Runs local commands with approval controls, bounded output and Stop support
- Streams a checklist for multi-step work
- Reminders that pop in while you chat, with desktop notifications
- Memory notes as plain Markdown, with an Obsidian mode
- Works with any OpenAI-compatible model server

## Desktop app

Run `kova-app` (or `./desktop-app` from a checkout; `desktop-app.bat` on Windows).
It starts the same local server as the CLI and opens your chats in a native window
when **pywebview** is installed, otherwise in your default browser. Force the browser
with `kova-app --browser`. No Node, CDN, account, or token pasting is needed.

Native windows are optional: from a checkout, run `uv sync --extra app`, then
`./desktop-app`. For a tool install, use
`uv tool install --force 'kova-ai[app] @ git+https://github.com/wynxo/Kova.git'`.
The `app` extra keeps GUI dependencies out of the base install; Linux also needs a
working [GTK or Qt webview backend](https://pywebview.flowrl.com/guide/installation).
If the backend is unavailable, Kova falls back to your browser.

The responsive UI has streaming Markdown, code highlighting, collapsible thinking,
live tool status, approval cards, plan checklists, working-folder and permission
controls, chat history/search, modes, web search, model selection, Stop,
reminder popups, and dark/light themes. The UI loads even when the model is offline;
run `kova setup` or start your model server to chat.

The launcher trades the local server token for a **single-use, 60-second ticket**
in the URL fragment, which never goes into HTTP access logs. The page removes it
and exchanges it for an **HttpOnly, SameSite=Strict cookie** valid for 12 hours.
Browser API calls enforce a loopback host and same origin (including port).
Disconnect revokes the cookie; restarting the server revokes all sessions.
The server still binds only to `127.0.0.1`; phone/network access is not enabled.
Stop cancels the model turn, denies pending approvals, and kills running commands
and their child processes. It cannot undo changes already made or a synchronous
file operation that has already started.

## Model setup

`kova setup` finds a local model server, tests it, and saves it. No server on this computer? Type its address the way you think of it (`192.168.1.50`, `homelab:8080`, `openrouter`) and Kova tries the usual ports, or type `scan` to search your network (only when you ask). `kova model add NAME ADDRESS [MODEL]` takes the same loose addresses and picks the model if the server has only one. For another machine: `kova setup --url http://192.168.1.20:8080 --model MODEL` (add `--key KEY` if needed).

Kova works with OpenAI-compatible servers, including llama.cpp / PolyStrata, Ollama (`http://localhost:11434/v1`), and LM Studio (`http://localhost:1234/v1`). You can also add one manually with `kova model add NAME URL MODEL` then `kova model use NAME`.

## Usage

The CLI starts the server for you in the background, so normally you don't have to think about it.

- `kova [--new | --chat ID]` — just type `kova` to chat: interactive chat with a compact model/server/memory header, live Markdown replies, and tool status lines. `--new` starts fresh, `--chat ID` continues an old chat, and `--no-think` hides reasoning and its timing until you enable `/think`.
  - `--mode chat|work` starts the chat in that mode (default work); `--no-web` turns web search off for it.
- `kova ask "question"` — one question, one answer, printed to stdout. Good for scripts:
  - Pipe text in: `git diff | kova ask "review this"`, or `cat question.txt | kova ask` to use the input as the question. Input over 100 KB is cut off with a warning on stderr.
  - `-f/--file PATH` attaches a file (repeatable); the model sees its name.
  - `-m/--model NAME` uses another configured model for this question only.
  - `--mode chat|work` runs the question in that mode (default work).
  - `--json` prints `{"answer", "chat_id", "model", "tools"}`.
  - `--chat ID` continues a chat; otherwise each ask gets its own chat titled `[ask] …`.
  - `--think` shows the reasoning on stderr, so stdout stays clean.
  - Exit code 0 on success, 1 on a model or server error (message on stderr).
- `kova setup` — find a model server, test it, and pick a memory folder.
- `kova chats search <words>` — search inside your chats; results show the chat number, title, when, and a snippet with the match highlighted.
- `kova chats export <#|id> [--md|--json]` — print one chat to stdout, as Markdown (default) or JSON.
- `kova export [PATH]` — write one zip backup (default `~/Kova-backup-YYYY-MM-DD.zip`) with chats as JSON and Markdown, memory notes, reminders, and `config.toml` without API keys; the server token is never included.
- `kova import PATH [--merge|--replace]` — restore a backup. Merge is the default and skips chats/notes that already exist; `--replace` asks first, then replaces chats, Kova's memory notes, and reminders.

Inside a chat:

- `/new` — start a fresh chat
- `/mode` — show the chat's mode; `/mode chat` or `/mode work` switches it
- `/web` — show whether web search is on; `/web on` or `/web off` toggles it
- `/model` — list models and mark the active one; `/model <name>` switches models
- `/think` — toggle full reasoning; by default you see a spinner, then a compact elapsed-time summary
- `/clear` — clear the screen without resetting the conversation
- `/help` — list chat commands
- `/exit` or `/quit` — leave the chat

Use **↑** for this session's input history and **Tab** to complete slash commands. **Ctrl+C** stops the current reply and returns to the prompt (or clears unfinished input); **Ctrl+D** leaves. Piped input uses plain input without terminal animations.

Memory:

- `kova memory list` — list your notes
- `kova memory search <query>` — search them
- `kova memory show <slug>` — print one note
- `kova memory add` — add a note
- `kova memory rm <slug>` — delete a note
- `kova memory use <path> [--obsidian]` — switch the memory folder immediately, even while the server is running

Models:

- `kova model list` — configured models
- `kova model add NAME URL MODEL [--key KEY]` — add one
- `kova model use NAME` — set the active one

Reminders:

- `kova reminders list` — upcoming reminders
- `kova reminders add "<when>" "<text>"` — add one; `when` can be natural local time (`in 10 min`, `tomorrow 5pm`, `monday 9am`, `morgen 9 Uhr`) or ISO 8601
- `kova reminders rm ID` — delete one

Server:

- `kova serve` — run the server in the foreground
- `kova stop` — stop it
- `kova status` — is it up, which model is active, is the model reachable

Missing notes, reminders, or models and server connection failures print a red, one-line error and exit with code 1, without a traceback.

## Chat modes

Every chat runs in one of two modes, shown in the chat header and in `kova chats`:

- **work** (default) — all of Kova's tools: commands, project files, plans, web search, reminders and memory.
- **chat** — plain conversation. No tools except web search (and `/web off` turns that off too), and no saving new memories — though what Kova already remembers about you is still part of the conversation.

Switch with `/mode` inside a chat, or start one with `kova --mode chat`. `kova ask --mode chat` does the same for one-off questions.

## Tools

Work mode includes:

- `run_command`: runs a shell command **in this chat's working folder**, as your current user. It returns combined stdout/stderr and the exit code, keeps at most **32 KB** of output, and has a **60-second** default timeout (**300 seconds** maximum). No interactive stdin or automatic elevation. Uses Bash/sh on Linux/macOS, PowerShell on Windows (cmd fallback). Stop and timeout kill the process tree; changes already made are not undone.
- `read_file`, `list_files`, `search_files`: read text, list project files, or search with ripgrep (Python fallback). Reads keep the original plain-text result, capped at 100 KB; optional `start_line`/`end_line` return numbered lines. Listings/search skip dependency and build folders, and searches don't follow symlinks.
- `edit_file`: replaces exact text only when it matches once. `write_file`: creates or overwrites UTF-8 text, creating parent folders as needed. Both save atomically; file modes are preserved. Files changed since a prior read, during approval, or during saving are rejected so you can read them again.
- `update_plan`: publishes up to eight checklist items with `pending`, `in_progress`, or `done` status. Plans stream to clients and the latest checklist is saved per chat.
- Existing web, memory, and reminder tools remain available.

File tools resolve relative paths against the **working folder** and reject paths or symlinks that escape it, even in Full access mode. Existing file-tool names are unchanged; chats default to the configured Kova files folder (`~/Kova Files`). Set an existing absolute folder using the desktop composer or `PATCH /chats/{id}` with `{"cwd": "/absolute/project"}`. Commands are not a filesystem sandbox: they can access anything your user can access.

In work mode, `KOVA.md` and `AGENTS.md` from that folder are included as project guidance in the system prompt, capped at **4,096 characters total**. They cannot override permissions. Chat mode neither offers nor executes commands, file tools, or plan tools, and does not load project instructions.

## Permissions

Each chat has a saved `permission` setting, selectable in the desktop composer or through the chat create/update API:

| Mode | Commands | File writes/edits |
| --- | --- | --- |
| **manual** (default) | Approve every command | Approve outside the configured Kova files folder |
| **auto** | Run normal commands; approve destructive ones | Run inside the working folder |
| **full** | Never ask, including destructive commands | Never ask, still folder-contained |

Reads, listings, searches, and plans never ask. The destructive-command classifier flags deletion, disk operations, pipe-to-shell, package removal, elevation, discarding Git work and other destructive actions, including Windows equivalents. This is a heuristic, not a shell security boundary; Manual keeps every command behind approval.

Approvals appear inline with a summary, risk badge, optional guidance, and **Allow**, **Allow for this chat**, or **Deny**. A session allowance lasts only for that chat while this server runs; destructive commands always need individual approval unless the chat is in Full access. For a destructive call, an API `allow_session` answer allows that single call only and never creates a session allowance. Changing the folder or permission mode clears a session allowance. Stop denies pending requests; unanswered requests are denied after **10 minutes**. Denial and optional guidance are returned to the model. Noninteractive `kova ask` denies approval-requiring actions immediately and prints a hint to continue in interactive chat, so scripts do not hang waiting for a prompt. Settings cannot change during an active turn: stop it first.

### Client protocol

The existing `POST /chats/{id}/messages` SSE stream now also emits:

```json
{"type":"approval","id":"approval-id","tool":"run_command","args":{"command":"git status"},"risk":"normal","summary":"git status"}
{"type":"plan","items":[{"text":"Inspect the project","status":"in_progress"}]}
```

The server pauses that call until another authenticated request answers it:

```http
POST /chats/{chat_id}/approvals/{approval_id}
Content-Type: application/json

{"decision":"allow","message":null}
```

`decision` is `allow`, `allow_session`, or `deny`. The optional `message` gives guidance on a denied action. Unknown, already answered, expired, stopped, or other-chat approval IDs return 404; malformed decisions return 422. All existing bearer/session and browser same-origin checks apply.

An additive `approval_resolved` event carries `id`, `decision`, and `message` so clients can retire cards on answers or timeout. Stop ends the stream with the existing `stopped` event. Chat create/get/list/update responses include `permission` and `cwd`; get/create also include the latest `plan`. `PATCH /chats/{id}` accepts `permission` and `cwd` alongside `title`, `mode`, and `web`. `KovaClient.answer_approval(...)` answers approvals; `create_chat` and `update_chat` accept both new settings. Approvals and session allowances are server memory only; chat settings and the latest plan survive restart.

## Memory and Obsidian mode

Kova's memory is plain Markdown files with a little YAML frontmatter. As you chat, it saves lasting facts about you as notes, searches them, and brings the best matches into the conversation.

You can point it at an Obsidian vault:

```bash
kova memory use <vault> --obsidian
```

In this mode Kova reads your whole vault when searching, but only ever writes inside `<vault>/Kova/`. Your own notes are never touched.

## Where your data lives

Everything Kova keeps — config, the chat database, memory notes — lives in `~/.kova/`. Set `KOVA_HOME` to put it somewhere else. There is no cloud service and no account.

The server only listens on `127.0.0.1`, so nothing outside your machine can reach it, and API requests need a local bearer token or an authenticated browser session. The app shell and health check are public, but contain no chat data or credentials. 🔒

## Development

From a checkout, `./cli` starts chat and `./cli status` checks the server (`cli.bat` on Windows). For an editable `kova` command, use `./install.sh --dev` (`install.bat --dev` on Windows), or `uv tool install --force --editable .`. The import package stays `kova`; the distribution is `kova-ai`.

Installer smoke tests can set `KOVA_SOURCE=/absolute/path/to/checkout` to avoid fetching GitHub. Use a disposable HOME and unset KOVA_HOME so no real install/data is touched.

```bash
uv run pytest
```

There's also a live smoke test against a real model server:

```bash
KOVA_LIVE=1 uv run pytest -m live
```

## Credits

Built for GenHacks 2026. Every AI tool and model used to build Kova is listed in [AI-TOOLS.md](AI-TOOLS.md).
