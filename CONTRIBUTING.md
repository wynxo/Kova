# Make Kova yours

Kova is meant to be forked. Everything runs from this checkout, the code is
plain Python, and the parts you are most likely to want to change — the
personality, the tools, the colours, the model server — are small and
self-contained. This guide gets you running and then shows the usual edits.

If you only read one other file, read [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).
It explains how the server, clients, agent loop, store and memory fit together.

## Run it from source

You need [uv](https://docs.astral.sh/uv/) and Python 3.12+ (uv can fetch
Python for you). No global install is required.

```sh
git clone https://github.com/wynxo/Kova.git
cd Kova

./cli            # terminal chat (starts the background server)
./cli status     # is the server up, which model is active
./cli doctor     # check a first run: config, sandbox, port, token, Node, disk
./desktop-app    # the desktop app (Electron); --browser opens the web UI
```

`./cli` is a small wrapper around `uv run kova`, so the first run creates
`.venv` and installs dependencies. `cli.bat` and `desktop-app.bat` are the
Windows equivalents.

The server only listens on `127.0.0.1:7700`, and the clients start it for you.
`./cli serve` runs it in the foreground when you want to see logs; `./cli stop`
stops a background one.

The desktop app needs Node.js 22+ and npm; `./desktop-app` installs its
Electron dependencies on first run. Without Node/npm it says so and opens the
web UI instead, which `./desktop-app --browser` selects directly (the installed
equivalent is `kova-web`).

If you have never picked a model, run `./cli setup`, or point Kova at a server
with the recipe below.

## Run the tests

```sh
uv run pytest -q
```

The suite uses `httpx.MockTransport` and FastAPI's `TestClient`, so it needs no
model server and no network. It finishes in seconds.

Useful variations:

```sh
uv run pytest -q tests/test_agent.py          # one file
uv run pytest -q -k reminder                  # one topic

# real model server, opt-in (in-process server, throwaway home)
KOVA_LIVE=1 KOVA_LIVE_URL=http://127.0.0.1:8080/v1 KOVA_LIVE_MODEL=MODEL \
  uv run pytest -m live
```

The live test builds its own throwaway config from those two variables, so it
never touches your real chats. Everything else is deterministic.

There is no formatter or linter configured; match the style around you (four
spaces, type hints where the file already uses them, docstrings on public
functions). The tests are the gate.

## Recipes

### Add a tool

Tools live in `src/kova/tools/`. Each module exposes `tools(ctx)` returning
`Tool` objects. Create `src/kova/tools/clock.py`:

```python
"""A tiny local-time tool."""

from __future__ import annotations

from . import Tool, ToolContext


def tools(ctx: ToolContext) -> list[Tool]:
    def local_time(args: dict) -> str:
        return ctx.now().isoformat()

    return [
        Tool(
            "local_time",
            "Return the user's current local date and time.",
            {"type": "object", "properties": {}},
            local_time,
        )
    ]
```

Register it in `build_tools` in `src/kova/tools/__init__.py`:

```python
from . import clock, files, memory, reminders, web

tools = [*web.tools(), *files.tools(ctx), *clock.tools(ctx),
         *reminders.tools(ctx), *memory.tools(ctx)]
```

That is enough. The tool appears in the model's schema (`openai_schemas`), gets
the `Available tools` line in the system prompt, and is available in work mode
automatically. In chat mode, `agent.select_tools` allows only the names in
`_WEB_TOOLS`, so a new tool stays out of plain conversation unless you add it
there on purpose.

A `run` callable may be synchronous or `async`; sync tools execute in a worker
thread. Return a string (JSON is a good choice for structured data). Raise
`ToolError` for a message the model should see; any other exception is also
turned into tool output instead of killing the turn. Results are truncated at
32,000 characters.

To test it, use the scripted model in `tests/fake_model.py`. A script such as
`FakeModel([tool("local_time", {}), [{"content": "It is noon."}]])` first asks
for your tool, then answers with the next script entry. `tests/test_agent.py`
shows the full `Agent` setup and `tests/test_tools.py` tests tools directly.

If the tool touches the disk, confine paths with `files.resolve_in`; if it
talks to the network, think about what a hostile web page could make the model
do.

### Add a slash command

Slash commands belong to the terminal chat. In `src/kova/ui.py`:

1. Add the line to `_COMMANDS` so `/help` lists it and Tab completes it.
2. Add a branch in the `while True:` loop in `_run_chat`, before the generic
   "unknown command" fallback.
3. If it takes an argument, add the choices to `_COMMAND_ARGUMENTS` for Tab
   completion.

A minimal example:

```python
_COMMANDS = {
    # ...
    "/version": "Show the Kova version",
}

# inside _run_chat, alongside the /help branch:
elif command == "/version" and not args:
    console.print(f"Kova {client.health()['version']}", style=theme.MUTED)
```

Keep the `not args` guard unless the command really takes arguments; the
parser splits on the first space. Anything that changes shared state (a chat's
mode or web flag, the active model) should call the server, as `/mode`, `/web`
and `/model` do, so the desktop app sees the same state.

For a new top-level CLI command instead, add it in `src/kova/cli.py` with
`@app.command()`; command groups use `@_chats.command()`, `@_model.command()`
and so on. Top-level commands are HTTP calls through `KovaClient`, like the
existing ones.

The desktop app has no slash commands. Its controls live in
`web_assets/index.html` and `web_assets/app.js`; add a button there and call
the same API endpoint.

### Change the identity or system prompt

