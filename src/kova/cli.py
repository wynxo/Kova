"""Kova's command-line interface.

The CLI is a thin HTTP client of ``kova serve``. ``get_client`` starts the
server on demand; the commands render chat replies with Rich.
"""

from __future__ import annotations

import json
import os
import signal
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import typer
from rich import box
from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.table import Table
from rich.text import Text
from typer.core import TyperGroup

from kova import __version__, backup, lifecycle, onboarding, theme
from kova.client import KovaClient, KovaError, ensure_server
from kova.config import ensure_token, load_config, save_config
from kova.modes import MODES
from kova.tools.reminders import format_relative, format_when
from kova.ui import _run_chat

console = Console()

SYMBOL_OK = "✓"
SYMBOL_ERROR = "✗"
SYMBOL_BULLET = "◇"
SYMBOL_ARROW = "→"


def _hint_for(message: str) -> str | None:
    """One short fix-it line that matches this error, or None for plain errors."""
    text = message.casefold()
    if "no note named" in text:
        return "Run: kova memory list"
    if "no reminder" in text:
        return "Run: kova reminders list"
    if "no model named" in text:
        return "Run: kova model list"
    if "no chat" in text:
        return "Run: kova chats"
    if "either --md or --json" in text:
        return "Pick one, for example: kova chats export 3 --md"
    if "title cannot be empty" in text:
        return "Usage: kova chats rename <#|id> TITLE"
    if "at least one word" in text:
        return "Usage: kova chats search <words>"
    if "cannot reach kova server" in text:
        return "It starts by itself when you chat; run: kova status to check."
    if "couldn't reach a model server" in text:
        return "Check the address and that the model server is running, then: kova setup"
    if "--key" in text:
        return "Add the key, for example: kova setup --url URL --model MODEL --key KEY"
    if "--model" in text and ("didn't list" in text or "pass --model" in text):
        return "Name one, for example: kova setup --url URL --model MODEL"
    if "--url" in text:
        return "Add the server, for example: kova setup --url http://127.0.0.1:11434"
    return None


def _error_line(message: str) -> None:
    """One styled error line, without exiting (for commands that keep going)."""
    text = " ".join(message.splitlines())
    console.print(Text.assemble((f"{SYMBOL_ERROR} ", f"bold {theme.ERROR}"), (text, theme.ERROR)))


def _fail(message: str, hint: str | None = None) -> None:
    """Print one consistent error line, plus an optional dim hint, and exit 1."""
    text = " ".join(message.splitlines())
    _error_line(text)
    hint = hint if hint is not None else _hint_for(text)
    if hint:
        console.print(Text(f"  {hint}", style=theme.MUTED))
    raise typer.Exit(1)


def _ok(message: str) -> None:
    console.print(Text.assemble((f"{SYMBOL_OK} ", f"bold {theme.SUCCESS}"), (message, "")))


def _muted(message: str) -> None:
    console.print(Text(message, style=theme.MUTED))


def _table(*, title: str | None = None, caption: str | None = None) -> Table:
    """A Kova table: muted header rule, accent title, no outer box."""
    return Table(
        box=box.SIMPLE_HEAD,
        border_style=theme.BORDER,
        header_style=f"bold {theme.MUTED}",
        title=Text(title, style=f"bold {theme.ACCENT}") if title else None,
        title_justify="left",
        caption=Text(caption, style=theme.MUTED) if caption else None,
        caption_justify="left",
        pad_edge=False,
        show_edge=False,
        expand=False,
    )


def _empty(message: str, hint: str) -> None:
    """A friendly empty state: one line, then a dim hint on how to fill it."""
    console.print(Text.assemble((f"{SYMBOL_BULLET} ", f"bold {theme.ACCENT_SOFT}"), (message, "")))
    console.print(Text(f"  {hint}", style=theme.MUTED))


