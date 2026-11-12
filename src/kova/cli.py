"""Kova's command-line interface.

The CLI is a thin HTTP client of ``kova serve``. ``get_client`` starts the
server on demand; the commands render chat replies with Rich.
"""

from __future__ import annotations

import json
import os
import signal
import sys
from pathlib import Path

import typer
from rich.console import Console
from typer.core import TyperGroup

from kova.client import KovaClient, KovaError, ensure_server
from kova.config import ensure_token, load_config, save_config
from kova.ui import _run_chat

console = Console()


def _fail(message: str) -> None:
    console.print(" ".join(message.splitlines()), style="red", markup=False, soft_wrap=True)
    raise typer.Exit(1)


class _KovaGroup(TyperGroup):
    def invoke(self, ctx):
        try:
            return super().invoke(ctx)
        except KovaError as exc:
            _fail(str(exc))


app = typer.Typer(help="Kova: your own private AI.", no_args_is_help=True, cls=_KovaGroup)

_model = typer.Typer(no_args_is_help=True, help="Manage model servers.")
app.add_typer(_model, name="model")

_memory = typer.Typer(no_args_is_help=True, help="Manage Markdown memory notes.")
app.add_typer(_memory, name="memory")

_reminders = typer.Typer(no_args_is_help=True, help="Manage reminders.")
app.add_typer(_reminders, name="reminders")


def get_client() -> KovaClient:
    """Return a client, starting the server in the background if it is down."""
    return ensure_server(load_config())


# ---------------------------------------------------------------------------

ASK_INPUT_LIMIT = 100_000  # bytes per piped input or attached file
ASK_TITLE_PREFIX = "[ask] "  # marks throwaway ask-chats so history can tell them apart

_stderr = Console(stderr=True, highlight=False, soft_wrap=True)


def _read_capped(stream, label: str) -> str:
    """Read at most ``ASK_INPUT_LIMIT`` bytes, warning on stderr if there was more."""
    data = stream.read(ASK_INPUT_LIMIT + 1)
    if len(data) > ASK_INPUT_LIMIT:
        data = data[:ASK_INPUT_LIMIT]
        _stderr.print(f"Warning: {label} is over {ASK_INPUT_LIMIT // 1000} KB; only the first "
                      f"{ASK_INPUT_LIMIT // 1000} KB was sent.", style="yellow", markup=False)
    return data.decode("utf-8", errors="replace")


def _piped_stdin() -> str | None:
    """Return piped stdin, or None when stdin is a terminal or empty."""
    if sys.stdin is None or sys.stdin.isatty():
        return None
    piped = _read_capped(typer.get_binary_stream("stdin"), "piped input")
    return piped if piped.strip() else None


def _ask_prompt(question: str | None, piped: str | None, files: list[tuple[str, str]]) -> str:
    """Join the question with clearly delimited piped input and attached files."""
    if not (question and question.strip()):
        if piped is None:
            raise typer.BadParameter("give a question, or pipe some text in.", param_hint="TEXT")
        question, piped = piped.strip(), None
    parts = [question]
    if piped is not None:
        parts.append(f"<stdin>\n{piped.rstrip()}\n</stdin>")
    for name, content in files:
        parts.append(f'<file name="{name}">\n{content.rstrip()}\n</file>')
    return "\n\n".join(parts)


def _ask_error(message: str) -> None:
    print(f"Error: {' '.join(message.splitlines())}", file=sys.stderr)
    raise typer.Exit(1)


@app.command("ask")
def ask(
    text: str | None = typer.Argument(None, help="The question. Optional if you pipe text in."),
    files: list[Path] = typer.Option([], "--file", "-f", exists=True, dir_okay=False, readable=True,
                                     help="Attach a file's contents (repeatable)."),
    model: str | None = typer.Option(None, "--model", "-m",
                                     help="Use this model for this question only."),
    as_json: bool = typer.Option(False, "--json", help="Print a JSON object for scripts."),
    chat_id: str | None = typer.Option(None, "--chat", help="Continue an existing chat by ID."),
    think: bool = typer.Option(False, "--think", help="Also print the reasoning to stderr."),
):
    """Answer once and print it; handy for scripts.

    Piped stdin is sent along with the question (or is the question if none is given):
    git diff | kova ask "review this"
    """
    attached = []
    for path in files:
        with path.open("rb") as handle:
            attached.append((path.name, _read_capped(handle, str(path))))
    prompt = _ask_prompt(text, _piped_stdin(), attached)

    try:
        client = get_client()
        if chat_id is None:
            first_line = ((text or prompt).strip().splitlines() or [""])[0]
            title = ASK_TITLE_PREFIX + first_line[:54]
            chat_id = client.create_chat(title)["id"]
        pieces: list[str] = []
        tools: list[str] = []
        used_model = model
        reasoning = False
        for event in client.stream_message(chat_id, prompt, model=model):
            kind = event["type"]
            if kind == "reasoning" and think:
                reasoning = True
                _stderr.print(event["text"], style="dim", end="", markup=False)
            elif kind == "text":
                if reasoning:
                    reasoning = False
                    _stderr.print()
                pieces.append(event["text"])
                if not as_json:
                    sys.stdout.write(event["text"])
                    sys.stdout.flush()
            elif kind == "tool_call":
                tools.append(event["name"])
            elif kind == "error":
                if pieces and not as_json:
                    print()
                _ask_error(event["message"])
            elif kind == "done":
                used_model = event.get("model", used_model)
                break
    except KovaError as exc:
        _ask_error(str(exc))
    if reasoning:
        _stderr.print()
    if as_json:
        print(json.dumps({"answer": "".join(pieces), "chat_id": chat_id,
                          "model": used_model, "tools": tools}, ensure_ascii=False))
    else:
        print()


