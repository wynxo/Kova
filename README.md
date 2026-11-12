# Kova

Your own private AI that lives on your computer.

Kova is an assistant you run yourself, so it's private, free, and has no usage limits. Your chats and its memory of you stay on your machine, not on someone else's server. It can search the web, work with files, set reminders that fire even when the CLI is closed, and remember what it learns about you across chats. For now you talk to it through a command line; a desktop app and a phone/web UI are coming soon.

## Quick start

Linux and macOS, from a clone of this repo:

```bash
./install.sh
kova chat
```

On Windows, run `install.bat` instead, then `kova chat`.

`install.sh` installs [uv](https://docs.astral.sh/uv/) if you don't have it, installs Kova's dependencies, and puts a `kova` command on your PATH, so `kova chat` works from any folder. Running it again is safe.

No install needed to take a look — the launcher in the repo root runs Kova straight from the checkout:

```bash
./cli            # same as: ./cli chat
./cli status
```

On Windows use `cli.bat`. To undo an install, run `./uninstall.sh` (`uninstall.bat` on Windows); it keeps your data unless you say otherwise.

Either way, Kova needs a model server before it can talk — see [Install](#install).

## Features

- Streaming chat, with the model's reasoning shown dimmed
- Remembers facts about you and brings the relevant ones into every chat
- Web search and page reading
- Reads and writes files in its own folder (`~/Kova Files` by default)
- Reminders that pop in while you chat, with desktop notifications
- Memory notes as plain Markdown, with an Obsidian mode
- Works with any OpenAI-compatible model server

## Install

You need [uv](https://docs.astral.sh/uv/).

```bash
git clone https://github.com/wynxo/Kova.git
cd Kova
uv tool install .
```

Kova needs a model to talk to. Point it at any OpenAI-compatible server:

```bash
kova model add NAME URL MODEL
kova model use NAME
```

For example, a local llama.cpp or PolyStrata server, Ollama at `http://localhost:11434/v1`, or LM Studio at `http://localhost:1234/v1`. If your server needs an API key, pass `--key KEY`.

## Usage

The CLI starts the server for you in the background, so normally you don't have to think about it.

- `kova chat [--new | --chat ID]` — interactive chat. Replies stream in, tool calls show as one-line badges, and reminders pop in while you chat. `--new` starts fresh, `--chat ID` continues an old chat, and `--no-think` hides the model's reasoning.
- `kova ask "question"` — one question, one answer, printed to stdout. Good for scripts.

Inside `kova chat`:

- `/new` — start a fresh chat
- `/model` — switch model
- `/exit` — leave the chat

Memory:

- `kova memory list` — list your notes
- `kova memory search <query>` — search them
- `kova memory show <slug>` — print one note
- `kova memory add` — add a note
- `kova memory rm <slug>` — delete a note
- `kova memory use <path>` — switch the memory folder

Models:

- `kova model list` — configured models
- `kova model add NAME URL MODEL [--key KEY]` — add one
- `kova model use NAME` — set the active one

Reminders:

- `kova reminders list` — upcoming reminders
- `kova reminders add "<when>" "<text>"` — add one; `when` is ISO 8601 local time
- `kova reminders rm ID` — delete one

Server:

- `kova serve` — run the server in the foreground
- `kova stop` — stop it
- `kova status` — is it up, which model is active, is the model reachable

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

```bash
uv run pytest
```

There's also a live smoke test against a real model server:

```bash
KOVA_LIVE=1 uv run pytest -m live
```

## Credits

Built for GenHacks 2026. Every AI tool and model used to build Kova is listed in [AI-TOOLS.md](AI-TOOLS.md).