def _clip(value: str, limit: int = 60) -> str:
    """Collapse whitespace and shortens to one tidy table cell."""
    text = " ".join((value or "").split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


class _KovaGroup(TyperGroup):
    def invoke(self, ctx):
        try:
            return super().invoke(ctx)
        except KovaError as exc:
            _fail(str(exc))


app = typer.Typer(
    help="Kova: your own private AI that lives on your computer.",
    epilog=f"[{theme.MUTED}]Start a chat with [bold]kova[/bold]. "
           "Add [bold]--help[/bold] to any command for its flags.[/]",
    no_args_is_help=False,
    cls=_KovaGroup,
)


def _version(value: bool) -> None:
    if value:
        console.print(Text.assemble(
            ("Kova", f"bold {theme.ACCENT}"),
            ("  ·  ", theme.BORDER),
            (f"v{__version__}", theme.MUTED),
        ))
        console.print(Text("Your own private AI that lives on your computer.", style=f"italic {theme.MUTED}"))
        raise typer.Exit()


@app.callback(invoke_without_command=True)
def root(
    ctx: typer.Context,
    version: bool = typer.Option(False, "--version", callback=_version, is_eager=True,
                                 help="Show the installed version."),
    new: bool = typer.Option(False, "--new", help="Start a fresh chat instead of the latest."),
    chat: str | None = typer.Option(None, "--chat", help="Continue an existing chat by ID."),
    resume: str | None = typer.Option(None, "--resume", help="Continue a chat by its number from: kova chats"),
    no_think: bool = typer.Option(False, "--no-think", help="Hide the model's reasoning."),
    mode: str | None = typer.Option(None, "--mode", help="Start the chat in this mode: chat or work."),
    no_web: bool = typer.Option(False, "--no-web", help="Turn web search off for this chat."),
):
    """Chat with Kova; replies stream live. Type /exit to leave."""
    if ctx.invoked_subcommand is None:
        _start_chat(new, chat, resume, no_think, mode, no_web)


def _lifecycle_line(message: str) -> Text:
    """Style one lifecycle (update/uninstall) line: ✓ success, → step, else muted."""
    text = message.strip()
    if text.startswith(f"{SYMBOL_OK} "):
        return Text.assemble((f"{SYMBOL_OK} ", f"bold {theme.SUCCESS}"), (text[2:], ""))
    if text.startswith(f"{SYMBOL_ARROW} "):
        return Text.assemble((f"{SYMBOL_ARROW} ", theme.ACCENT_SOFT), (text[2:], theme.MUTED))
    return Text(text, style=theme.MUTED)


def _lifecycle_emit(message: str) -> None:
    console.print(_lifecycle_line(message))


def _print_update_check(message: str) -> None:
    """Give the update check a ✓ / ↑ / • line that matches the rest of the CLI."""
    lowered = message.casefold()
    if lowered.startswith("up to date"):
        console.print(Text.assemble((f"{SYMBOL_OK} ", f"bold {theme.SUCCESS}"), (message, theme.SUCCESS)))
    elif lowered.startswith("update available"):
        console.print(Text.assemble(("↑ ", f"bold {theme.WARNING}"), (message, "")))
    else:
        console.print(Text.assemble((f"{SYMBOL_BULLET} ", theme.ACCENT_SOFT), (message, theme.MUTED)))


@app.command()
def update(
    ref: str | None = typer.Option(None, "--ref", help="GitHub branch, tag, or commit (default: KOVA_REF or main)."),
    check: bool = typer.Option(False, "--check", help="Only compare installed and remote versions/commits."),
):
    """Check for updates or reinstall the latest Kova with uv."""
    try:
        target = lifecycle.selected_ref(ref)
        if check:
            _print_update_check(lifecycle.check_update(target))
        else:
            lifecycle.update(target, emit=_lifecycle_emit)
    except (lifecycle.LifecycleError, OSError) as exc:
        _fail(str(exc))


@app.command()
def uninstall(
    purge: bool = typer.Option(False, "--purge", help="Also delete ~/.kova (or KOVA_HOME) without asking."),
    keep_data: bool = typer.Option(False, "--keep-data", help="Keep data without asking."),
):
    """Stop the server and remove Kova. Data is kept unless you confirm deletion."""
    try:
        lifecycle.uninstall(purge=purge, keep_data=keep_data,
                            confirm=lambda question: typer.confirm(question, default=False),
                            emit=_lifecycle_emit)
    except (lifecycle.LifecycleError, OSError) as exc:
        _fail(str(exc))


_model = typer.Typer(no_args_is_help=True, help="Manage model servers.")
app.add_typer(_model, name="model")

_memory = typer.Typer(no_args_is_help=True, help="Manage Markdown memory notes.")
app.add_typer(_memory, name="memory")

_reminders = typer.Typer(no_args_is_help=True, help="Manage reminders.")
app.add_typer(_reminders, name="reminders")

_chats = typer.Typer(no_args_is_help=False, help="Browse and manage chat history.")
app.add_typer(_chats, name="chats")


@_chats.callback(invoke_without_command=True)
def chats_root(ctx: typer.Context):
    """List chats, newest first."""
    if ctx.invoked_subcommand is None:
        chats_list()


def get_client() -> KovaClient:
    """Return a client, starting the server in the background if it is down."""
    return ensure_server(load_config())


def _check_mode(mode: str | None) -> None:
    """Reject an unknown --mode value as a usage error."""
    if mode is not None and mode not in MODES:
        raise typer.BadParameter(f"must be one of: {', '.join(MODES)}", param_hint="--mode")


# ---------------------------------------------------------------------------

ASK_INPUT_LIMIT = 100_000  # bytes per piped input or attached file
from kova.client import ASK_TITLE_PREFIX  # marks throwaway ask-chats so history can hide them

_stderr = Console(stderr=True, highlight=False, soft_wrap=True)


def _read_capped(stream, label: str) -> str:
    """Read at most ``ASK_INPUT_LIMIT`` bytes, warning on stderr if there was more."""
    data = stream.read(ASK_INPUT_LIMIT + 1)
    if len(data) > ASK_INPUT_LIMIT:
        data = data[:ASK_INPUT_LIMIT]
        _stderr.print(f"Warning: {label} is over {ASK_INPUT_LIMIT // 1000} KB; only the first "
                      f"{ASK_INPUT_LIMIT // 1000} KB was sent.", style=theme.WARNING, markup=False)
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
    mode: str | None = typer.Option(None, "--mode", help="Chat mode for this question: chat or work."),
):
    """Answer once and print it; handy for scripts.

    Piped stdin is sent along with the question (or is the question if none is given):
    git diff | kova ask "review this"
    """
    _check_mode(mode)
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
            chat_id = client.create_chat(title, mode=mode)["id"]
        elif mode is not None:
            client.update_chat(chat_id, mode=mode)
        pieces: list[str] = []
        tools: list[str] = []
        used_model = model
        reasoning = False
        for event in client.stream_message(chat_id, prompt, model=model):
            kind = event["type"]
            if kind == "reasoning" and think:
                reasoning = True
                _stderr.print(event["text"], style=theme.MUTED, end="", markup=False)
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


