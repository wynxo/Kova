# Kova

Your own private AI that lives on your computer.

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
kova chat
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

## Features

Kova is an assistant you run yourself, so it's private, free, and has no usage limits. Your chats and its memory of you stay on your machine, not on someone else's server. It can search the web, work with files, set reminders that fire even when the CLI is closed, and remember what it learns about you across chats. For now you talk to it through a command line; a desktop app and a phone/web UI are coming soon.

- Live Markdown replies with syntax-highlighted code, compact tool status, and collapsible reasoning
- Remembers facts about you and brings the relevant ones into every chat
- Web search and page reading
- Reads and writes files in its own folder (`~/Kova Files` by default)
- Reminders that pop in while you chat, with desktop notifications
- Memory notes as plain Markdown, with an Obsidian mode
- Works with any OpenAI-compatible model server

## Model setup

`kova setup` finds a local model server, tests it, and saves it. For another machine: `kova setup --url http://192.168.1.20:8080 --model MODEL` (add `--key KEY` if needed).

Kova works with OpenAI-compatible servers, including llama.cpp / PolyStrata, Ollama (`http://localhost:11434/v1`), and LM Studio (`http://localhost:1234/v1`). You can also add one manually with `kova model add NAME URL MODEL` then `kova model use NAME`.

## Usage

The CLI starts the server for you in the background, so normally you don't have to think about it.

- `kova chat [--new | --chat ID]` — interactive chat with a compact model/server/memory header, live Markdown replies, and tool status lines. `--new` starts fresh, `--chat ID` continues an old chat, and `--no-think` hides reasoning and its timing until you enable `/think`.
- `kova ask "question"` — one question, one answer, printed to stdout. Good for scripts:
  - Pipe text in: `git diff | kova ask "review this"`, or `cat question.txt | kova ask` to use the input as the question. Input over 100 KB is cut off with a warning on stderr.
  - `-f/--file PATH` attaches a file (repeatable); the model sees its name.
  - `-m/--model NAME` uses another configured model for this question only.
  - `--json` prints `{"answer", "chat_id", "model", "tools"}`.
  - `--chat ID` continues a chat; otherwise each ask gets its own chat titled `[ask] …`.
  - `--think` shows the reasoning on stderr, so stdout stays clean.
  - Exit code 0 on success, 1 on a model or server error (message on stderr).
- `kova setup` — find a model server, test it, and pick a memory folder.
- `kova chats search <words>` — search inside your chats; results show the chat number, title, when, and a snippet with the match highlighted.
- `kova chats export <#|id> [--md|--json]` — print one chat to stdout, as Markdown (default) or JSON.
- `kova export [PATH]` — write one zip backup (default `~/Kova-backup-YYYY-MM-DD.zip`) with chats as JSON and Markdown, memory notes, reminders, and `config.toml` without API keys; the server token is never included.
- `kova import PATH [--merge|--replace]` — restore a backup. Merge is the default and skips chats/notes that already exist; `--replace` asks first, then replaces chats, Kova's memory notes, and reminders.

Inside `kova chat`:

- `/new` — start a fresh chat
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

## Memory and Obsidian mode

Kova's memory is plain Markdown files with a little YAML frontmatter. As you chat, it saves lasting facts about you as notes, searches them, and brings the best matches into the conversation.

You can point it at an Obsidian vault:

```bash
kova memory use <vault> --obsidian
```

In this mode Kova reads your whole vault when searching, but only ever writes inside `<vault>/Kova/`. Your own notes are never touched.

## Where your data lives

Everything Kova keeps — config, the chat database, memory notes — lives in `~/.kova/`. Set `KOVA_HOME` to put it somewhere else. There is no cloud service and no account.

The server only listens on `127.0.0.1`, so nothing outside your machine can reach it, and every request needs a token that's generated on your machine on first run. 🔒

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
