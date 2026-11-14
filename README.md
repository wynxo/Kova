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

Then connect a provider and chat:

```sh
kova setup
kova
```

The installer adds the `kova` command using [uv](https://docs.astral.sh/uv/) (installed automatically if needed). It also installs the desktop app: `kova-app` opens the Electron window and `kova-web` opens the same chats in your browser. On Linux/macOS, follow its printed PATH line or add it to your shell profile. On Windows it updates your user PATH and adds Kova to Settings → Apps. No clone needed; installs currently come from GitHub as **kova-ai**, not PyPI.

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

`./install` (`install.bat`) installs the `kova`, `kova-app` and `kova-web` commands from the checkout.

## Features

Kova runs on your computer. Your chat history and memory stay on your machine; messages and tool results are sent to the model provider you choose. Local models can keep inference on your own hardware. Hosted providers have their own pricing, privacy policies and usage limits. It can search the web, work with files, set reminders that fire even when the CLI is closed, and remember what it learns about you across chats. Chat in the terminal or the desktop app, both connected to the same local server.

- Two modes per chat: **work** (default) with every tool, and **chat** for plain conversation with nothing but web search
- Live Markdown replies with syntax-highlighted code, compact tool status, and collapsible reasoning
- Remembers facts about you and brings the relevant ones into every chat
- Web search and page reading, with a per-chat on/off toggle
- Reads, searches and atomically edits project files in a per-chat working folder (`~/Kova Files` by default)
- Runs local commands inside an OS sandbox when the platform supports one, with approval controls, bounded output and Stop support
- Reviews what each turn changed and restores it from local checkpoints, without touching Git
- Streams a checklist for multi-step work
- Reminders that pop in while you chat, with desktop notifications
- Memory notes as plain Markdown, with an Obsidian mode
- Connect API-key providers, a ChatGPT subscription, an installed login, or your own model server

## Desktop app

`kova-app` opens your chats in the desktop app: an Electron window over the same
local server as the CLI. It installs its Node dependencies with `npm ci` on first
run, so it needs Node.js 22+ and npm; if they are missing it says so and opens the
web UI instead. `kova-web` opens that same UI in your default browser
(`kova-app --browser` is the older spelling of the same thing). From a checkout,
`./desktop-app` (`desktop-app.bat` on Windows) runs exactly what `kova-app` does.

The installer keeps a copy of the desktop app in `~/.kova/desktop` (or your
`KOVA_HOME`), so the commands work from a plain install with no checkout. On Linux,
`./install.sh --desktop` also registers Kova in your application menu.

The Electron shell starts the server when it is down, reads the local token, and
exchanges it for a one-use ticket; its caches and remembered window bounds live
under `KOVA_HOME`. Closing the window hides it to the tray. Packaged builds
(AppImage/deb, NSIS, DMG) are configured in [desktop/README.md](desktop/README.md);
they are built per platform and need the Kova Python CLI installed separately.

The responsive UI has streaming Markdown, code highlighting, collapsible thinking,
live tool status, approval cards, plan checklists, working-folder and permission
controls, chat history/search, modes, web search, model selection, Stop,
queue-or-steer while a reply runs, per-turn change review, reminder popups, and
dark/light themes. The UI loads even when the model is offline;
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

## Setup / Providers

Run `kova setup`, choose a provider, connect it, and select one or more models.
Choose the active model at the end. Arrow keys work in interactive terminals;
numbered prompts work in simpler terminals. Long catalogs can be filtered by name.
Setup **only reads model catalogs**: it never sends a test chat, scans localhost,
or searches your LAN. Run setup again to add another connection without replacing
existing ones. Memory keeps its current location; `kova memory use` changes it.

| Connection | What it does |
| --- | --- |
| **API key** | OpenAI, Anthropic, Google Gemini, OpenRouter, DeepSeek, Groq, Mistral or xAI. Setup links to the provider's key page and asks for a hidden key. Keys are checked with model listing; OpenRouter also needs a read-only key-info check because its catalog is public. |
| **ChatGPT subscription** | Continue with ChatGPT in your browser using OAuth with PKCE. Kova verifies the account token, obtains your explicit ChatGPT-plan permission, and uses the public Responses endpoint. Availability and limits depend on your account and the sign-in preview. This is separate from an OpenAI API key. |
| **Installed Codex login** | With your consent, read this machine's `~/.codex/auth.json` on each request. Kova never copies, changes, refreshes or revokes that login. If it expires, sign in again in the installed CLI. Only ChatGPT-token logins are supported, not API-key-only logins. |
| **Local server** | Type an address such as `homelab:8080`, `192.168.178.29:11434` or `http://localhost:1234/v1`. Only that address is contacted. Loose hostnames can try common ports on that named host; there is no discovery scan. |
| **Custom provider** | Give any OpenAI-compatible base URL (including its API path), a connection name, optional key and optional JSON headers. Nonstandard paths are preserved. |

The desktop sidebar has **Add provider** with key/custom/local setup, browser
sign-in and consented installed-login reuse. Its model menu groups choices by
provider. Terminal chat's `/model` picker has the same groups and type-to-filter.

```sh
kova providers                     # connected providers, never credentials
kova providers add                 # provider wizard
kova providers add openai          # start with a provider type
kova providers login CONNECTION    # sign in again / replace a key
kova providers logout CONNECTION   # forget credentials; keep selected models
kova providers remove CONNECTION   # remove connection and its selected models
kova model list                    # selected models, grouped by provider
kova model use CONNECTION/MODEL_ID
```

Existing `[[models]]` configs still load with the same names and active model.
The next save writes schema version 2 with `[[providers]]`; matching legacy
URL/key pairs share one provider. The old command still works:

```sh
kova model add home homelab:8080 MODEL_ID
kova model use home
kova setup --url http://192.168.178.29:11434 --model MODEL_ID --name home
```

Keys, custom headers and owned OAuth tokens are stored only in
`~/.kova/config.toml`, written atomically with **0600** permissions on Unix. Set `compact_threshold = 0.75` to change the fraction of model context that triggers automatic conversation compaction. Use `/compact` in the TUI to summarize older turns manually; the original transcript remains available.
There is no extra keyring dependency. Avoid putting keys directly in shell
history; interactive setup hides input. Owned tokens refresh near expiry under
a cross-process lock. Logging out attempts to revoke the owned renewable session;
Kova reports if remote revocation could not be confirmed. Borrowed logins are
only disconnected locally. Exports omit all keys, headers and OAuth data.

Kova uses OpenAI-compatible endpoints where available, Anthropic's native
Messages API for its tool-call format, and a Responses adapter for subscription
connections. There is no provider-specific logic in the agent's tool loop.

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
- `kova setup` — connect a provider and select models, without a test chat or scan.
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
- `/steer <text>` — send a message into the reply that is running now
- `/changes` — review the files each turn changed; `/diff [n]` previews one change set; `/restore <n>` puts the files back
- `/clear` — clear the screen without resetting the conversation
- `/help` — list chat commands
- `/exit` or `/quit` — leave the chat

Use `/resume` to pick an earlier chat (its picker marks the current one, and **Ctrl+S** forks the highlighted chat and switches to the copy), or type `/fork` to copy the current chat, or `/fork 2` to copy the second one in `/resume`'s list. Forks include messages and compacted context, and the original is left untouched.

The first setup asks you to name your assistant and choose a tone (warm, concise, professional or playful). Desktop and web also ask on first connection. The name and tone are saved in `~/.kova/config.toml` (or `$KOVA_HOME/config.toml`) under `[persona]` and used wherever your assistant appears: its replies' headers and the chat's welcome card, the desktop window title and tray menu, and how it speaks. Restarting the desktop refreshes its tray menu.

Each chat start shows the welcome card plus one short tip, rotating through keyboard and command tips a run at a time, so you learn a little at a time instead of reading a wall of keys. `?` still shows every shortcut at once.

For a light terminal background, set `theme = "light"` under `[tui]` in that file. The default is `"dark"`; colours still adapt to terminal colour depth and `NO_COLOR`. Bindings can be changed under `[tui.keys]`, using terminal key names; for example, to swap the palette and reasoning keys:

```toml
[tui]
theme = "light"

[tui.keys]
palette = "ctrl-t"
thinking = "ctrl-p"
```

Bindings must be unique, so swapping two keys requires both entries. Actions include `shortcuts`, `palette`, `thinking`, `tools`, `scroll_up`, `scroll_down`, `cancel`, `interrupt`, `exit`, `permissions`, `clear`, `suspend`, `up`, `down`, `complete`, `submit`, `newline`, and editor actions such as `left` and `backspace`. Supported key names are the default action keys and `ctrl-g`, `ctrl-r`, `ctrl-v`, and `ctrl-x`. Typed command prefixes (`/`, `!`, `#`, `@`) are not keybindings.

While a reply is running, **Enter** queues your message: it is saved on the server, shown as *queued*, and starts the next turn when the current one finishes. **/steer** instead joins the reply in progress at its next round, so it can change the work rather than wait behind it. Both are labelled in the transcript, and a queued message survives quitting and reopening the chat.

Use **↑** for this session's input history and **Tab** to complete slash commands. **Ctrl+C** stops the current reply and returns to the prompt (or clears unfinished input); **Ctrl+D** leaves. Piped input uses plain input without terminal animations.

Memory:

- `kova memory list` — list your notes
- `kova memory search <query>` — search them
- `kova memory show <slug>` — print one note
- `kova memory add` — add a note
- `kova memory rm <slug>` — delete a note
- `kova memory use <path> [--obsidian]` — switch the memory folder immediately, even while the server is running

Models:

- `kova model list` — selected models grouped by provider
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

- `run_command`: runs a shell command **in this chat's working folder**, as your current user. It returns combined stdout/stderr and the exit code, keeps at most **32 KB** of output, and has a **60-second** default timeout (**300 seconds** maximum). No interactive stdin or automatic elevation. Uses Bash/sh on Linux/macOS, PowerShell on Windows (cmd fallback). Stop and timeout kill the process tree; changes already made are not undone. On Linux with Landlock (kernel 5.13+), commands run in a kernel sandbox that may create, change, rename or delete files only inside the working folder and temporary space; reads and program execution are unrestricted. If the kernel cannot provide the sandbox (or `KOVA_SANDBOX=off` disables it), Kova asks instead of running the command unconfined. Full access runs commands without the sandbox.
- `read_file`, `list_files`, `search_files`: read text, list project files, or search with ripgrep (Python fallback). Reads keep the original plain-text result, capped at 100 KB; optional `start_line`/`end_line` return numbered lines. Listings/search skip dependency and build folders, and searches don't follow symlinks.
- `edit_file`: replaces exact text only when it matches once. `write_file`: creates or overwrites UTF-8 text, creating parent folders as needed. Both save atomically; file modes are preserved. Files changed since a prior read, during approval, or during saving are rejected so you can read them again.
- `ask_user`: asks a clarifying question with optional choices and waits for an answer in the web UI or full-screen terminal. Noninteractive clients skip the question instead of hanging.
- `update_plan`: publishes up to eight checklist items with `pending`, `in_progress`, or `done` status. Plans stream to clients and the latest checklist is saved per chat and shown when the conversation is reopened. The web sidebar and terminal show whether a conversation is working, waiting for approval or an answer, or done.
- Existing web, memory, and reminder tools remain available.

File tools resolve relative paths against the **working folder** and reject paths or symlinks that escape it, even in Full access mode. Existing file-tool names are unchanged; chats default to the configured Kova files folder (`~/Kova Files`). Set an existing absolute folder using the desktop composer or `PATCH /chats/{id}` with `{"cwd": "/absolute/project"}`. Commands are not a filesystem sandbox: they can access anything your user can access.

In work mode, `KOVA.md` and `AGENTS.md` are discovered at three levels: your Kova home folder (preferences for every chat), the nearest project root above the working folder (a folder with `.git`, so a chat inside `src/` still sees the repository's rules), and the working folder itself. A subdirectory's guidance is loaded only when a chat works there; Kova never scans the whole tree. Later, more specific sections take precedence, and the whole block is capped at **16 KiB** of UTF-8, keeping the most specific guidance when the budget is tight. None of it can override your request or Kova's permissions. Chat mode neither offers nor executes commands, file tools, or plan tools, and does not load project instructions.

## Reviewing changes

After a turn that changed files, Kova records a **change set**: the unified diff for each path, with line counts and a preview of both sides. `/changes` lists them, `/diff <n>` shows one, and `/restore <n>` puts the working folder back to that point (creating, restoring or removing only the paths those turns touched), after a confirmation. The web UI shows the same card after each turn, with expandable diffs and a Restore button.

Checkpoints are local and independent of Git: file contents live compressed under `KOVA_HOME/checkpoints`, outside the working folder, and Kova never reads or writes your repository's Git state. Files above **8 MB** are listed as skipped rather than copied. Only Kova's own file tools are checkpointed; files a shell command changes are not.

## Permissions

Each chat has a saved `permission` setting, selectable in the desktop composer or through the chat create/update API:

| Mode | Commands | File writes/edits |
| --- | --- | --- |
| **manual** (default) | Sandboxed normal commands run; destructive or unconfined commands need approval | Approve outside the configured Kova files folder |
| **auto** | Sandboxed normal commands run; destructive or unconfined commands need approval | Run inside the working folder |
| **full** | Never ask, including destructive commands, and no sandbox | Never ask, still folder-contained |

Scheduled shell jobs (`job_add` with `command`) run unconfined as your user,
not inside the chat command sandbox. Scheduling one requires approval in Manual
and Auto even when Landlock is available; Full access skips that prompt. The
approval authorizes the recurring command. Prompt-only jobs do not require shell
approval. Existing jobs are not re-approved by this scheduling check.

Reads, listings, searches, and plans never ask. A sandboxed command that cannot be confined (no Landlock, or `KOVA_SANDBOX=off`) asks in every mode except Full, so a missing sandbox never becomes an unconfined auto-run. The destructive-command classifier still gates obvious data loss inside the working folder — deletion, disk operations, pipe-to-shell, package removal, elevation and discarding Git work, including Windows equivalents — even when the write itself would be contained.

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

Approvals and `ask_user` questions still waiting in a chat can be listed at any time, so a reloaded page or a second window can rebuild the card and answer it. Each item is the exact event the stream sent:

```http
GET /chats/{chat_id}/approvals
```

An additive `approval_resolved` event carries `id`, `decision`, and `message` so clients can retire cards on answers or timeout. Stop ends the stream with the existing `stopped` event.

While a turn runs, two endpoints decide where a new message goes:

```http
POST /chats/{chat_id}/queue        {"text": "also update the docs"}
POST /chats/{chat_id}/steer        {"text": "use the other file"}
```

`GET /chats/{chat_id}/queue` lists queued messages oldest first; `DELETE /chats/{chat_id}/queue/{id}` removes one; `POST /chats/{chat_id}/queue/next` takes the oldest so a client can send it as the next turn. Queued messages are stored in the chat database and survive restarts. Steering requires a running turn (otherwise **409**) and delivers the message to that turn at its next model round, followed by a `{"type":"steer","text":...}` event; a steer that arrives after the last round is moved to the queue rather than lost. `KovaClient` exposes `queue_message`, `queued_messages`, `delete_queued_message`, `next_queued_message` and `steer_message`. Chat create/get/list/update responses include `permission` and `cwd`; get/create also include the latest `plan`. `PATCH /chats/{id}` accepts `permission` and `cwd` alongside `title`, `mode`, and `web`. `KovaClient.answer_approval(...)` answers approvals; `create_chat` and `update_chat` accept both new settings. Approvals and session allowances are server memory only; chat settings and the latest plan survive restart.

## Memory and Obsidian mode

Kova's memory is plain Markdown files with a little YAML frontmatter. As you chat, it saves lasting facts about you as notes, searches them, and brings the best matches into the conversation.

You can point it at an Obsidian vault:

```bash
kova memory use <vault> --obsidian
```

In this mode Kova reads your whole vault when searching, but only ever writes inside `<vault>/Kova/`. Your own notes are never touched.

## Where your data lives

Everything Kova keeps — config, the chat database, memory notes — lives in `~/.kova/`. Set `KOVA_HOME` to put it somewhere else. Kova runs no cloud service and requires no Kova account. Hosted model connections use the provider account or key you choose.

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

### Electron desktop app

`kova-app` runs the Electron app in `desktop/` (from a checkout, `./desktop-app`;
Windows: `desktop-app.bat`) with Node.js/npm installed. It runs `npm ci` there on
first use; without Node/npm it says so and opens the web UI with `kova-web`.
The installers keep a copy of `desktop/` in `~/.kova/desktop`, so `kova-app` works
without a checkout; Linux can register the menu entry with `./install.sh --desktop`.
AppImage/deb, NSIS and DMG builds are configured in [desktop/README.md](desktop/README.md).
Packaged Electron builds require the Kova Python CLI installed separately.