@app.command(hidden=True)
def chat(
    new: bool = typer.Option(False, "--new", help="Start a fresh chat instead of the latest."),
    chat: str | None = typer.Option(None, "--chat", help="Continue an existing chat by ID."),
    resume: str | None = typer.Option(None, "--resume", help="Continue a chat by its number from: kova chats"),
    no_think: bool = typer.Option(False, "--no-think", help="Hide the model's reasoning."),
    mode: str | None = typer.Option(None, "--mode", help="Start the chat in this mode: chat or work."),
    no_web: bool = typer.Option(False, "--no-web", help="Turn web search off for this chat."),
):
    """Old name for plain ``kova``; kept so existing habits and scripts still work."""
    _start_chat(new, chat, resume, no_think, mode, no_web)


def _start_chat(new: bool, chat: str | None, resume: str | None, no_think: bool,
                mode: str | None, no_web: bool) -> None:
    _check_mode(mode)
    cfg = load_config()
    if sys.stdin.isatty() and onboarding.needs_setup(cfg):
        configured = any(m.model for m in cfg.models)
        question = (f"Can't reach your model \"{cfg.active}\" right now. Run the setup wizard?"
                    if configured else "Kova isn't set up yet. Run the setup wizard first?")
        # An offline homelab is common: default to "no" so chat still opens.
        if typer.confirm(question, default=not configured):
            onboarding.run_setup(cfg)
    client = get_client()
    if resume:
        chat = _resolve_chat_id(client, resume)
    _run_chat(client, chat_id=chat, new=new, no_think=no_think, mode=mode,
              web=False if no_web else None, console=console)


