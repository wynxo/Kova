"""Kova's command-line interface.

The CLI is a thin HTTP client of ``kova serve``. ``get_client`` starts the
server on demand; the commands render chat replies with Rich.
"""

from __future__ import annotations

import os
import signal
import sys
import threading
from pathlib import Path

import httpx
import typer
from rich.console import Console

from kova.client import KovaClient, ensure_server
from kova.config import ensure_token, load_config, save_config

console = Console()
app = typer.Typer(help="Kova: your own private AI.", no_args_is_help=True)

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
# Chat rendering
# ---------------------------------------------------------------------------

def _watch_reminders(client: KovaClient) -> None:
    """Background thread: print reminder events as they arrive."""
    watch_console = Console()
    try:
        for event in client.events():
            if event.get("type") == "reminder":
                watch_console.print(f"[green]⏰ {event['text']}[/green]")
    except Exception:  # a dead server must never crash the chat
        pass


def _render_turn(client: KovaClient, chat_id: str, text: str, no_think: bool, console: Console) -> None:
    """Stream one turn, rendering reasoning, tools and replies live."""
    last = None  # kind of the last streamed chunk, to break lines between thinking and answer
    for event in client.stream_message(chat_id, text):
        kind = event["type"]
        if kind in ("reasoning", "text") and last in ("reasoning", "text") and kind != last:
            console.print()
        if kind == "reasoning":
            if no_think:
                continue
            console.print(event["text"], end="", markup=False, soft_wrap=True, style="dim")
        elif kind == "text":
            console.print(event["text"], end="", markup=False, soft_wrap=True)
        elif kind == "tool_call":
            if last in ("reasoning", "text"):
                console.print()
            console.print(f"[bold yellow]⚙ {event['name']}[/bold yellow]")
        elif kind == "tool_result":
            summary = event["summary"][:100]
            if event["ok"]:
                console.print(f"[yellow]⚙ {event['name']} →[/yellow] {summary}")
            else:
                console.print(f"[red]✗ {event['name']}:[/red] {summary}")
        elif kind == "error":
            console.print(f"[red]Error: {event['message']}[/red]")
        elif kind == "done":
            break
        last = kind
    console.print()


def _run_chat(client: KovaClient, chat_id: str, no_think: bool) -> None:
    console.print("[dim]Type a message. Commands: /new, /model <name>, /exit.[/dim]")
    threading.Thread(target=_watch_reminders, args=(client,), daemon=True).start()
    while True:
        try:
            line = console.input("[cyan]⚡ [/cyan]")
        except (EOFError, KeyboardInterrupt):
            break
        stripped = line.strip()
        if stripped in ("/exit", "/quit"):
            break
        if stripped == "/new":
            chat_id = client.create_chat()["id"]
            console.print("[dim]Started a new chat.[/dim]")
            continue
        if stripped.startswith("/model"):
            parts = stripped.split(maxsplit=1)
            if len(parts) == 2:
                client.model_use(parts[1])
                console.print(f"[dim]Now using {parts[1]}.[/dim]")
            continue
        if not stripped:
            continue
        _render_turn(client, chat_id, stripped, no_think, console)


# ---------------------------------------------------------------------------
# Top-level commands
# ---------------------------------------------------------------------------

@app.command("ask")
def ask(text: str):
    """Answer once and print it; handy for scripts."""
    client = get_client()
    chat = client.create_chat()
    pieces = []
    for event in client.stream_message(chat["id"], text):
        if event["type"] == "text":
            pieces.append(event["text"])
        elif event["type"] == "error":
            print(f"Error: {event['message']}", file=sys.stderr)
            raise typer.Exit(1)
    print("".join(pieces))


@app.command()
def chat(
    new: bool = typer.Option(False, "--new", help="Start a fresh chat instead of the latest."),
    chat: str | None = typer.Option(None, "--chat", help="Continue an existing chat by ID."),
    no_think: bool = typer.Option(False, "--no-think", help="Hide the model's reasoning."),
):
    """Chat with Kova; replies stream live. Type /exit to leave."""
    client = get_client()
    if chat:
        chat_id = chat
    else:
        chats = client.list_chats()
        chat_id = chats[0]["id"] if (chats and not new) else client.create_chat()["id"]
    _run_chat(client, chat_id, no_think)


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
    except httpx.RequestError:
        console.print("[red]Server is down.[/red]")
        return
    reachable = "yes" if info["model_ok"] else "no"
    console.print(f"[green]Server up[/green] · v{info['version']} · model {info['model']} · model reachable: {reachable}")


# ---------------------------------------------------------------------------
# Command groups
# ---------------------------------------------------------------------------


@_model.command("list")
def model_list():
    for entry in get_client().models_list():
        console.print(f"[bold]{entry['name']}[/bold] — {entry['model']} @ {entry['base_url']}")


@_model.command("add")
def model_add(name: str, base_url: str, model: str, key: str | None = typer.Option(None, "--key")):
    get_client().model_add(name, base_url, model, key)
    console.print(f"Added model [bold]{name}[/bold].")


@_model.command("use")
def model_use(name: str):
    get_client().model_use(name)
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
    raise typer.Exit(f"No note named {slug}.")


@_memory.command("add")
def memory_add(title: str, body: str, tags: list[str] = typer.Option([], "--tags")):
    note = get_client().memory_add(title, body, tags)
    console.print(f"Saved [bold]{note['title']}[/bold].")


@_memory.command("rm")
def memory_rm(slug: str):
    if get_client().memory_delete(slug):
        console.print(f"Removed [bold]{slug}[/bold].")
    else:
        raise typer.Exit(f"No note named {slug}.")


@_memory.command("use")
def memory_use(path: str, obsidian: bool = typer.Option(False, "--obsidian")):
    cfg = load_config()
    cfg.memory_path = Path(path)
    cfg.memory_write_subdir = "Kova" if obsidian else None
    save_config(cfg)
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
        raise typer.Exit(f"No reminder #{rid}.")
