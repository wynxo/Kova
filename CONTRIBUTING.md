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

uv run kova            # terminal chat (starts the background server)
uv run kova status     # is the server up, which model is active
uv run kova doctor     # check a first run: config, sandbox, port, token, Node, disk
uv run kova app    # the desktop app (Electron); --browser opens the web UI
```

These commands work on Windows too. The first run creates `.venv` and installs dependencies.

The server only listens on `127.0.0.1:7700`, and the clients start it for you.
`uv run kova serve` runs it in the foreground when you want to see logs; `uv run kova stop`
stops a background one.

The desktop app needs Node.js 22+ and npm; `uv run kova app` installs its
Electron dependencies on first run (developer convenience only; normal installers
prepare them and register an OS launcher at install time). Without Node/npm it says so and opens the
web UI instead, which `uv run kova app --browser` selects directly (the installed
equivalent is `kova web`).

If you have never picked a model, run `uv run kova setup`, or point Kova at a server
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

### Visual snapshots

The optional `visual` group uses Python Playwright and Chromium, with no Node
setup or running model. It captures the component gallery, token sheet and empty
start screen at 390 px and 1280 px in Dark, Light and the installed Forest
example theme. A throwaway `KOVA_HOME`, offline model stub and fresh browser
contexts keep the captures separate from your chats and settings.

The gallery's Markdown reference uses the real reply renderer. Fences can name
a file with `language:path` or `language title="file name"`. Math accepts
`$…$` inline and `$$…$$` on its own line. Use a `mermaid` fence for supported
Mermaid Tiny diagrams; unsupported diagrams keep their copyable source.

On x86-64 Linux, a quick local run is:

```sh
uv run --locked --group visual playwright install chromium
KOVA_VISUAL=1 uv run --locked --group visual pytest -q -m visual tests/visual
```

The regular suite skips these opt-in tests, as does a local visual run without
Playwright or Chromium. The visual CI job requires both.
Use the same pinned Linux image as CI for authoritative comparisons and baseline
updates, since font and browser rendering can differ across hosts:

```sh
docker run --rm --platform linux/amd64 --ipc=host --user "$(id -u):$(id -g)" \
  -e HOME=/tmp -e UV_PROJECT_ENVIRONMENT=/tmp/visual-venv \
  -v "$PWD:/work" -w /work mcr.microsoft.com/playwright/python:v1.64.1-noble \
  bash -euc 'set -o pipefail
    curl -LsSf https://astral.sh/uv/install.sh | UV_UNMANAGED_INSTALL=/tmp/visual-tools sh
    KOVA_VISUAL_REQUIRED=1 /tmp/visual-tools/uv run --locked --group visual pytest -q -m visual tests/visual'
```

To accept an intentional change, add `-e KOVA_UPDATE_SNAPSHOTS=1` to the
`docker run` options. Review the PNGs in `tests/visual/chromium-linux/`, rerun
without that flag, and commit the changed baselines. The token sheet compares
pixels exactly; the other screens allow up to 40 changed pixels after filtering
small channel differences. Mismatches write expected, actual and diff PNGs to
`test-results/visual/`, uploaded by CI as `visual-diffs` on failure.

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
uv run kova stop
uv run kova            # starts a fresh server
```

or keep `uv run kova serve` running in another terminal while you work.

### Change the colours

`src/kova/theme.py` names the terminal's colour roles, with `kova-dark`'s values:

```python
ACCENT = "#f6f6f8"
ACCENT_SOFT = "#b4b4c0"
USER = "#5fd7af"
MUTED = "#8a8f98"
FAINT = "#5c616b"
BORDER = "#3a3d45"
SUCCESS = "#5fd787"
WARNING = "#ffaf5f"
ERROR = "#ff5f6d"
GRADIENT = ("#ffffff", "#d8d8e0", "#b4b4c0", "#8a8f98")  # the logo
CODE_THEME = "monokai"
```

(plus `SURFACE`, `ON_ACCENT`, `BASH`, `MEMORY` and `CODE`). The CLI, setup
wizard and chat all use these names, including the input line and completion
menu. Every other theme maps onto them from its semantic tokens
(`theme.ROLE_TOKENS`), so the terminal and the web app share one palette:
`kova themes use light` or `KOVA_THEME=forest uv run kova` repaints the terminal with
that theme. To change the terminal's colours for every theme, change the token
in `design/tokens.json`; to change only the dark terminal, change the constant.
The full-screen chat converts them for 256- and 16-colour terminals by itself
(`kova.tui.style`); run `KOVA_COLORS=16 uv run kova` or `NO_COLOR=1 uv run kova` to check a
change on a simpler terminal.
`CODE_THEME` is the Pygments theme for code blocks in the terminal.

The web app consumes semantic CSS variables generated from `design/tokens.json`.
Edit that source and run `python scripts/build_tokens.py`; never edit `tokens.css`
by hand. Components use roles such as `--bg-base`, `--fg-muted` and
`--accent-default`, not a second palette in `app.css`. See
[docs/DESIGN.md](docs/DESIGN.md) and [docs/THEMES.md](docs/THEMES.md).

### Add a model server

Any OpenAI-compatible server works. The normal path is:

```sh
uv run kova setup                                                # wizard
uv run kova model add lab http://192.168.1.20:8080/v1 MODEL      # or by hand
uv run kova model use lab
```

Add `--key KEY` if the server needs a bearer token. `base_url` is the prefix
Kova appends `/chat/completions` and `/models` to, so it usually ends in `/v1`.
`uv run kova model list` shows what is configured and which entry is active; inside a
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

[voice]
stt = "local"          # "browser" for the desktop web UI
tts = "espeak-ng"      # "off" disables spoken replies
max_seconds = 60        # longest voice clip the server accepts
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
  KOVA_HOME=/tmp/kova-dev uv run kova status   # creates /tmp/kova-dev/config.toml
  # set port = 7788 in that file to avoid your real server on 7700
  KOVA_HOME=/tmp/kova-dev uv run kova
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