@app.command()
def setup(
    url: str | None = typer.Option(None, "--url", help="Model server URL, for example http://192.168.1.20:8080."),
    model: str | None = typer.Option(None, "--model", help="Model name to use."),
    name: str | None = typer.Option(None, "--name", help="Name to save this model under."),
    key: str | None = typer.Option(None, "--key", help="API key, if the server needs one."),
    yes: bool = typer.Option(False, "--yes", "-y", help="Save it even if the test message fails."),
):
    """Find a model server and get Kova ready to chat."""
    try:
        onboarding.run_setup(load_config(), url=url, model=model, name=name, api_key=key, yes=yes)
    except onboarding.SetupError as exc:
        _fail(str(exc))


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
        _muted("No running server recorded.")
        return
    try:
        pid = int(pidfile.read_text().strip())
    except ValueError:
        pidfile.unlink(missing_ok=True)
        return
    try:
        os.kill(pid, signal.SIGTERM)
        _ok("Server stopped.")
    except ProcessLookupError:
        pidfile.unlink(missing_ok=True)
        _muted("Server was not running.")
    except OSError as exc:
        _error_line(f"Could not stop the server: {exc}")


def _status_rows(cfg, info: dict | None, *, notes: int | None, reminders: int | None) -> Table:
    """The two-column label/value grid inside the status card."""
    grid = Table.grid(padding=(0, 2))
    grid.add_column(no_wrap=True)
    grid.add_column(no_wrap=True)

    def dot(style: str) -> Text:
        return Text("●", style=style)

    if info is None:
        server = Text.assemble(dot(theme.ERROR), ("  down", ""), ("  ·  is the server running?", theme.MUTED))
        model = Text(cfg.active or "none", style=theme.MUTED)
    else:
        server = Text.assemble(dot(theme.SUCCESS), ("  up", ""), (f"  ·  v{info['version']}", theme.MUTED))
        entry = next((m for m in info.get("models", []) if m.get("name") == info.get("model")), None)
        name = f"{info['model']}" + (f" · {entry['model']}" if entry and entry.get("model") else "")
        model = Text.assemble(
            dot(theme.SUCCESS if info.get("model_ok") else theme.WARNING),
            ("  " + name, ""),
            ("  ·  reachable" if info.get("model_ok") else "  ·  not reachable", theme.SUCCESS if info.get("model_ok") else theme.WARNING),
        )
    note_word = "note" if notes == 1 else "notes"
    memory = Text(f"{notes} {note_word}" if notes is not None else "—", style=theme.MUTED)
    upcoming = Text(f"{reminders} upcoming" if reminders is not None else "—", style=theme.MUTED)
    for label, value in (("Server", server), ("Model", model), ("Memory", memory), ("Reminders", upcoming)):
        grid.add_row(Text(label, style=theme.MUTED), value)
    return grid


@app.command()
def status():
    """Report whether the server is up, its version and active model."""
    cfg = load_config()
    client = KovaClient(f"http://127.0.0.1:{cfg.port}", ensure_token(cfg.home))
    try:
        info = client.health()
    except KovaError as exc:
        if exc.status is not None:
            raise
        panel = Panel(
            _status_rows(cfg, None, notes=None, reminders=None),
            title=Text.assemble(("Kova", f"bold {theme.ACCENT}"), (" status", theme.MUTED)),
            title_align="left",
            border_style=theme.BORDER,
            box=box.ROUNDED,
            padding=(0, 1),
            expand=False,
        )
        console.print(panel)
        _muted("It starts by itself when you run kova.")
        return

    try:
        models = client.models_list()
    except KovaError:
        models = []
    info["models"] = models
    try:
        notes = len(client.memory_list())
    except KovaError:
        notes = None
    try:
        reminders = len(client.reminders_list())
    except KovaError:
        reminders = None

    panel = Panel(
        _status_rows(cfg, info, notes=notes, reminders=reminders),
        title=Text.assemble(("Kova", f"bold {theme.ACCENT}"), (" status", theme.MUTED)),
        title_align="left",
        border_style=theme.BORDER,
        box=box.ROUNDED,
        padding=(0, 1),
        expand=False,
    )
    console.print(panel)
    if not info.get("model_ok"):
        _muted("The model server isn't answering; run: kova setup")