The assistant's personality is in `_IDENTITIES` in `src/kova/agent.py`, one
entry per mode (`work` and `chat`). Edit the text directly. Keep the mode
differences honest: chat mode really cannot use files, reminders or memory
writes, so its identity says so.

`build_system_prompt` wraps that identity with:

- the current local time (the model uses it for "tomorrow at 5pm")
- the current mode
- the names of the tools available this turn
- the memory notes selected for this message

If you want a global instruction instead of a rewrite, appending a line to both
identities is usually the cleanest change. Do not put secrets in the prompt:
it is sent to the model server on every call.

The server builds the prompt, so both the terminal and desktop clients pick up
your change. Restart the background server after editing Python:

```sh
./cli stop
./cli            # starts a fresh server
```

or keep `./cli serve` running in another terminal while you work.

### Change the colours

`src/kova/theme.py` is the palette for the terminal:

```python
ACCENT = "#8b7cff"
ACCENT_SOFT = "#b4a9ff"
USER = "#5fd7af"
MUTED = "#8a8f98"
FAINT = "#5c616b"
BORDER = "#3a3d45"
SUCCESS = "#5fd787"
WARNING = "#ffaf5f"
ERROR = "#ff5f6d"
GRADIENT = ("#7aa2ff", "#8b7cff", "#c77dff", "#ff7eb6")  # the logo
CODE_THEME = "monokai"
```

(plus `SURFACE`, `ON_ACCENT`, `BASH`, `MEMORY` and `CODE`). The CLI, setup
wizard and chat all use these names, and `ui._CHAT_STYLE` mirrors them for the
input line and completion menu. The full-screen chat converts them for 256- and
16-colour terminals by itself (`kova.tui.style`); run `KOVA_COLORS=16 ./cli` or
`NO_COLOR=1 ./cli` to check a change on a simpler terminal.
`CODE_THEME` is the Pygments theme for code blocks in the terminal.

The web app keeps its own copy as CSS variables at the top of
`src/kova/web_assets/app.css`:

- `:root` — the dark theme
- `:root[data-theme=light]` — the light theme

Keep the two in sync: `--accent` with `ACCENT`, `--accent-soft` with
`ACCENT_SOFT`, `--user` with `USER`, and so on. The CSS file also has
background, surface and shadow variables that have no terminal equivalent, and
`.hljs-*` rules for code highlighting. Reload the window to see CSS changes;
restart the server to see Python changes.

### Add a model server

Any OpenAI-compatible server works. The normal path is:

```sh
./cli setup                                                # wizard
./cli model add lab http://192.168.1.20:8080/v1 MODEL      # or by hand
./cli model use lab
```

Add `--key KEY` if the server needs a bearer token. `base_url` is the prefix
Kova appends `/chat/completions` and `/models` to, so it usually ends in `/v1`.
`./cli model list` shows what is configured and which entry is active; inside a
chat, `/model` lists and switches them.

The same entries live in `~/.kova/config.toml`:

```toml
active = "lab"

[[models]]
name = "lab"
base_url = "http://192.168.1.20:8080/v1"
model = "MODEL"
context = 131072
api_key = "..."        # optional
```

`context` is the token budget used when trimming history. Set it to your
model's real context window; the `model add` command uses 131072 by default.
If you edit `config.toml` by hand, stop the server first so it does not write
over you, then start it again. API keys sit in that file in plain text, which
is why it is created with mode 0600 and left out of backups.

## Working on it with a coding agent

A coding agent can go a long way here, as long as you give it the map:

- Point it at `docs/ARCHITECTURE.md` first, then the specific recipe above.
  Ask it to name the files it plans to touch before it edits.
- Ask for a test with every behaviour change, and require
  `uv run pytest -q` to pass before it reports back.
- Keep changes to one recipe at a time. The turn loop and auth code are small
  but load-bearing; a broad refactor is harder to review than three focused
  diffs.
- Use a scratch home for manual poking, so your real chats are untouched:

  ```sh
  KOVA_HOME=/tmp/kova-dev ./cli status   # creates /tmp/kova-dev/config.toml
  # set port = 7788 in that file to avoid your real server on 7700
  KOVA_HOME=/tmp/kova-dev ./cli
  ```

- Some tests pin exact behaviour on purpose: the order of tools in
  `test_agent.py`, the SSE wire format in `test_server.py`, and ANSI colours in
  `test_ui.py`. If you change those deliberately, update the test in the same
  commit.

Please keep these invariants, which the rest of the design relies on:

- the server binds to loopback only, never `0.0.0.0`
- API routes require the bearer token or a same-origin browser session
- the bearer token never appears in a URL, a log, a backup, or the browser UI
- file tools stay inside `files_path`; memory writes stay inside the write dir
- `web_fetch` only fetches public addresses, including across redirects
- backups never contain the token or API keys

## Send a change

```sh
git checkout -b my-change
git add -A
git commit -m "Describe the change"
git push -u origin my-change
gh pr create --base main
```

Small, tested pull requests are easiest to merge. If you are unsure whether an
idea fits, open an issue with the behaviour you want and the part of
`docs/ARCHITECTURE.md` it touches.

## Privacy

This repository is public. Never commit or post real home folders, usernames, shell prompts, hostnames, home-network addresses or machine details, in code, docs, screenshots, issues or PRs. Use placeholders such as `~/`, `you@demo` and `192.0.2.10`. `python scripts/privacy_check.py` checks the repository and CI runs it. AI agents: see [AGENTS.md](AGENTS.md).