@app.command()
def chat(
    new: bool = typer.Option(False, "--new", help="Start a fresh chat instead of the latest."),
    chat: str | None = typer.Option(None, "--chat", help="Continue an existing chat by ID."),
    no_think: bool = typer.Option(False, "--no-think", help="Hide the model's reasoning."),
):
    """Chat with Kova; replies stream live. Type /exit to leave."""
    _run_chat(get_client(), chat_id=chat, new=new, no_think=no_think, console=console)


@app.command()
def serve():
    """Run the Kova server in the foreground."""
    import uvicorn

    from kova.server import create_app

    uvicorn.run(create_app(load_config()), host="127.0.0.1", port=load_config().port, timeout_graceful_shutdown=3)


@app.command()
def stop():
    """Stop a background Kova server."""
    pidfile = load_config().home / "server.pid"
    if not pidfile.exists():
        console.print("[dim]No running server recorded.[/dim]")
        return
    try:
        pid = int(pidfile.read_text().strip())
    except ValueError:
        pidfile.unlink(missing_ok=True)
        return
    try:
        os.kill(pid, signal.SIGTERM)
        console.print("Server stopped.")
    except ProcessLookupError:
        pidfile.unlink(missing_ok=True)
        console.print("[dim]Server was not running.[/dim]")
    except OSError:
        console.print("[red]Could not stop the server.[/red]")


@app.command()
def status():
    """Report whether the server is up, its version and active model."""
    cfg = load_config()
    console.print(f"Active model: [bold]{cfg.active}[/bold]")
    try:
        info = KovaClient(f"http://127.0.0.1:{cfg.port}", ensure_token(cfg.home)).health()
    except KovaError as exc:
        if exc.status is not None:
            raise
        console.print("[red]Server is down.[/red] It starts by itself when you run kova chat.")
        return
    reachable = "yes" if info["model_ok"] else "no"
    console.print(f"[green]Server up[/green] · v{info['version']} · model {info['model']} · model reachable: {reachable}")


# ---------------------------------------------------------------------------
# Command groups
# ---------------------------------------------------------------------------


def _list_models(client: KovaClient) -> None:
    active = client.health()["model"]
    for entry in client.models_list():
        marker = " (active)" if entry["name"] == active else ""
        console.print(f"[bold]{entry['name']}[/bold] — {entry['model']} @ {entry['base_url']}{marker}")


@_model.command("list")
def model_list():
    _list_models(get_client())


@_model.command("add")
def model_add(name: str, base_url: str, model: str, key: str | None = typer.Option(None, "--key")):
    get_client().model_add(name, base_url, model, key)
    console.print(f"Added model [bold]{name}[/bold].")


@_model.command("use")
def model_use(name: str):
    try:
        get_client().model_use(name)
    except KovaError as exc:
        if exc.status == 404:
            _fail(f'No model named "{name}". Run: kova model list')
        raise
    console.print(f"[bold]{name}[/bold] is now active.")


@_memory.command("list")
def memory_list(q: str | None = typer.Argument(None)):
    for note in get_client().memory_list(q):
        console.print(f"[bold]{note['title']}[/bold] — {note['body']}")


@_memory.command("search")
def memory_search(q: str):
    for note in get_client().memory_list(q):
        console.print(f"[bold]{note['title']}[/bold] — {note['body']}")


@_memory.command("show")
def memory_show(slug: str):
    for note in get_client().memory_list():
        if note["slug"] == slug:
            console.print(f"[bold]{note['title']}[/bold]\n{note['body']}")
            return
    _fail(f'No note named "{slug}".')


@_memory.command("add")
def memory_add(title: str, body: str, tags: list[str] = typer.Option([], "--tags")):
    note = get_client().memory_add(title, body, tags)
    console.print(f"Saved [bold]{note['title']}[/bold].")


@_memory.command("rm")
def memory_rm(slug: str):
    if get_client().memory_delete(slug):
        console.print(f"Removed [bold]{slug}[/bold].")
    else:
        _fail(f'No note named "{slug}".')


@_memory.command("use")
def memory_use(path: str, obsidian: bool = typer.Option(False, "--obsidian")):
    cfg = load_config()
    cfg.memory_path = Path(path).expanduser().resolve()
    cfg.memory_write_subdir = "Kova" if obsidian else None
    client = KovaClient(f"http://127.0.0.1:{cfg.port}", ensure_token(cfg.home))
    try:
        client.memory_use(str(cfg.memory_path), obsidian)
    except KovaError as exc:
        if exc.status is not None:
            raise
        save_config(cfg)  # server not running: it reads the new folder on next start
    suffix = " (Obsidian mode)" if obsidian else ""
    console.print(f"Memory folder set to {path}{suffix}.")


@_reminders.command("list")
def reminders_list():
    for reminder in get_client().reminders_list():
        console.print(f"[bold]#{reminder['id']}[/bold] {reminder['when']} — {reminder['text']}")


@_reminders.command("add")
def reminders_add(when: str, text: str):
    result = get_client().reminder_add(when, text)
    console.print(f"Reminder #{result['id']} set for {result['when']}.")


@_reminders.command("rm")
def reminders_rm(rid: int):
    if get_client().reminder_delete(rid):
        console.print(f"Removed reminder #{rid}.")
    else:
        _fail(f"No reminder #{rid}.")