def _restart_server(cfg) -> bool:
    """Stop and start a background server after an import; True if restarted."""
    pidfile = cfg.home / "server.pid"
    if not pidfile.exists():
        return False
    try:
        pid = int(pidfile.read_text().strip())
        os.kill(pid, 0)
    except (OSError, ValueError):
        pidfile.unlink(missing_ok=True)
        return False
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        return False
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            break
        time.sleep(0.05)
    pidfile.unlink(missing_ok=True)
    try:
        ensure_server(cfg)
    except KovaError as exc:
        console.print(Text(f"Imported, but the server did not restart: {exc}", style=theme.WARNING))
        return False
    return True


@app.command("export")
def export(path: str | None = typer.Argument(None, help="Zip path (default: ~/Kova-backup-YYYY-MM-DD.zip).")):
    """Write one zip with chats, memory notes, reminders and a safe config."""
    try:
        stats = backup.export_archive(load_config(), path)
    except (backup.BackupError, OSError) as exc:
        _fail(str(exc))
    _ok(f"Backup written to {stats['path']}")
    _muted(f"  {stats['chats']} chats · {stats['notes']} notes · {stats['reminders']} reminders")
    _muted("API keys were left out of config.toml; the server token is not included.")


@app.command("import")
def import_archive(
    path: Path = typer.Argument(..., exists=True, dir_okay=False, readable=True, help="A Kova backup zip."),
    merge: bool = typer.Option(True, "--merge/--replace",
                               help="Merge by default; --replace clears existing chats and Kova notes."),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip the replace confirmation."),
):
    """Restore a backup; merge is the default."""
    if not merge and not yes:
        if not typer.confirm("Replace all chats, Kova memory notes and reminders?", default=False):
            _muted("Cancelled.")
            return
    cfg = load_config()
    try:
        stats = backup.import_archive(cfg, path, merge=merge)
    except (backup.BackupError, OSError) as exc:
        _fail(str(exc))
    _ok(f"Imported {stats['chats']} chats · {stats['notes']} notes · {stats['reminders']} reminders")
    skipped = stats["chats_skipped"] + stats["notes_skipped"] + stats["reminders_skipped"]
    if skipped:
        _muted(f"Skipped {stats['chats_skipped']} chats, {stats['notes_skipped']} notes "
               f"and {stats['reminders_skipped']} reminders that already existed.")
    if _restart_server(cfg):
        _muted("Server restarted to pick up the import.")
    else:
        _muted("Chats, memory and reminders are read live from disk; "
               "restart a foreground server if you changed its config.")


# ---------------------------------------------------------------------------
# Command groups
# ---------------------------------------------------------------------------


def _list_models(client: KovaClient) -> None:
    active = client.health()["model"]
    models = client.models_list()
    if not models:
        _empty("No models configured.", "Run: kova setup to find a model server.")
        return
    table = _table(caption=f"{len(models)} configured · active: {active}")
    table.add_column("#", justify="right", style=theme.ACCENT_SOFT, no_wrap=True)
    table.add_column("Name", no_wrap=True)
    table.add_column("Model", no_wrap=True)
    table.add_column("Server", style=theme.MUTED, overflow="fold")
    for number, entry in enumerate(models, start=1):
        selected = entry["name"] == active
        badge = Text.assemble(
            ("● " if selected else "  ", theme.SUCCESS if selected else ""),
            (entry["name"], f"bold {theme.ACCENT}" if selected else ""),
        )
        table.add_row(str(number), badge, entry["model"], entry["base_url"])
    console.print(table)


@_model.command("list")
def model_list():
    _list_models(get_client())


@_model.command("add")
def model_add(
    name: str,
    address: str = typer.Argument(..., help="Server address: 192.168.1.50, homelab:8080, a URL, or openrouter."),
    model: str | None = typer.Argument(None, help="Model id; optional when the server has only one."),
    key: str | None = typer.Option(None, "--key"),
):
    """Add a model server. Loose addresses work; Kova finds the right URL."""
    from kova import server_finder

    try:
        found = server_finder.connect(address, api_key=key)
    except ValueError as exc:
        _fail(str(exc))
    if not found.answered:
        if model and "://" in address:
            # Offline right now but fully spelled out: save it as given.
            get_client().model_add(name, server_finder.normalize_base(address), model, key)
            _ok(f"Added model {name}. Its server didn't answer just now ({found.error}).")
            return
        _fail(found.error)
    if found.needs_key and not key:
        _fail(f"{found.base_url} wants an API key. Add it with --key.")
    if model is None:
        if len(found.models) == 1:
            model = found.models[0]
        elif not found.models:
            _fail(f"{found.base_url} lists no models. Name one: kova model add {name} {address} MODEL")
        else:
            for item in found.models[:30]:
                console.print(f"  {item}")
            _fail(f"{found.base_url} has {len(found.models)} models; pick one from the list above.",
                  hint=f"Then run: kova model add {name} {address} MODEL")
    get_client().model_add(name, found.base_url, model, key)
    _ok(f"Added model {name}: {model} at {found.base_url}.")


@_model.command("use")
def model_use(name: str):
    try:
        get_client().model_use(name)
    except KovaError as exc:
        if exc.status == 404:
            _fail(f'No model named "{name}".')
        raise
    _ok(f"{name} is now active.")


def _memory_table(notes: list[dict], *, caption: str | None = None) -> Table:
    table = _table(caption=caption)
    table.add_column("#", justify="right", style=theme.ACCENT_SOFT, no_wrap=True)
    table.add_column("Note", no_wrap=True, max_width=32, overflow="ellipsis")
    table.add_column("Tags", style=theme.MUTED, no_wrap=True, max_width=24, overflow="ellipsis")
    table.add_column("Updated", style=theme.MUTED, no_wrap=True)
    table.add_column("About", max_width=52, overflow="fold")
    for number, note in enumerate(notes, start=1):
        table.add_row(
            str(number),
            Text(note["title"], style="bold"),
            ", ".join(note.get("tags") or []) or "—",
            _relative_when(note.get("updated")),
            _clip(note.get("body", "")),
        )
    return table


@_memory.command("list")
def memory_list(q: str | None = typer.Argument(None)):
    notes = get_client().memory_list(q)
    if not notes:
        if q:
            _empty(f'No notes match "{q}".', "Try a different word, or run: kova memory list")
        else:
            _empty("No memory notes yet.", "Kova writes them as you chat, or add one with: kova memory add")
        return
    word = "note" if len(notes) == 1 else "notes"
    caption = f'{len(notes)} {word} · matching "{q}"' if q else f"{len(notes)} {word}"
    console.print(_memory_table(notes, caption=caption))


@_memory.command("search")
def memory_search(q: str):
    notes = get_client().memory_list(q)
    if not notes:
        _empty(f'No notes match "{q}".', "Try a different word, or run: kova memory list")
        return
    word = "note" if len(notes) == 1 else "notes"
    console.print(_memory_table(notes, caption=f'{len(notes)} {word} · matching "{q}"'))


@_memory.command("show")
def memory_show(slug: str):
    for note in get_client().memory_list():
        if note["slug"] == slug:
            console.print(Text(note["title"], style=f"bold {theme.ACCENT}"))
            console.print(Text(note["slug"], style=theme.MUTED))
            console.print()
            console.print(note["body"], markup=False, soft_wrap=True)
            return
    _fail(f'No note named "{slug}".')


@_memory.command("add")
def memory_add(title: str, body: str, tags: list[str] = typer.Option([], "--tags")):
    note = get_client().memory_add(title, body, tags)
    _ok(f"Saved {note['title']}.")


@_memory.command("rm")
def memory_rm(slug: str):
    if get_client().memory_delete(slug):
        _ok(f"Removed {slug}.")
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
    _ok(f"Memory folder set to {path}{suffix}.")


def _local_now() -> datetime:
    """Current local time; a separate helper so tests can freeze the clock."""
    return datetime.now().astimezone()


def _reminder_time(when: str, now: datetime) -> datetime:
    parsed = datetime.fromisoformat(when)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=now.tzinfo)
    return parsed


@_reminders.command("list")
def reminders_list():
    now = _local_now()
    reminders = get_client().reminders_list()
    if not reminders:
        _empty("Nothing scheduled.", 'Add one with: kova reminders add "in 10 min" "Stretch"')
        return
    table = _table(caption=f"{len(reminders)} upcoming")
    table.add_column("#", justify="right", style=theme.ACCENT_SOFT, no_wrap=True)
    table.add_column("When", no_wrap=True)
    table.add_column("In", style=theme.MUTED, no_wrap=True)
    table.add_column("Reminder", overflow="ellipsis")
    for reminder in reminders:
        try:
            when = _reminder_time(reminder["when"], now)
        except (TypeError, ValueError):
            table.add_row(str(reminder["id"]), Text(reminder["when"], style=theme.WARNING), "—",
                          _clip(reminder["text"]))
            continue
        table.add_row(str(reminder["id"]), when.strftime("%Y-%m-%d %H:%M"),
                      format_relative(when, now), _clip(reminder["text"]))
    console.print(table)


@_reminders.command("add")
def reminders_add(when: str, text: str):
    result = get_client().reminder_add(when, text)
    now = _local_now()
    try:
        parsed = _reminder_time(result["when"], now)
    except (TypeError, ValueError):
        console.print(Text(f"⏰ Reminder #{result['id']} · {result['when']}"))
        return
    console.print(Text.assemble(
        ("⏰ ", ""),
        (f"Reminder #{result['id']}", f"bold {theme.ACCENT}"),
        (f" · {format_when(parsed, now)} ({format_relative(parsed, now, spaces=True)})", ""),
    ))


@_reminders.command("rm")
def reminders_rm(rid: int):
    if get_client().reminder_delete(rid):
        _ok(f"Removed reminder #{rid}.")
    else:
        _fail(f"No reminder #{rid}.")


# ---------------------------------------------------------------------------
# Chat history
# ---------------------------------------------------------------------------

def _relative_when(iso: str | None) -> str:
    """Human-friendly age of an ISO timestamp, e.g. ``2h ago``."""
    if not iso:
        return ""
    try:
        stamp = datetime.fromisoformat(iso)
    except ValueError:
        return iso
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    seconds = max(0, int((datetime.now(timezone.utc) - stamp).total_seconds()))
    if seconds < 60:
        return "just now"
    if seconds < 3600:
        return f"{seconds // 60}m ago"
    if seconds < 86400:
        return f"{seconds // 3600}h ago"
    if seconds < 7 * 86400:
        return f"{seconds // 86400}d ago"
    return stamp.astimezone().strftime("%Y-%m-%d")


def _resolve_chat(client: KovaClient, ref: str) -> dict:
    """A chat by its display number or its id, or a friendly exit."""
    if ref.isdigit():
        chats = client.list_chats()
        number = int(ref)
        if 1 <= number <= len(chats):
            return chats[number - 1]
        _fail(f"No chat {ref}.")
    try:
        chat = client.get_chat(ref)
    except KovaError as exc:
        if exc.status == 404:
            _fail(f'No chat "{ref}".')
        raise
    if chat is None:
        _fail(f'No chat "{ref}".')
    return chat


def _resolve_chat_id(client: KovaClient, ref: str) -> str:
    return _resolve_chat(client, ref)["id"]


@_chats.command("list")
def chats_list():
    """List chats, newest first."""
    chats = get_client().list_chats()
    if not chats:
        _empty("No chats yet.", "Start one with: kova")
        return
    word = "conversation" if len(chats) == 1 else "conversations"
    table = _table(caption=f"{len(chats)} {word} · newest first")
    table.add_column("#", justify="right", style=theme.ACCENT_SOFT, no_wrap=True)
    table.add_column("Title", no_wrap=True, max_width=40, overflow="ellipsis")
    table.add_column("Mode", no_wrap=True)
    table.add_column("When", style=theme.MUTED, no_wrap=True)
    table.add_column("Messages", justify="right", style=theme.MUTED, no_wrap=True)
    for number, chat in enumerate(chats, start=1):
        title = Text(chat.get("title") or "(untitled)")
        if not chat.get("title"):
            title.stylize(theme.MUTED)
        mode = Text(chat.get("mode") or "work",
                    style=theme.USER if (chat.get("mode") or "work") == "chat" else theme.ACCENT_SOFT)
        table.add_row(
            str(number),
            title,
            mode,
            _relative_when(chat.get("last_message") or chat.get("created")),
            str(chat.get("message_count", 0)),
        )
    console.print(table)


@_chats.command("search")
def chats_search(words: list[str] = typer.Argument(..., help="Words to find in your chats.")):
    """Search inside user and assistant messages."""
    query = " ".join(words).strip()
    if not query:
        _fail("Give at least one word to search for.")
    client = get_client()
    results = client.search_chats(query)
    if not results:
        _empty(f'No chats match "{_clip(query, 40)}".', "Try fewer or different words.")
        return
    # Same numbering as ``kova chats`` and ``_resolve_chat``; hidden ask-chats
    # fall back to their id, which ``chats show <id>`` also accepts.
    numbers = {chat["id"]: number for number, chat in enumerate(client.list_chats(), start=1)}
    word = "match" if len(results) == 1 else "matches"
    table = _table(caption=f'{len(results)} {word} for "{_clip(query, 40)}"')
    table.add_column("#", justify="right", style=theme.ACCENT_SOFT, no_wrap=True)
    table.add_column("Title", no_wrap=True, max_width=32, overflow="ellipsis")
    table.add_column("When", style=theme.MUTED, no_wrap=True)
    table.add_column("Snippet", overflow="fold")
    for result in results:
        number = numbers.get(result["id"])
        label = str(number) if number is not None else result["id"]
        title = Text(result.get("title") or "(untitled)")
        if not result.get("title"):
            title.stylize(theme.MUTED)
        snippet = Text(result.get("snippet") or "")
        lowered = snippet.plain.casefold()
        for word in query.split():
            needle = word.casefold()
            start = 0
            while needle and (position := lowered.find(needle, start)) != -1:
                snippet.stylize(f"bold {theme.WARNING}", position, position + len(needle))
                start = position + len(needle)
        table.add_row(
            label,
            title,
            _relative_when(result.get("last_message") or result.get("created")),
            snippet,
        )
    console.print(table)


@_chats.command("export")
def chats_export(
    ref: str,
    as_json: bool = typer.Option(False, "--json", help="Print the chat as JSON."),
    as_md: bool = typer.Option(False, "--md", help="Print the chat as Markdown (default)."),
):
    """Print one chat to stdout. REF is a number from ``kova chats`` or a chat id."""
    if as_json and as_md:
        _fail("Choose either --md or --json.")
    client = get_client()
    chat = _resolve_chat(client, ref)
    full = chat if "messages" in chat else client.get_chat(chat["id"])
    if full is None:
        _fail(f'No chat "{ref}".')
    if as_json:
        sys.stdout.write(json.dumps(full, indent=2, ensure_ascii=False) + "\n")
    else:
        sys.stdout.write(backup.render_chat_markdown(full))


@_chats.command("show")
def chats_show(ref: str):
    """Print a conversation. REF is the number from ``kova chats`` or a chat id."""
    client = get_client()
    summary = _resolve_chat(client, ref)
    chat = summary if "messages" in summary else client.get_chat(summary["id"])
    console.print(Text(chat.get("title") or "Untitled chat", style=f"bold {theme.ACCENT}"))
    console.print(Text(chat["id"], style=theme.MUTED))
    for message in chat.get("messages", []):
        role, content = message.get("role"), message.get("content")
        if not content:
            continue
        if role == "user":
            console.print()
            console.print(Text("You", style=f"bold {theme.USER}"))
            console.print(content, markup=False, soft_wrap=True)
        elif role == "assistant":
            console.print()
            console.print(Text("Kova", style=f"bold {theme.ACCENT}"))
            console.print(Markdown(content))


@_chats.command("rename")
def chats_rename(ref: str, title: str):
    """Rename a chat. REF is the number from ``kova chats`` or a chat id."""
    title = title.strip()
    if not title:
        _fail("Title cannot be empty.")
    client = get_client()
    chat = client.rename_chat(_resolve_chat_id(client, ref), title)
    _ok(f'Renamed chat to "{chat["title"]}".')


@_chats.command("rm")
def chats_rm(ref: str, yes: bool = typer.Option(False, "--yes", "-y", help="Skip the confirmation.")):
    """Delete a chat. REF is the number from ``kova chats`` or a chat id."""
    client = get_client()
    chat = _resolve_chat(client, ref)
    title = chat.get("title") or "Untitled chat"
    if not yes and not typer.confirm(f'Delete "{title}"?', default=False):
        _muted("Cancelled.")
        return
    if client.delete_chat(chat["id"]):
        _ok(f'Deleted chat "{title}".')
    else:
        _fail(f'No chat "{ref}".')
