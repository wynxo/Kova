"""Kova's command-line interface.

The CLI is a thin HTTP client of ``kova serve``. ``get_client`` starts the
server on demand; the commands render chat replies with Rich.
"""

from __future__ import annotations

import json
import math
import os
import re
import signal
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from difflib import get_close_matches
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import typer
from rich import box
from rich.console import Console
from rich.markdown import Markdown
from rich.table import Table
from rich.text import Text
from typer.core import TyperGroup

from kova import __version__, autostart, backup, lifecycle, onboarding, providers, theme, egress, ui_prefs
from kova.client import KovaClient, KovaError, ensure_server
from kova.config import (
    CONFIG_SCHEMA,
    ensure_token,
    kova_home,
    load_config,
    save_config,
    unknown_config_keys,
)
from kova.modes import MODES
from kova.persona import TONES, Persona
from kova.privacy import status_text as privacy_status_text
from kova.tools.reminders import format_relative, format_when
from kova.tui import brand
from kova.tui.keymap import DEFAULT_KEYS, Keymap
from kova.tui.style import colour
from kova.ui import _run_chat


class _CliConsole(Console):
    @property
    def is_terminal(self) -> bool:
        # A redirected stream stays plain, even when CI exports FORCE_COLOR.
        return bool(getattr(self.file, "isatty", lambda: False)()) and super().is_terminal


console = _CliConsole()

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
    if "no job" in text:
        return "Run: kova jobs list"
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
    if "run kova doctor" in text or "could not establish the command sandbox" in text:
        return "Run: kova doctor  (README: Troubleshoot an install)"
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
    console.print(Text(message, style=colour('MUTED')))


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


_HELP_EPILOG = ("Start a chat with [bold]kova[/bold]. "
                "Add [bold]--help[/bold] to any command for its flags.\n"
                "Configuration keys: [bold]docs/CONFIG.md[/bold]")


class _KovaGroup(TyperGroup):
    def main(self, *args, **kwargs):
        from typer import rich_utils

        # Windows pipes otherwise default to a legacy code page that cannot
        # encode the wordmark. CLI output is UTF-8, like Kova's data files.
        if sys.platform == "win32" and hasattr(sys.stdout, "reconfigure"):
            sys.stdout.reconfigure(encoding="utf-8")

        # Typer caches CI color flags at import. Decide against the current
        # stream instead (also covers CliRunner and every nested command).
        previous = rich_utils.FORCE_TERMINAL
        rich_utils.FORCE_TERMINAL = console.is_terminal
        try:
            brand.style_help(self)
            return super().main(*args, **kwargs)
        finally:
            rich_utils.FORCE_TERMINAL = previous

    def format_help(self, ctx, formatter):
        if ctx.parent is None:
            self.epilog = f"[{colour('MUTED')}]{_HELP_EPILOG}[/]"
            console.print()
            console.print(brand.wordmark(__version__))
        return super().format_help(ctx, formatter)

    def invoke(self, ctx):
        try:
            return super().invoke(ctx)
        except (KovaError, onboarding.SetupError, egress.LocalOnlyError) as exc:
            _fail(str(exc))
        except (ValueError, TypeError, KeyError, OSError):
            _fail('Cannot read configuration or local files. Check config.toml and directory permissions, then run kova doctor.')


app = typer.Typer(
    help="Kova: your own private AI that lives on your computer.",
    epilog=_HELP_EPILOG,
    no_args_is_help=False,
    cls=_KovaGroup,
)


def _version(value: bool) -> None:
    if value:
        console.print(Text.assemble(
            brand.gradient_text("Kova"),
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
    incognito: bool = typer.Option(False, "--incognito", help="Keep this chat in memory only; no tools or memories."),
):
    """Chat with Kova; replies stream live. Type /exit to leave."""
    ctx.with_resource(egress.scope(config_loader=load_config))
    if ctx.invoked_subcommand is None:
        _start_chat(new, chat, resume, no_think, mode, no_web, **({"incognito": True} if incognito else {}))


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

_jobs = typer.Typer(no_args_is_help=True, help="Manage scheduled recurring jobs.")
app.add_typer(_jobs, name="jobs")

_plugins = typer.Typer(no_args_is_help=True, help="Install plugin bundles of skills, commands and MCP servers.")
app.add_typer(_plugins, name="plugin")

_catalog = typer.Typer(no_args_is_help=True, help="Browse and install plugins and skills from the extensions catalog.")
app.add_typer(_catalog, name="catalog")

_skills = typer.Typer(no_args_is_help=True, help="See, read and delete the skills Kova uses.")
app.add_typer(_skills, name="skills")

_chats = typer.Typer(no_args_is_help=False, help="Browse and manage chat history.")
app.add_typer(_chats, name="chats")

_prefs = typer.Typer(no_args_is_help=True, help="Export and import UI preferences.")
app.add_typer(_prefs, name="prefs")


@_chats.callback(invoke_without_command=True)
def chats_root(ctx: typer.Context):
    """List chats, newest first."""
    if ctx.invoked_subcommand is None:
        chats_list()


def get_client() -> KovaClient:
    """Return a client, starting the server in the background if it is down."""
    return ensure_server(load_config())


def _reload_mcp() -> None:
    """Reload the running MCP manager if the server is up, without starting it.

    A plugin enable/disable/install/remove only needs the manager to rebuild its
    tool list; when the server is down the plugin servers load there on the next
    start instead, so a reload is not needed here.
    """
    cfg = load_config()
    client = KovaClient(f"http://127.0.0.1:{cfg.port}", ensure_token(cfg.home))
    try:
        client.mcp_reload()
    except KovaError:
        pass
    finally:
        client.http.close()


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


def _reasoning_error(model: str | None) -> str | None:
    """Why ``--think`` cannot show anything here, or ``None`` when it can.

    Reads the provider's declared capabilities, so a provider with no reasoning
    stream is refused up front instead of printing nothing at all.
    """
    cfg = load_config()
    try:
        entry = next((m for m in cfg.models if m.name == model), None) if model else cfg.active_model()
    except KeyError:
        return None  # nothing configured: the server reports that, not this check
    provider = entry._provider if entry is not None else None
    if provider is None or providers.supports(provider, 'reasoning'):
        return None
    return f"{provider.name} does not stream reasoning"


@app.command("ask")
def ask(
    text: str | None = typer.Argument(None, help="The question. Optional if you pipe text in."),
    files: list[Path] = typer.Option([], "--file", "-f", exists=True, dir_okay=False, readable=True,
                                     help="Attach a file's contents (repeatable)."),
    model: str | None = typer.Option(None, "--model", "-m",
                                     help="Use this model for this question only."),
    incognito: bool = typer.Option(False, "--incognito", help="Do not save this question or remember it."),
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
    if incognito and chat_id:
        _ask_error("Use --incognito without --chat to start a private one-off question.")
    if incognito:
        _stderr.print("Incognito: not saved on this PC. Your provider still receives messages.", markup=False)
    cleanup_id = None
    if think:
        reason = _reasoning_error(model)
        if reason:
            _ask_error(f"{reason}. Use a reasoning model, or drop --think.")
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
            chat_id = client.create_chat("Incognito" if incognito else title, mode=mode,
                                         **({"ephemeral": True} if incognito else {}))["id"]
            if incognito:
                cleanup_id = chat_id
        elif mode is not None:
            client.update_chat(chat_id, mode=mode)
        pieces: list[str] = []
        tools: list[str] = []
        used_model = model
        usage = None
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
            elif kind == "privacy":
                if not as_json:
                    _stderr.print(privacy_status_text(event), style=theme.MUTED, markup=False)
            elif kind == "tool_call":
                tools.append(event["name"])
            elif kind in {"approval", "question"}:
                # `ask` is the noninteractive/script interface. Do not silently
                # leave it waiting ten minutes for an approval it cannot show.
                guidance = "This action needs approval. Continue in interactive Kova chat to approve it."
                client.answer_approval(chat_id, event["id"], "deny", guidance)
                _stderr.print(guidance, style=theme.WARNING, markup=False)
            elif kind == "error":
                if pieces and not as_json:
                    print()
                _ask_error(event["message"])
            elif kind == "done":
                used_model = event.get("model", used_model)
                usage = event.get("usage")
                break
    except KovaError as exc:
        _ask_error(str(exc))
    finally:
        if cleanup_id is not None:
            client.delete_chat(cleanup_id)
    if reasoning:
        _stderr.print()
    if as_json:
        print(json.dumps({"answer": "".join(pieces), "chat_id": chat_id,
                          "model": used_model, "tools": tools,
                          **({"usage": usage} if usage else {}),
                          **({"incognito": True} if incognito else {})}, ensure_ascii=False))
    else:
        print()


@app.command("compare")
def compare(
    text: str | None = typer.Argument(None, help="The question. Optional if you pipe text in."),
    models: str | None = typer.Option(None, "--models", "-m",
                                      help="Comma-separated model names; default is every configured model."),
    as_json: bool = typer.Option(False, "--json", help="Print a JSON object for scripts."),
):
    """Run one prompt on several models at once and show the answers side by side."""
    prompt = _ask_prompt(text, _piped_stdin(), [])
    wanted = [part.strip() for part in models.split(",")] if models else None
    if wanted is not None:
        wanted = [part for part in wanted if part]
        if not wanted:
            _fail("Give at least one model name, for example: kova compare --models lab,cloud \"hi\"")
    try:
        client = get_client()
        result = client.compare(prompt, wanted)
    except KovaError as exc:
        _ask_error(str(exc))
    if as_json:
        print(json.dumps(result, ensure_ascii=False))
        return
    for entry in result.get("results", []):
        name = entry.get("model", "?")
        ms = entry.get("ms", 0)
        error = entry.get("error")
        if error:
            console.print(f"## {name} (failed in {ms} ms: {error})", style=theme.ERROR)
        else:
            console.print(f"## {name} ({ms} ms)", style=theme.ACCENT)
            console.print(entry.get("text", ""), markup=False)
        console.print()


@app.command(hidden=True)
def chat(
    new: bool = typer.Option(False, "--new", help="Start a fresh chat instead of the latest."),
    chat: str | None = typer.Option(None, "--chat", help="Continue an existing chat by ID."),
    resume: str | None = typer.Option(None, "--resume", help="Continue a chat by its number from: kova chats"),
    no_think: bool = typer.Option(False, "--no-think", help="Hide the model's reasoning."),
    mode: str | None = typer.Option(None, "--mode", help="Start the chat in this mode: chat or work."),
    no_web: bool = typer.Option(False, "--no-web", help="Turn web search off for this chat."),
    incognito: bool = typer.Option(False, "--incognito", help="Keep this chat in memory only; no tools or memories."),
):
    """Old name for plain ``kova``; kept so existing habits and scripts still work."""
    _start_chat(new, chat, resume, no_think, mode, no_web, **({"incognito": True} if incognito else {}))


def _start_chat(new: bool, chat: str | None, resume: str | None, no_think: bool,
                mode: str | None, no_web: bool, incognito: bool = False) -> None:
    _check_mode(mode)
    if incognito and (chat or resume):
        _fail("Use --incognito for a new chat, without --chat or --resume.")
    cfg = load_config()
    if sys.stdin.isatty() and onboarding.needs_setup(cfg):
        console.print()
        console.print(brand.wordmark(__version__))
        console.print()
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
              web=False if no_web else None, console=console,
              **({"incognito": True} if incognito else {}))


@app.command()
def setup(
    url: str | None = typer.Option(None, "--url", help="Model server URL, for example http://192.168.1.20:8080."),
    model: str | None = typer.Option(None, "--model", help="Model name to use."),
    name: str | None = typer.Option(None, "--name", help="Name to save this model under."),
    key: str | None = typer.Option(None, "--key", help="API key, if the server needs one."),
    yes: bool = typer.Option(False, "--yes", "-y", help="Use defaults for scripted setup (never bypasses credential validation)."),
):
    """Connect a provider and choose models. No scan or test chat."""
    if not url and sys.stdin.isatty() and console.is_terminal:
        brand.setup_banner(console, __version__)
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
    cfg = load_config()
    from kova.managed_engine import stop_owned
    from kova.local_models import LocalModelError
    try:
        stop_owned(cfg.home)
    except LocalModelError as exc:
        _fail(str(exc))
    pidfile = cfg.home / "server.pid"
    if not pidfile.exists():
        client = KovaClient(f"http://127.0.0.1:{cfg.port}", ensure_token(cfg.home))
        try:
            client.health(probe=False)
        except KovaError as exc:
            if exc.status is not None:
                raise
        finally:
            client.http.close()
        _muted("No running server recorded.")
        return
    try:
        pid = int(pidfile.read_text().strip())
    except ValueError:
        pidfile.unlink(missing_ok=True)
        return
    try:
        if lifecycle.verified_server(pid, pidfile.parent) is None:
            pidfile.unlink(missing_ok=True)
            _muted("Stale or unverifiable server PID; no process was signalled.")
            return
        lifecycle.stop_server(pidfile.parent)
        _ok("Server stopped.")
    except ProcessLookupError:
        pidfile.unlink(missing_ok=True)
        _muted("Server was not running.")
    except (OSError, lifecycle.LifecycleError) as exc:
        _error_line(f"Could not stop the server: {exc}")


def _status_rows(cfg, info: dict | None, *, notes: int | None, reminders: int | None,
                 spend: str | None = None) -> Table:
    """The two-column label/value grid inside the status card."""
    grid = Table.grid(padding=(0, 2))
    grid.add_column(no_wrap=True)
    grid.add_column(no_wrap=True)

    def dot(style: str) -> Text:
        return Text("●", style=style)

    if info is None:
        server = Text.assemble(dot(colour('ERROR')), ("  down", ""), ("  ·  is the server running?", colour('MUTED')))
        model = Text(cfg.active or "none", style=colour('MUTED'))
    else:
        server = Text.assemble(dot(colour('SUCCESS')), ("  up", ""), (f"  ·  v{info['version']}", colour('MUTED')))
        entry = next((m for m in info.get("models", []) if m.get("name") == info.get("model")), None)
        name = f"{info['model']}" + (f" · {entry['model']}" if entry and entry.get("model") else "")
        model = Text.assemble(
            dot(colour('SUCCESS') if info.get("model_ok") else colour('WARNING')),
            ("  " + name, ""),
            ("  ·  reachable" if info.get("model_ok") else "  ·  not reachable", colour('SUCCESS') if info.get("model_ok") else colour('WARNING')),
        )
    note_word = "note" if notes == 1 else "notes"
    memory = Text(f"{notes} {note_word}" if notes is not None else "—", style=colour('MUTED'))
    upcoming = Text(f"{reminders} upcoming" if reminders is not None else "—", style=colour('MUTED'))
    rows = [("Server", server), ("Model", model), ("Memory", memory), ("Reminders", upcoming)]
    if spend is not None:
        rows.append(("Today", Text(spend, style=colour('MUTED'))))
    for label, value in rows:
        grid.add_row(Text(label, style=colour('MUTED')), value)
    if (info or {}).get('local_only', cfg.privacy_local_only):
        grid.add_row(Text('Privacy', style=colour('MUTED')), Text('Local only · loopback connections only', style=colour('SUCCESS')))
    return grid


def _spend_line(bucket: dict) -> str:
    """``3 answers · 12k tokens · ~$0.04`` for one usage bucket."""
    from kova.usage import money, short_tokens
    answers = bucket.get("answers", 0)
    tokens = (bucket.get("prompt_tokens") or 0) + (bucket.get("completion_tokens") or 0)
    parts = [f"{answers} answer{'' if answers == 1 else 's'}", f"{short_tokens(tokens)} tokens",
             "~" + money(bucket.get("cost_usd") or 0)]
    if bucket.get("unpriced"):
        parts.append(f"{bucket['unpriced']} unpriced")
    return " · ".join(parts)


@app.command()
def privacy(
    days: int = typer.Option(30, '--days', min=1, max=365, help='Report the last N days.'),
    as_json: bool = typer.Option(False, '--json', help='Export metadata as JSON.'),
    chat_id: str | None = typer.Option(None, '--chat', help='Filter by chat ID.'),
    local_only: str | None = typer.Option(None, '--local-only', help='Set on or off, immediately.'),
):
    """See which destinations Kova contacted and control Local only."""
    if local_only not in (None, 'on', 'off'):
        _fail('Choose --local-only on or off.')
    client = get_client()
    if local_only is not None:
        client.privacy_update(local_only == 'on')
    report = client.privacy_report(days, chat_id)
    if as_json:
        sys.stdout.write(json.dumps(report, indent=2, ensure_ascii=False) + '\n')
        return
    _muted('Local only: ' + ('on' if report['local_only'] else 'off'))
    _muted(f"{report['requests']} requests · {report['bytes_out']} body bytes prepared · {report['bytes_in']} received")
    table = _table(caption='HTTP body sizes; failed requests may not send. Tokens are provider-reported, not estimates.')
    for heading in ('Day', 'Provider', 'Host', 'Kind', 'Calls', 'Out', 'In', 'Tokens in/out'):
        table.add_column(heading)
    for row in report['groups']:
        tokens = f"{row['tokens_in'] if row['tokens_in'] is not None else '?'} / {row['tokens_out'] if row['tokens_out'] is not None else '?'}"
        table.add_row(row['day'], row['provider'] or '—', row['host'], row['kind'],
                      str(row['requests']), str(row['bytes_out']), str(row['bytes_in']), tokens)
    console.print(table)
    if not report['requests']:
        _muted('No recorded requests in this period. Older traffic is not reconstructed.')


@app.command()
def status():
    """Report whether the server is up, its version and active model."""
    cfg = load_config()
    client = KovaClient(f"http://127.0.0.1:{cfg.port}", ensure_token(cfg.home))
    try:
        info = client.health(probe=True)
    except KovaError as exc:
        if exc.status is not None:
            raise
        console.print(brand.card(_status_rows(cfg, None, notes=None, reminders=None),
                                 "Kova status", tone=theme.ERROR))
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
    try:
        spend = _spend_line(client.usage(1)["today"])
    except (KovaError, KeyError, TypeError, ValueError):
        spend = None

    console.print(brand.card(_status_rows(cfg, info, notes=notes, reminders=reminders, spend=spend),
                             "Kova status",
                             tone=theme.ACCENT if info.get("model_ok") else theme.WARNING))
    if not info.get("model_ok"):
        _muted("The model server isn't answering; run: kova setup")


@app.command("usage")
def usage_command(
    days: int = typer.Option(30, "--days", min=1, max=366, help="How many days back to count."),
    as_json: bool = typer.Option(False, "--json", help="Print one JSON object for scripts."),
):
    """Tokens, estimated cost and speed of your answers, by day and by model."""
    from kova.usage import money, short_tokens
    data = get_client().usage(days)
    if as_json:
        print(json.dumps(data, ensure_ascii=False))
        return
    total = data["total"]
    if not total["answers"]:
        _empty(f"No answers in the last {data['days']} day(s).", "Chat with kova, then check again.")
        return
    table = _table(caption=f"Last {data['days']} day(s) · prices checked {data['prices_checked']}; "
                           "set your own in [pricing] in config.toml")
    table.add_column("Model", no_wrap=True)
    table.add_column("Answers", justify="right")
    table.add_column("In", justify="right")
    table.add_column("Out", justify="right")
    table.add_column("Cost", justify="right")
    table.add_column("Speed", justify="right")
    for row in data["by_model"]:
        cost = "~" + money(row["cost_usd"]) if not row["unpriced"] else (
            "price unknown" if row["unpriced"] == row["answers"] else "~" + money(row["cost_usd"]) + "+")
        speed = f"{row['tokens_per_second']:g} tok/s" if row.get("tokens_per_second") else "—"
        table.add_row(Text(row["model"], style="bold"), str(row["answers"]),
                      short_tokens(row["prompt_tokens"]), short_tokens(row["completion_tokens"]), cost, speed)
    console.print(table)
    console.print(Text.assemble(("Today       ", theme.MUTED), _spend_line(data["today"])))
    console.print(Text.assemble(("This month  ", theme.MUTED), _spend_line(data["month"])))
    budget = data.get("budget") or {}
    if budget.get("monthly_usd"):
        line = f"Budget      ~{money(budget['spent_usd'])} of {money(budget['monthly_usd'])} this month"
        console.print(Text(line, style=theme.WARNING if budget.get("warning") else theme.MUTED))
    if total["estimated"]:
        _muted("Some servers sent no token counts; those answers are estimated (about 4 characters a token).")


_autostart = typer.Typer(no_args_is_help=False, help="Start Kova's server at login.")
app.add_typer(_autostart, name="autostart")


@_autostart.callback(invoke_without_command=True)
def autostart_root(ctx: typer.Context):
    """Show whether Kova starts at login."""
    if ctx.invoked_subcommand is None:
        autostart_status()


@_autostart.command("on")
def autostart_on():
    """Start Kova's server at login; no administrator rights needed."""
    try:
        state = autostart.enable()
    except (autostart.AutostartError, OSError) as exc:
        _fail(str(exc))
    _ok(f"Start at login is on ({state.label}).")
    if state.path:
        _muted(f"  {state.path}")
    else:
        _muted("  Kova's per-user HKCU Run entry was added.")
    _muted("  It starts with your next login. Kova uses a little memory while idle; "
           "models load only when you chat.")


@_autostart.command("off")
def autostart_off():
    """Stop starting Kova at login; removes only Kova's own entry."""
    try:
        autostart.disable()
    except (autostart.AutostartError, OSError) as exc:
        _fail(str(exc))
    _ok("Start at login is off. Kova's own entry was removed.")


@_autostart.command("status")
def autostart_status():
    """Report whether the per-platform login entry is installed and current."""
    try:
        state = autostart.status()
    except (autostart.AutostartError, OSError) as exc:
        _fail(str(exc))
    line = autostart.describe(state)
    if state.stale:
        console.print(Text.assemble(("! ", f"bold {theme.WARNING}"), (line, theme.WARNING)))
    elif state.installed:
        _ok(line)
    else:
        _muted(line)
    if state.path:
        _muted(f"  {state.path}")
    if state.stale:
        _muted("  Run: kova autostart on to update it. Nothing was changed.")


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
        process = lifecycle.verified_server(pid, cfg.home)
        if process is None:
            pidfile.unlink(missing_ok=True)
            return False
        process.send_signal(signal.SIGTERM)
    except (OSError, lifecycle.psutil.Error):
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


@_prefs.command("export")
def prefs_export(path: str | None = typer.Argument(
        None, help="JSON path (default: ~/Kova-ui-preferences-YYYY-MM-DD.json).")):
    """Save every UI preference to one validated JSON file."""
    try:
        target = ui_prefs.write_file(get_client().preferences(), path)
    except (ui_prefs.UiPrefsError, OSError) as exc:
        _fail(str(exc))
    _ok(f"UI preferences written to {target}")
    _muted(f"  Move them with: kova prefs import {target.name}")


@_prefs.command("import")
def prefs_import(
    path: Path = typer.Argument(..., exists=True, dir_okay=False, readable=True,
                                help="A preferences JSON file from `kova prefs export`."),
):
    """Restore UI preferences, refusing a file that does not validate."""
    try:
        values = ui_prefs.read_file(path)
    except (ui_prefs.UiPrefsError, OSError) as exc:
        _fail(str(exc))
    try:
        saved = get_client().set_preferences(values)
    except KovaError as exc:
        _fail(str(exc))
    _ok(f"Restored {len(saved)} UI preferences")
    _muted("  " + " · ".join(f"{name} {value}" for name, value in sorted(saved.items())))


# ---------------------------------------------------------------------------
# Command groups
# ---------------------------------------------------------------------------


def _list_models(client: KovaClient) -> None:
    active = client.health(probe=False)["model"]
    models = client.models_list()
    if not models:
        _empty("No models configured.", "Run: kova setup to find a model server.")
        return
    table = _table(caption=f"{len(models)} configured · active: {active}")
    table.add_column("#", justify="right", style=theme.ACCENT_SOFT, no_wrap=True)
    table.add_column("Name", no_wrap=True)
    table.add_column("Model", no_wrap=True)
    table.add_column("Provider", style=theme.MUTED, overflow="fold")
    table.add_column("Server", style=theme.MUTED, overflow="fold")
    for number, entry in enumerate(sorted(models, key=lambda m: (m.get("provider_name", ""), m["name"])), start=1):
        selected = entry["name"] == active
        badge = Text.assemble(
            ("● " if selected else "  ", theme.SUCCESS if selected else ""),
            (entry["name"], f"bold {theme.ACCENT}" if selected else ""),
        )
        table.add_row(str(number), badge, entry["model"], entry.get("provider_name", "Custom"), entry["base_url"])
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


@_model.command("remove-local")
def model_remove_local(provider: str):
    """Remove a managed local model's weights and connection (not an engine install)."""
    result = get_client().local_model_remove(provider)
    _ok(result['message'])


_mcp = typer.Typer(no_args_is_help=True, help="Use the tools of MCP (Model Context Protocol) servers.")
app.add_typer(_mcp, name="mcp")

_MCP_STATE = {"connected": theme.SUCCESS, "connecting": theme.WARNING, "error": theme.ERROR, "off": theme.MUTED}


def _pairs(values: list[str], label: str) -> dict[str, str]:
    """``KEY=VALUE`` options as a dict; a malformed one is an error, not a guess."""
    result = {}
    for value in values:
        key, sep, item = value.partition("=")
        if not sep or not key.strip():
            _fail(f"{label} must look like NAME=VALUE (got {value.split('=')[0]!r}).")
        result[key.strip()] = item
    return result


def _mcp_tools(row: dict) -> None:
    if row["error"]:
        _error_line(row["error"])
    if not row["tools"]:
        if row["state"] == "connected":
            _muted(f"{row['name']} offers no tools.")
        return
    table = _table(caption=f"{len(row['tools'])} tools · Kova asks before each call unless always allowed")
    table.add_column("Tool", no_wrap=True)
    table.add_column("Kova name", style=theme.MUTED, no_wrap=True)
    table.add_column("On", no_wrap=True)
    table.add_column("Description", style=theme.MUTED, overflow="fold", max_width=60)
    for tool in row["tools"]:
        table.add_row(tool["name"], tool["kova_name"], "yes" if tool["enabled"] else "no",
                      _clip(tool["description"].split("\n")[0], 60))
    console.print(table)


@_mcp.command("list")
def mcp_list():
    """Show MCP servers, whether they are connected, and their tools."""
    rows = get_client().mcp_list()
    if not rows:
        _empty("No MCP servers yet.",
               "Add one: kova mcp add NAME -- COMMAND [ARGS...]   or   kova mcp add NAME --url URL")
        return
    table = _table(caption=f"{len(rows)} server(s) · docs/CONFIG.md#mcp-servers")
    table.add_column("Server", no_wrap=True)
    table.add_column("Status", no_wrap=True)
    table.add_column("Tools", justify="right", no_wrap=True)
    table.add_column("Runs", style=theme.MUTED, overflow="fold")
    table.add_column("Source", style=theme.MUTED, no_wrap=True)
    for row in rows:
        enabled = sum(1 for tool in row["tools"] if tool["enabled"])
        tools = f"{enabled}/{len(row['tools'])}" if row["tools"] else "-"
        table.add_row(row["name"], Text(row["state"], style=_MCP_STATE.get(row["state"], "")), tools,
                      f"{row['transport']} · {_clip(row['target'], 70)}",
                      f"plugin: {row['plugin']}" if row.get("plugin") else "")
    console.print(table)
    for row in rows:
        if row["error"]:
            _error_line(row["error"])


@_mcp.command("add")
def mcp_add(
    name: str = typer.Argument(..., help="Short name; tools appear as NAME__tool."),
    command: list[str] = typer.Argument(None, help="Command and arguments of a stdio server (after --)."),
    url: str | None = typer.Option(None, "--url", help="Address of a streamable HTTP server."),
    env: list[str] = typer.Option([], "--env", help="NAME=VALUE for a stdio server's environment."),
    header: list[str] = typer.Option([], "--header", help="NAME=VALUE HTTP header, e.g. Authorization=Bearer …"),
    sandbox: bool = typer.Option(False, "--sandbox", help="Confine a stdio server's writes to its own folder."),
):
    """Add an MCP server and connect to it.

    kova mcp add files -- npx -y @modelcontextprotocol/server-filesystem ~/projects
    kova mcp add docs --url https://example.com/mcp
    """
    command = list(command or [])
    if bool(command) == bool(url):
        _fail("Give either a command (after --) or --url, not both.",
              hint="kova mcp add NAME -- COMMAND [ARGS...]   or   kova mcp add NAME --url URL")
    server = {"name": name, "command": command[0] if command else "", "args": command[1:],
              "env": _pairs(env, "--env"), "url": url or "", "headers": _pairs(header, "--header"),
              "sandbox": sandbox}
    row = get_client().mcp_add(server)
    if row["state"] == "connected":
        _ok(f"Added {name}: {len(row['tools'])} tool(s). Kova asks before each call.")
        _mcp_tools(row)
    else:
        _ok(f"Added {name}, but it is not connected yet.")
        if row["error"]:
            _error_line(row["error"])
        _muted(f"Fix it, then run: kova mcp test {name}")


@_mcp.command("remove")
def mcp_remove(name: str):
    """Stop an MCP server and forget it (and its always-allow rules)."""
    try:
        result = get_client().mcp_remove(name)
    except KovaError as exc:
        if exc.status == 404:
            _fail(f'No MCP server named "{name}".', hint="See them with: kova mcp list")
        raise
    if result.get("plugin"):
        _fail(f'{name} comes from the {result["plugin"]} plugin, so it cannot be removed here.',
              hint=f"Use: kova plugin disable {result['plugin']} (or kova plugin remove {result['plugin']})")
    _ok(f"Removed {name}.")


@_mcp.command("test")
def mcp_test(name: str):
    """Reconnect an MCP server now and list the tools it offers."""
    try:
        row = get_client().mcp_test(name)
    except KovaError as exc:
        if exc.status == 404:
            _fail(f'No MCP server named "{name}".', hint="See them with: kova mcp list")
        raise
    if row["state"] == "off":
        _fail(f"{name} is switched off.", hint="Turn it on in Settings › MCP servers, or set enabled = true.")
    if row["state"] != "connected":
        _fail(row["error"] or f"{name} did not connect.")
    info = row.get("server_info") or {}
    label = " ".join(part for part in (info.get("name"), info.get("version")) if part)
    _ok(f"{name} is working" + (f" ({label})" if label else "") + ".")
    _mcp_tools(row)


def _memory_table(notes: list[dict], *, caption: str | None = None) -> Table:
    table = _table(caption=caption)
    table.add_column("#", justify="right", style=theme.ACCENT_SOFT, no_wrap=True)
    table.add_column("Note", no_wrap=True, max_width=32, overflow="ellipsis")
    table.add_column("Tags", style=theme.MUTED, no_wrap=True, max_width=24, overflow="ellipsis")
    table.add_column("From", style=theme.MUTED, no_wrap=True)
    table.add_column("Updated", style=theme.MUTED, no_wrap=True)
    table.add_column("About", max_width=52, overflow="fold")
    for number, note in enumerate(notes, start=1):
        table.add_row(
            str(number),
            Text(note["title"], style="bold"),
            ", ".join(note.get("tags") or []) or "—",
            _origin_label(note),
            _relative_when(note.get("updated")),
            _clip(note.get("body", "")),
        )
    return table


def _origin_label(note: dict) -> str:
    """Where a memory came from: the user's own words, or Kova's own note."""
    origin = note.get("origin") or "user"
    return "you said" if origin == "user" else "Kova noted"


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
            console.print(Text(
                f"{_origin_label(note)}"
                + (f" · from chat {note['source']}" if note.get("source") else "")
                + (f" · first recorded {_relative_when(note.get('created'))}"
                   if note.get("created") else "")
                + f" · updated {_relative_when(note.get('updated'))}",
                style=theme.MUTED))
            console.print()
            console.print(note["body"], markup=False, soft_wrap=True)
            return
    _fail(f'No note named "{slug}".')


@_memory.command("add")
def memory_add(title: str, body: str, tags: list[str] = typer.Option([], "--tags")):
    note = get_client().memory_add(title, body, tags)
    _ok(f"Saved {note['title']}.")


@_memory.command("edit")
def memory_edit(
    slug: str,
    body: str | None = typer.Option(None, "--body", help="Replacement text for the note."),
    title: str | None = typer.Option(None, "--title", help="New title for the note."),
    tag: list[str] = typer.Option([], "--tag", help="Replace the note's tags; repeatable."),
):
    """Rewrite one note in place; the slug and where it came from are kept."""
    if body is None and title is None and not tag:
        _fail("Nothing to change.", hint=f"Usage: kova memory edit {slug} --body \"new text\"")
    note = get_client().memory_edit(slug, title=title, body=body, tags=tag or None)
    _ok(f"Updated {note['title']}.")


@_memory.command("rm")
def memory_rm(slug: str):
    if get_client().memory_delete(slug):
        _ok(f"Removed {slug}.")
    else:
        _fail(f'No note named "{slug}".')


@_memory.command("forget")
def memory_forget(slug: str):
    """Delete one note; the same as `memory rm`, named for the review notices."""
    if get_client().memory_forget(slug):
        _ok(f"Forgot {slug}.")
    else:
        _fail(f'No note named "{slug}".')


@_memory.command("review")
def memory_review(
    state: str = typer.Argument(..., help="on or off"),
    every: int | None = typer.Option(None, "--every", min=1,
                                     help="Review after this many user turns."),
):
    """Turn post-turn memory review on or off; it is off until you opt in."""
    if state not in ("on", "off"):
        _fail(f'Unknown state "{state}".', hint="Usage: kova memory review on|off")
    try:
        settings = get_client().memory_settings(review=state == "on", review_every=every)
    except KovaError as exc:
        if exc.status is not None:
            raise
        cfg = load_config()
        cfg.memory_review = state == "on"
        if every:
            cfg.memory_review_every = every
        save_config(cfg)
        settings = {"review": cfg.memory_review, "review_every": cfg.memory_review_every}
    if settings.get("review"):
        _ok(f"Memory review is on (every {settings.get('review_every')} turns).")
    else:
        _muted("Memory review is off.")


@_memory.command("nightly")
def memory_nightly(
    state: str = typer.Argument(..., help="on or off"),
    hour: int | None = typer.Option(None, "--hour", min=0, max=23,
                                    help="Earliest local hour it may run."),
):
    """Turn nightly consolidation on or off; it is off until you opt in."""
    if state not in ("on", "off"):
        _fail(f'Unknown state "{state}".', hint="Usage: kova memory nightly on|off")
    try:
        settings = get_client().memory_settings(consolidate=state == "on",
                                               consolidate_hour=hour)
    except KovaError as exc:
        if exc.status is not None:
            raise
        cfg = load_config()
        cfg.memory_consolidate = state == "on"
        if hour is not None:
            cfg.memory_consolidate_hour = hour
        save_config(cfg)
        settings = {"consolidate": cfg.memory_consolidate,
                    "consolidate_hour": cfg.memory_consolidate_hour}
    if settings.get("consolidate"):
        _ok(f"Nightly consolidation is on (from {settings.get('consolidate_hour')}:00).")
    else:
        _muted("Nightly consolidation is off.")


@_memory.command("consolidate")
def memory_consolidate(
    dry_run: bool = typer.Option(False, "--dry-run", help="Show the plan without changing notes."),
    yes: bool = typer.Option(False, "--yes", help="Apply without asking."),
    min_confidence: float | None = typer.Option(None, "--min-confidence",
                                               help="Score gate for each operation."),
    max_loss: float | None = typer.Option(None, "--max-loss",
                                          help="Most of the store one run may drop (0-1)."),
):
    """Ask the model for operations over memory, gate them, then apply them."""
    if (min_confidence is not None or max_loss is not None):
        get_client().memory_settings(consolidate_min_confidence=min_confidence,
                                     consolidate_max_loss=max_loss)
    if not dry_run and not yes and not typer.confirm(
            "Apply the planned memory changes?", default=False):
        _muted("Nothing changed.")
        return
    result = get_client().memory_consolidate(dry_run=dry_run)
    planned = result.get("planned") or []
    if not planned:
        _muted("Nothing worth consolidating.")
    for operation in planned:
        title = f" → {operation['title']}" if operation.get("title") else ""
        _muted(f"{operation['op']} {', '.join(operation['slugs'])}{title} "
               f"(confidence {operation['confidence']:.2f})")
    for reason in result.get("refused") or []:
        _muted(f"refused: {reason}")
    if dry_run:
        _ok(f"Planned {len(result.get('accepted') or [])} operation(s); nothing was changed.")
    elif result.get("applied"):
        _ok(f"Applied {len(result['applied'])} change(s); `kova memory undo` reverses them.")
    else:
        _muted("No changes were applied.")


@_memory.command("diary")
def memory_diary(limit: int = typer.Option(20, "--limit", min=1, max=500)):
    """What Kova's background memory passes did, and why."""
    entries = get_client().memory_diary(limit)
    if not entries:
        _empty("No memory diary entries yet.",
               "Nightly consolidation and reviews write them; both are off by default.")
        return
    table = _table(caption=f"{len(entries)} entries")
    table.add_column("When", style=theme.MUTED, no_wrap=True)
    table.add_column("Kind", no_wrap=True)
    table.add_column("What", overflow="fold")
    for entry in entries:
        table.add_row(_relative_when(entry.get("created")), entry.get("kind", ""),
                      entry.get("entry", ""))
    console.print(table)


@_memory.command("undo")
def memory_undo(revision_id: int | None = typer.Argument(None),
                list_revisions: bool = typer.Option(False, "--list",
                                                    help="Show recent changes instead.")):
    """Reverse remembered changes made by review or consolidation."""
    if list_revisions:
        entries = get_client().memory_revisions()
        if not entries:
            _empty("No memory changes to undo.", "Kova has not rewritten memory behind your back.")
            return
        table = _table(caption=f"{len(entries)} recent change(s)")
        table.add_column("#", justify="right", style=theme.ACCENT_SOFT, no_wrap=True)
        table.add_column("When", style=theme.MUTED, no_wrap=True)
        table.add_column("Kind", no_wrap=True)
        table.add_column("Note", no_wrap=True)
        table.add_column("Undone", no_wrap=True)
        for entry in entries:
            table.add_row(str(entry["id"]), _relative_when(entry.get("created")),
                          entry.get("kind", ""), entry.get("slug", ""),
                          "yes" if entry.get("undone") else "no")
        console.print(table)
        return
    undone = get_client().memory_undo(revision_id)
    if undone:
        _ok(f"Put back: {', '.join(undone)}.")
    else:
        _muted("Nothing to undo.")


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


@_jobs.command("list")
def jobs_list():
    """List the scheduled recurring jobs."""
    now = _local_now()
    jobs = get_client().jobs_list()
    if not jobs:
        _empty("No scheduled jobs.",
               'Add one with: kova jobs add "every 2h" "Check the backups" --command "ls ~/backups"')
        return
    table = _table(caption=f"{len(jobs)} job" + ("" if len(jobs) == 1 else "s"))
    table.add_column("#", justify="right", style=theme.ACCENT_SOFT, no_wrap=True)
    table.add_column("Job", overflow="ellipsis")
    table.add_column("Kind", style=theme.MUTED, no_wrap=True)
    table.add_column("Schedule", no_wrap=True)
    table.add_column("Next", no_wrap=True)
    table.add_column("Runs", justify="right", style=theme.MUTED, no_wrap=True)
    for job in jobs:
        kind = "heartbeat" if job["idle_only"] else job["kind"]
        try:
            next_run = _reminder_time(job["next_run"], now)
            when = f"{next_run.strftime('%Y-%m-%d %H:%M')} · {format_relative(next_run, now)}"
        except (TypeError, ValueError):
            when = job["next_run"]
        table.add_row(str(job["id"]), _clip(job["text"]), kind, job["schedule"], when, str(job["runs"]))
    console.print(table)


@_jobs.command("add")
def jobs_add(
    schedule: str,
    text: str,
    command: str = typer.Option(None, "--command", help="Shell command to run, with no model call."),
    prompt: str = typer.Option(None, "--prompt", help="Instruction for one Kova turn per run."),
    idle_only: bool = typer.Option(False, "--idle-only", help="Only start when nothing else is running."),
    first_run: str = typer.Option(None, "--first-run", help="First run time, e.g. 'tomorrow 8am'."),
):
    """Add a recurring job: --command for a script, --prompt for a Kova turn."""
    if bool(command) == bool(prompt):
        _fail("Pass exactly one of --command (a shell command) or --prompt (an instruction).",
              "Example: kova jobs add \"daily 09:00\" \"Summarise the news\" --prompt \"Summarise today's headlines\"")
    job = get_client().job_add(text, schedule, command=command, prompt=prompt,
                               first_run=first_run, idle_only=idle_only)
    _ok(f"Job #{job['id']} scheduled {job['schedule']} · next {job['next_run']}")


@_jobs.command("rm")
def jobs_rm(jid: int):
    """Delete a scheduled job."""
    if get_client().job_delete(jid):
        _ok(f"Removed job #{jid}.")
    else:
        _fail(f"No job #{jid}.")


# ---------------------------------------------------------------------------
# Plugins
# ---------------------------------------------------------------------------

def _plugin_section(title: str, lines: list[str]) -> None:
    console.print(Text(f"{title}:", style=f"bold {theme.MUTED}"))
    if not lines:
        console.print(Text("  (none)", style=theme.FAINT))
        return
    for line in lines:
        console.print(Text("  " + line, style=theme.MUTED))


def _plugin_summary(plugin) -> None:
    """The install-time confirmation: exactly what this plugin adds."""
    from kova import plugins

    info = plugins.describe(plugin)
    console.print()
    console.print(Text(f"{info['name']} {info['version']}", style=f"bold {theme.ACCENT}"))
    if info["description"]:
        console.print(Text(info["description"], style=theme.MUTED))
    meta = [part for part in (f"by {info['author']}" if info["author"] else "",
                              info["homepage"]) if part]
    if meta:
        console.print(Text("  ".join(meta), style=theme.FAINT))
    if info["min_kova"]:
        console.print(Text(f"  requires Kova >= {info['min_kova']}", style=theme.FAINT))

    mcp_lines = []
    for server in info["mcp"]:
        target = server["command"]
        if target and server["args"]:
            target += " " + " ".join(server["args"])
        elif server["url"]:
            target = server["url"]
        note = "network access" if server["url"] else ""
        if server["env_keys"]:
            note = (note + "; " if note else "") + "env: " + ", ".join(server["env_keys"])
        line = f"{server['name']}  →  {target}"
        if note:
            line += f"  ({note})"
        mcp_lines.append(line)

    command_lines = [f"/{cmd['name']} — {cmd['description']}".rstrip(" —")
                     for cmd in info["commands"]]
    permission_lines = [f"{rule['action']} {rule['tool']} {rule['pattern']}"
                        for rule in info["permissions"]]

    console.print()
    _plugin_section("MCP servers (tools)", mcp_lines)
    _plugin_section("Skills", [f"{s['name']} — {s['description']}".rstrip(" —")
                               for s in info["skills"]])
    _plugin_section("Slash commands", command_lines)
    _plugin_section("Permission rules", permission_lines)
    console.print()


@_plugins.command("install")
def plugin_install(
    source: str = typer.Argument(..., help="A local path, a git URL, or a catalog name."),
    yes: bool = typer.Option(False, "--yes", "-y", help="Install without asking; still shows what it adds."),
):
    """Install a plugin bundle, showing exactly what it adds before asking."""
    from kova import plugins

    def confirm(plugin):
        _plugin_summary(plugin)
        return yes or typer.confirm("Install this plugin?", default=False)

    name, _version = _catalog_split(source)
    if (not Path(source).expanduser().exists() and not plugins._looks_like_git(source)
            and plugins._NAME_RE.match(name)):
        _catalog_install(source, yes=yes, kind="plugin")
        return
    try:
        plugin = plugins.install(source, confirm=confirm)
    except plugins.PluginError as exc:
        _fail(str(exc))
    if plugin is None:
        _muted("Cancelled.")
        return
    _ok(f"Installed {plugin.name} {plugin.version}.")
    _reload_mcp()
    if plugin.mcp:
        _muted("  Its MCP servers connect once they are reachable.")


@_plugins.command("list")
def plugin_list():
    """List installed plugins and their enabled state."""
    from kova import plugins

    items = plugins.installed()
    if not items:
        _empty("No plugins installed.", "Add one with: kova plugin install <path|git url>")
        return
    table = _table(caption=f"{len(items)} installed")
    table.add_column("Plugin", no_wrap=True)
    table.add_column("Version", no_wrap=True)
    table.add_column("State", no_wrap=True)
    table.add_column("About", overflow="fold")
    for plugin in items:
        state = Text("enabled", style=theme.SUCCESS) if plugin.enabled else Text("disabled", style=theme.MUTED)
        table.add_row(Text(plugin.name, style="bold"), plugin.version, state,
                      _clip(plugin.description))
    console.print(table)


@_plugins.command("enable")
def plugin_enable(name: str):
    """Re-enable a disabled plugin, restoring its skills, commands and rules."""
    from kova import plugins

    try:
        plugins.enable(name)
    except plugins.PluginError as exc:
        _fail(str(exc))
    _ok(f"Enabled {name}.")
    _reload_mcp()


@_plugins.command("disable")
def plugin_disable(name: str):
    """Disable a plugin, removing its skills, commands, MCP servers and rules."""
    from kova import plugins

    try:
        plugins.disable(name)
    except plugins.PluginError as exc:
        _fail(str(exc))
    _ok(f"Disabled {name}.")
    _reload_mcp()


@_plugins.command("remove")
def plugin_remove(name: str):
    """Uninstall a plugin and everything it added."""
    from kova import plugins

    try:
        plugins.remove(name)
    except plugins.PluginError as exc:
        _fail(str(exc))
    _ok(f"Removed {name}.")
    _reload_mcp()


@_plugins.command("update")
def plugin_update(
    name: str = typer.Argument(..., help="The plugin to update from its recorded source."),
    yes: bool = typer.Option(False, "--yes", "-y", help="Update without asking."),
):
    """Re-install a plugin from its original source, showing what it adds."""
    from kova import plugins

    current = next((item for item in plugins.installed() if item.name == name), None)
    if current is not None and (current.source or "").startswith("catalog:"):
        _catalog_install(name, yes=yes, kind="plugin", upgrade=True)
        return

    def confirm(plugin):
        _plugin_summary(plugin)
        return yes or typer.confirm("Update to this version?", default=False)

    try:
        plugin = plugins.update(name, confirm=confirm)
    except plugins.PluginError as exc:
        _fail(str(exc))
    if plugin is None:
        _muted("Cancelled.")
        return
    _ok(f"Updated {plugin.name} {plugin.version}.")


# ---------------------------------------------------------------------------
# Extensions catalog
# ---------------------------------------------------------------------------

def _catalog_split(spec: str) -> tuple[str, str | None]:
    from kova import catalog
    return catalog.split_spec(spec)


def _load_catalog():
    from kova import catalog
    loaded = catalog.load(catalog.configured_url(load_config()))
    if loaded.warning:
        _muted(loaded.warning)
    return loaded


def _catalog_state(entry, home) -> Text:
    from kova import catalog
    if not entry.available:
        return Text("coming soon", style=theme.FAINT)
    installed = catalog.installed_version(entry, home)
    if installed is None:
        return Text("available", style=theme.MUTED)
    if installed != entry.version:
        return Text(f"{installed} → {entry.version}", style=theme.ACCENT)
    return Text("installed", style=theme.SUCCESS)


def _catalog_summary(staged) -> None:
    """The confirmation screen: where it comes from, its checksum, what it adds."""
    info = staged.describe()
    if staged.plugin is not None:
        _plugin_summary(staged.plugin)
    else:
        skill = info["skill"]
        console.print()
        console.print(Text(f"{skill['name']} (skill) {info['version']}", style=f"bold {theme.ACCENT}"))
        console.print(Text(skill["description"], style=theme.MUTED))
        console.print()
        _plugin_section("Instructions added to the prompt when the skill is loaded",
                        skill["body"].splitlines())
        console.print()
    _plugin_section("Catalog says", info["permissions"])
    console.print(Text(f"Source: {info['source']}", style=theme.FAINT))
    console.print(Text(f"SHA-256: {info['sha256']} (verified)", style=theme.FAINT))
    console.print()


def _catalog_install(spec: str, *, yes: bool, kind: str | None = None, upgrade: bool = False) -> None:
    from kova import catalog

    name, version = _catalog_split(spec)
    loaded = _load_catalog()
    try:
        entry = loaded.get(name)
        if kind and entry.kind != kind:
            _fail(f"{name} is a {entry.kind} in the catalog, not a {kind}.",
                  hint=f"Install it with: kova catalog install {name}")
        installed = catalog.installed_version(entry, load_config().home)
        if installed is not None and not upgrade:
            if installed == entry.version:
                _ok(f"{name} {installed} is already installed.")
                return
            _fail(f"{name} {installed} is installed; the catalog has {entry.version}.",
                  hint=f"Update it with: kova catalog install {name} --upgrade")
        if upgrade and installed == entry.version:
            _ok(f"{name} {installed} is already the catalog's version.")
            return

        def confirm(staged) -> bool:
            _catalog_summary(staged)
            verb = "Update to this version?" if installed else f"Install this {entry.kind}?"
            return yes or typer.confirm(verb, default=False)

        result = catalog.install(name, version=version, catalog=loaded, confirm=confirm,
                                 upgrade=installed is not None)
    except catalog.CatalogError as exc:
        _fail(str(exc))
    if result is None:
        _muted("Cancelled.")
        return
    _ok(f"Installed {result}.")
    _muted("Its skills apply from your next message.")
    _reload_mcp()


@_catalog.command("search")
def catalog_search(
    query: str = typer.Argument("", help="Words to look for in names, descriptions and tags."),
    kind: str = typer.Option(None, "--kind", help="Only plugin or skill."),
    as_json: bool = typer.Option(False, "--json", help="Print one JSON array for scripts."),
):
    """Search the extensions catalog."""
    if kind not in (None, "plugin", "skill"):
        _fail("--kind must be plugin or skill.")
    loaded = _load_catalog()
    entries = loaded.search(query, kind)
    home = load_config().home
    if as_json:
        from kova import catalog
        print(json.dumps([{**entry.as_dict(), "installed_version": catalog.installed_version(entry, home)}
                          for entry in entries], ensure_ascii=False))
        return
    if not entries:
        _empty("Nothing in the catalog matches that.", "Try fewer words, or: kova catalog search")
        return
    origin = "bundled with Kova" if loaded.bundled else loaded.source
    table = _table(caption=f"{len(entries)} extension(s) · {origin}")
    table.add_column("Name", no_wrap=True)
    table.add_column("Kind", no_wrap=True)
    table.add_column("Version", no_wrap=True)
    table.add_column("State", no_wrap=True)
    table.add_column("About", overflow="fold")
    for entry in entries:
        table.add_row(Text(entry.name, style="bold"), entry.kind, entry.version,
                      _catalog_state(entry, home), _clip(entry.description, 70))
    console.print(table)
    _muted("Details: kova catalog show NAME    Install: kova catalog install NAME")


@_catalog.command("show")
def catalog_show(
    name: str = typer.Argument(..., help="The extension's name."),
    as_json: bool = typer.Option(False, "--json", help="Print one JSON object for scripts."),
):
    """Show one extension: what it is, who made it, its source and checksum."""
    from kova import catalog

    loaded = _load_catalog()
    try:
        entry = loaded.get(name)
    except catalog.CatalogError as exc:
        _fail(str(exc))
    home = load_config().home
    if as_json:
        print(json.dumps({**entry.as_dict(), "installed_version": catalog.installed_version(entry, home)},
                         ensure_ascii=False))
        return
    console.print(Text.assemble((entry.name, f"bold {theme.ACCENT}"), (f"  {entry.kind} {entry.version}  ", theme.MUTED),
                                _catalog_state(entry, home)))
    if entry.description:
        console.print(Text(entry.description))
    meta = [part for part in (f"by {entry.author}" if entry.author else "", entry.homepage) if part]
    if meta:
        console.print(Text("  ".join(meta), style=theme.FAINT))
    if entry.tags:
        console.print(Text("Tags: " + ", ".join(entry.tags), style=theme.FAINT))
    console.print()
    _plugin_section("What it may do", entry.permissions)
    if entry.available:
        console.print(Text(f"Source: {entry.url}", style=theme.FAINT))
        console.print(Text(f"SHA-256: {entry.sha256}", style=theme.FAINT))
        _muted(f"Install: kova catalog install {entry.name}")
    else:
        _muted("Coming soon" + (f" (issue #{entry.issue})" if entry.issue else "") + ".")


@_catalog.command("install")
def catalog_install(
    spec: str = typer.Argument(..., help="NAME, or NAME@VERSION to insist on the pinned version."),
    yes: bool = typer.Option(False, "--yes", "-y", help="Install without asking; still shows what it adds."),
    upgrade: bool = typer.Option(False, "--upgrade", help="Replace an installed older version."),
):
    """Install a plugin or skill: verify its checksum, show what it adds, then ask."""
    _catalog_install(spec, yes=yes, upgrade=upgrade)


# ---------------------------------------------------------------------------
# Skills
# ---------------------------------------------------------------------------

_SKILL_ORIGINS = {"learned": "learned by Kova", "catalog": "from the catalog",
                  "yours": "added by you", "plugin": "plugin"}


def _skill_origin(row: dict) -> str:
    if row["origin"] == "plugin":
        return f"plugin {row['plugin']}"
    return _SKILL_ORIGINS[row["origin"]]


def _find_skill(name: str) -> dict:
    from kova import extensions
    rows = extensions.list_skills(load_config().home)
    wanted = name.strip().casefold()
    matches = [row for row in rows if row["id"] == name or row["name"].casefold() == wanted
               or Path(row["id"]).stem.casefold() == wanted]
    if not matches:
        _fail(f'No skill named "{name}".', hint="See them with: kova skills list")
    if len(matches) > 1:
        _fail(f'More than one skill is called "{name}".',
              hint="Use its id from: kova skills list --json")
    return matches[0]


@_skills.command("list")
def skills_list(
    as_json: bool = typer.Option(False, "--json", help="Print one JSON array for scripts."),
):
    """List every skill the model can see, and where each came from."""
    from kova import extensions

    rows = extensions.list_skills(load_config().home)
    if as_json:
        print(json.dumps(rows, ensure_ascii=False))
        return
    if not rows:
        _empty("No skills yet.", "Kova writes them as it learns; or browse: kova catalog search --kind skill")
        return
    table = _table(caption=f"{len(rows)} skill(s)")
    table.add_column("Skill", no_wrap=True)
    table.add_column("From", no_wrap=True)
    table.add_column("About", overflow="fold")
    for row in rows:
        table.add_row(Text(row["name"], style="bold"), _skill_origin(row), _clip(row["description"], 70))
    console.print(table)


@_skills.command("show")
def skills_show(name: str = typer.Argument(..., help="A skill's name or id.")):
    """Print a skill's full instructions."""
    row = _find_skill(name)
    console.print(Text.assemble((row["name"], f"bold {theme.ACCENT}"), (f"  {_skill_origin(row)}", theme.MUTED)))
    console.print(Text(row["description"], style=theme.MUTED))
    console.print(Text(row["id"], style=theme.FAINT))
    console.print()
    console.print(Text(row["body"]))


@_skills.command("rm")
def skills_rm(
    name: str = typer.Argument(..., help="A skill's name or id."),
    yes: bool = typer.Option(False, "--yes", "-y", help="Delete without asking."),
):
    """Delete a skill (plugin skills go with their plugin instead)."""
    from kova import extensions
    from kova.skills import SkillError

    row = _find_skill(name)
    if not row["editable"]:
        _fail(f'"{row["name"]}" comes with the {row["plugin"]} plugin.',
              hint=f"Disable it with: kova plugin disable {row['plugin']}")
    if not yes and not typer.confirm(f'Delete the skill "{row["name"]}"?', default=False):
        _muted("Cancelled.")
        return
    try:
        extensions.delete_skill(load_config().home, row["id"])
    except SkillError as exc:
        _fail(str(exc))
    _ok(f'Deleted "{row["name"]}".')


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
        # Chat ids are random hex, so an id can be all digits: try it as an id.
        if len(ref) < 12:
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
    if full.get("ephemeral"):
        _fail("Incognito chats cannot be exported.")
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


# ---------------------------------------------------------------------------
# config: read and change ~/.kova/config.toml without a text editor.

REDACTED = "<redacted>"
#: Whole name parts that mark an unknown value as credential-shaped. Unknown
#: keys are never interpreted, so anything that could be a key, token, session
#: or custom header is hidden rather than guessed to be harmless.
_SECRET_WORDS = frozenset({
    "apikey", "auth", "authorization", "bearer", "cert", "certificate",
    "cookie", "cookies", "credential", "credentials", "dsn", "header",
    "headers", "key", "keys", "oauth", "pass", "passphrase", "passwd",
    "password", "private", "pwd", "secret", "secrets", "session", "signature",
    "token", "tokens", "webhook",
})
_NAME_PART = re.compile(r"[A-Z]+(?![a-z])|[A-Z]?[a-z]+|\d+")


def _secret_name(name: str) -> bool:
    """Whether a key name looks like it holds a credential.

    Matching is by whole name parts (split on punctuation and camel case), so
    ``private_key``, ``apiKey`` and ``aws_access_key_id`` match while
    ``tokenizer`` and ``monkey`` do not.
    """
    for chunk in re.split(r"[^A-Za-z0-9]+", name):
        if not chunk:
            continue
        lowered = chunk.lower()
        if lowered in _SECRET_WORDS:
            return True
        if any(part.lower() in _SECRET_WORDS for part in _NAME_PART.findall(chunk)):
            return True
    return False


@dataclass(frozen=True)
class _ConfigField:
    """One validated scalar setting exposed by ``kova config set``."""

    key: str
    attr: str
    kind: str
    choices: tuple[str, ...] = ()
    minimum: float | None = None
    maximum: float | None = None


_CONFIG_FIELDS: dict[str, _ConfigField] = {
    field.key: field
    for field in (
        _ConfigField("active", "active", "active"),
        _ConfigField("bind", "bind", "choice", ("loopback", "lan")),
        _ConfigField("catalog.url", "catalog_url", "catalog"),
        _ConfigField("compact_threshold", "compact_threshold", "float", minimum=0.1, maximum=0.99),
        _ConfigField("files_path", "files_path", "path"),
        _ConfigField("local.idle_unload_minutes", "local_idle_unload_minutes", "int", minimum=0),
        _ConfigField("memory.budget", "memory_budget", "int", minimum=0),
        _ConfigField("memory.consolidate", "memory_consolidate", "bool"),
        _ConfigField("memory.consolidate_hour", "memory_consolidate_hour", "int",
                     minimum=0, maximum=23),
        _ConfigField("memory.consolidate_max_loss", "memory_consolidate_max_loss", "float",
                     minimum=0, maximum=1),
        _ConfigField("memory.consolidate_min_confidence",
                     "memory_consolidate_min_confidence", "float", minimum=0, maximum=1),
        _ConfigField("memory.index_lines", "memory_index_lines", "int", minimum=0),
        _ConfigField("memory.path", "memory_path", "path"),
        _ConfigField("memory.review", "memory_review", "bool"),
        _ConfigField("memory.review_every", "memory_review_every", "int", minimum=1),
        _ConfigField("memory.write_subdir", "memory_write_subdir", "component"),
        _ConfigField("persona.configured", "persona_configured", "bool"),
        _ConfigField("persona.name", "persona_name", "persona"),
        _ConfigField("persona.tone", "persona_tone", "choice", tuple(TONES)),
        _ConfigField("port", "port", "int", minimum=1, maximum=65535),
        _ConfigField("privacy.local_only", "privacy_local_only", "bool"),
        _ConfigField("voice.stt", "voice_stt", "choice", ("local", "browser")),
        _ConfigField("voice.tts", "voice_tts", "choice", ("espeak-ng", "off")),
        _ConfigField("voice.language", "voice_language", "str"),
        _ConfigField("voice.max_seconds", "voice_max_seconds", "int", minimum=1),
        _ConfigField("privacy.secrets", "privacy_secrets", "choice", ("off", "redact", "block")),
        _ConfigField("tui.theme", "tui_theme", "choice", ("dark", "light")),
        _ConfigField("usage.monthly_budget_usd", "usage_monthly_budget_usd", "float", minimum=0),
    )
}

_TRUE = {"1", "true", "yes", "on", "enabled"}
_FALSE = {"0", "false", "no", "off", "disabled"}


class _ConfigValueError(ValueError):
    """A ``config set`` value that fails the field's schema."""


def _config_range(field: _ConfigField) -> str:
    if field.minimum is not None and field.maximum is not None:
        return f"between {field.minimum} and {field.maximum}"
    if field.minimum is not None:
        return f"at least {field.minimum}"
    if field.maximum is not None:
        return f"at most {field.maximum}"
    return ""


def _config_path_value(key: str, raw: str) -> Path:
    text = raw.strip()
    if not text or "\0" in text:
        raise _ConfigValueError(f'Invalid value for {key}: "{raw}" must be a non-empty path.')
    try:
        return Path(text).expanduser().resolve()
    except (OSError, RuntimeError) as exc:
        raise _ConfigValueError(f'Invalid value for {key}: "{raw}" is not a usable path ({exc}).')


def _parse_config_value(field: _ConfigField, raw: str, cfg) -> object:
    """Parse and validate one scalar value against its declared schema."""
    text = raw.strip()
    key = field.key
    if field.kind == "bool":
        lowered = text.casefold()
        if lowered in _TRUE:
            return True
        if lowered in _FALSE:
            return False
        raise _ConfigValueError(
            f'Invalid value for {key}: "{raw}" must be on or off (true/false).')
    if field.kind in ("int", "float"):
        try:
            parsed = int(text) if field.kind == "int" else float(text)
        except ValueError:
            noun = "a whole number" if field.kind == "int" else "a number"
            raise _ConfigValueError(f'Invalid value for {key}: "{raw}" must be {noun}.')
        if field.kind == "float":
            if not math.isfinite(parsed):
                raise _ConfigValueError(f'Invalid value for {key}: "{raw}" must be a finite number.')
        if field.minimum is not None and parsed < field.minimum:
            raise _ConfigValueError(
                f'Invalid value for {key}: "{raw}" must be {_config_range(field)}.')
        if field.maximum is not None and parsed > field.maximum:
            raise _ConfigValueError(
                f'Invalid value for {key}: "{raw}" must be {_config_range(field)}.')
        return parsed
    if field.kind == "choice":
        if text not in field.choices:
            raise _ConfigValueError(
                f'Invalid value for {key}: "{raw}" must be one of: {", ".join(field.choices)}.')
        return text
    if field.kind == "path":
        return _config_path_value(key, raw)
    if field.kind == "component":
        if not text:
            return None
        if text in (".", "..") or "/" in text or "\\" in text or "\0" in text:
            raise _ConfigValueError(
                f'Invalid value for {key}: "{raw}" must be one folder name without separators.')
        return text
    if field.kind == "catalog":
        if not text or text == "bundled":
            return text
        if "\0" in text or any(ch.isspace() for ch in text):
            raise _ConfigValueError(f'Invalid value for {key}: "{raw}" must be a URL or a path.')
        if text.startswith(("http://", "https://")):
            if not text.startswith("https://"):
                raise _ConfigValueError(f'Invalid value for {key}: "{raw}" must use https.')
            return text
        return str(_config_path_value(key, text))
    if field.kind == "str":
        if not text:
            return None
        if "\0" in text:
            raise _ConfigValueError(f'Invalid value for {key}: "{raw}" must not contain a null.')
        return text
    if field.kind == "persona":
        try:
            return Persona(text).name
        except ValueError as exc:
            raise _ConfigValueError(f'Invalid value for {key}: {exc}')
    if field.kind == "active":
        names = {model.name for model in cfg.models}
        if text not in names:
            raise _ConfigValueError(
                f'Invalid value for {key}: "{raw}" is not a configured model. '
                "Run: kova model list")
        return text
    raise _ConfigValueError(f'{key} is not a settable config key.')  # pragma: no cover


def _set_config_value(cfg, key: str, raw: str) -> object:
    """Apply one setting to ``cfg`` (without saving); return the stored value."""
    if key.startswith("tui.keys."):
        action = key[len("tui.keys."):]
        if not action or action not in DEFAULT_KEYS:
            suggestion = get_close_matches(action, DEFAULT_KEYS, n=1, cutoff=0.6)
            hint = f' Did you mean "tui.keys.{suggestion[0]}"?' if suggestion else ""
            raise _ConfigValueError(
                f'Invalid value for {key}: unknown TUI action "{action}".{hint}')
        value = raw.strip()
        if not value:
            raise _ConfigValueError(f'Invalid value for {key}: a key name cannot be empty.')
        bindings = {**cfg.tui_keys, action: value}
        if value == DEFAULT_KEYS[action]:
            bindings.pop(action, None)
        try:
            Keymap(bindings)
        except ValueError as exc:
            raise _ConfigValueError(f'Invalid value for {key}: {exc}')
        cfg.tui_keys = bindings
        return value
    field = _CONFIG_FIELDS.get(key)
    if field is None:
        raise KeyError(key)
    value = _parse_config_value(field, raw, cfg)
    setattr(cfg, field.attr, value)
    return value


def _config_display_value(value: object) -> str:
    if value is None:
        return "(unset)"
    if isinstance(value, bool):
        return "on" if value else "off"
    return str(value)


def _suggest_config_key(key: str) -> str | None:
    """Closest known path, using the schema constants in ``kova.config``."""
    if "." in key:
        section, leaf = key.split(".", 1)
        data = {section: {leaf: None}}
    else:
        data = {key: None}
    found = unknown_config_keys(data)
    return found[0][1] if found else None


def _redact_tree(value: object) -> object:
    """Keep the shape of OAuth/auth data but never its values."""
    if isinstance(value, dict):
        return {str(key): _redact_tree(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_redact_tree(item) for item in value]
    if value is None or value == "":
        return value
    return REDACTED


def _redact_extra(value: object) -> object:
    """Redact unknown keys whose names look like credentials."""
    if isinstance(value, dict):
        return {
            str(key): REDACTED if _secret_name(str(key)) else _redact_extra(item)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_redact_extra(item) for item in value]
    return value


def _display_url(value: str) -> str:
    """Mask userinfo embedded in a URL; never echo it in ``config show``."""
    try:
        parts = urlsplit(value)
        if not (parts.username or parts.password):
            return value
        host = parts.hostname or ""
        port = parts.port
    except ValueError:
        # A URL this version cannot parse could hide anything in its userinfo.
        return REDACTED
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    netloc = f"{REDACTED}@{host}"
    if port is not None:
        netloc += f":{port}"
    return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))


def _config_snapshot(cfg) -> dict:
    """A complete, credential-safe view of every Config field."""
    return {
        "path": str(cfg.home / "config.toml"),
        "schema_version": CONFIG_SCHEMA,
        "home": str(cfg.home),
        "active": cfg.active,
        "port": cfg.port,
        "compact_threshold": cfg.compact_threshold,
        "files_path": str(cfg.files_path),
        "bind": cfg.bind,
        "privacy": {"secrets": cfg.privacy_secrets, "local_only": cfg.privacy_local_only},
        "local": {"idle_unload_minutes": cfg.local_idle_unload_minutes},
        "oauth_host_id": cfg.oauth_host_id,
        "memory": {
            "path": str(cfg.memory_path),
            "write_subdir": cfg.memory_write_subdir,
            "budget": cfg.memory_budget,
            "index_lines": cfg.memory_index_lines,
            "review": cfg.memory_review,
            "review_every": cfg.memory_review_every,
            "consolidate": cfg.memory_consolidate,
            "consolidate_hour": cfg.memory_consolidate_hour,
            "consolidate_min_confidence": cfg.memory_consolidate_min_confidence,
            "consolidate_max_loss": cfg.memory_consolidate_max_loss,
        },
        "tui": {"theme": cfg.tui_theme, "keys": dict(cfg.tui_keys)},
        "persona": {
            "name": cfg.persona_name,
            "tone": cfg.persona_tone,
            "configured": cfg.persona_configured,
        },
        "providers": [
            {
                "id": provider.id,
                "name": provider.name,
                "base_url": _display_url(provider.base_url),
                "kind": provider.kind,
                "adapter": provider.adapter,
                "api_key": REDACTED if provider.api_key else None,
                "headers": {name: REDACTED for name in provider.headers},
                "auth": _redact_tree(provider.auth),
            }
            for provider in cfg.providers
        ],
        "models": [
            {
                "name": model.name,
                "provider": model.provider,
                "base_url": _display_url(model.base_url),
                "model": model.model,
                "context": model.context,
                "vision": model.vision,
                "api_key": REDACTED if model.api_key else None,
            }
            for model in cfg.models
        ],
        "mcp": [
            {
                "name": server.name,
                "command": server.command,
                "args": list(server.args),
                # Environment variables and headers carry tokens: names only.
                "env": {name: REDACTED for name in server.env},
                "url": _display_url(server.url),
                "headers": {name: REDACTED for name in server.headers},
                "enabled": server.enabled,
                "disabled_tools": list(server.disabled_tools),
                "sandbox": server.sandbox,
            }
            for server in cfg.mcp
        ],
        "extra": _redact_extra(cfg._extra),
    }


def _config_rows(snapshot: dict) -> list[tuple[str, str]]:
    """Flatten the snapshot into readable table rows, still redacted."""
    memory = snapshot["memory"]
    tui = snapshot["tui"]
    persona = snapshot["persona"]
    rows = [
        ("schema_version", str(snapshot["schema_version"])),
        ("home", snapshot["home"]),
        ("active", snapshot["active"]),
        ("port", str(snapshot["port"])),
        ("compact_threshold", str(snapshot["compact_threshold"])),
        ("files_path", snapshot["files_path"]),
        ("bind", snapshot["bind"]),
        ("local.idle_unload_minutes", str(snapshot["local"]["idle_unload_minutes"])),
        ("privacy.secrets", snapshot["privacy"]["secrets"]),
        ("privacy.local_only", "on" if snapshot["privacy"]["local_only"] else "off"),
        ("oauth_host_id", snapshot["oauth_host_id"]),
        ("memory.path", memory["path"]),
        ("memory.write_subdir", memory["write_subdir"] or "(unset)"),
        ("memory.budget", str(memory["budget"])),
        ("memory.index_lines", str(memory["index_lines"])),
        ("memory.review", "on" if memory["review"] else "off"),
        ("memory.review_every", str(memory["review_every"])),
        ("memory.consolidate", "on" if memory["consolidate"] else "off"),
        ("memory.consolidate_hour", str(memory["consolidate_hour"])),
        ("memory.consolidate_min_confidence", str(memory["consolidate_min_confidence"])),
        ("memory.consolidate_max_loss", str(memory["consolidate_max_loss"])),
        ("tui.theme", tui["theme"]),
        ("tui.keys", json.dumps(tui["keys"], sort_keys=True) if tui["keys"] else "(defaults)"),
        ("persona.name", persona["name"]),
        ("persona.tone", persona["tone"]),
        ("persona.configured", "yes" if persona["configured"] else "no"),
    ]
    if snapshot["models"]:
        for index, model in enumerate(snapshot["models"]):
            rows.append((
                f"models.{index}",
                f'{model["name"]} · {model["provider"] or "(legacy)"} · {model["base_url"]}'
                f' · {model["model"] or "(no model)"} · context {model["context"]}'
                f' · vision {"on" if model["vision"] else "off"}'
                f' · key {"set" if model["api_key"] else "not set"}',
            ))
    else:
        rows.append(("models", "(none)"))
    if snapshot["providers"]:
        for index, provider in enumerate(snapshot["providers"]):
            access = "key set" if provider["api_key"] else "no key"
            if provider["headers"]:
                access += f' · {len(provider["headers"])} header(s)'
            if provider["auth"]:
                access += " · OAuth set"
            rows.append((
                f"providers.{index}",
                f'{provider["id"]} · {provider["name"]} · {provider["kind"]}'
                f' · {provider["adapter"]} · {provider["base_url"]} · {access}',
            ))
    else:
        rows.append(("providers", "(none)"))
    for index, server in enumerate(snapshot["mcp"]):
        target = server["url"] or " ".join([server["command"], *server["args"]])
        details = [server["name"], "on" if server["enabled"] else "off", target]
        if server["env"]:
            details.append(f'{len(server["env"])} env var(s)')
        if server["headers"]:
            details.append(f'{len(server["headers"])} header(s)')
        if server["disabled_tools"]:
            details.append(f'{len(server["disabled_tools"])} tool(s) off')
        if server["sandbox"]:
            details.append("sandboxed")
        rows.append((f"mcp.{index}", " · ".join(details)))
    if snapshot["extra"]:
        rows.append(("extra", json.dumps(snapshot["extra"], sort_keys=True, default=str)))
    return rows


_config = typer.Typer(no_args_is_help=True,
                      help="Read and change config.toml. See docs/CONFIG.md for every key.")
app.add_typer(_config, name="config")


@_config.command("path")
def config_path():
    """Print the config.toml path for this KOVA_HOME."""
    # Written directly, like --json: a wrapped path is not usable in a script.
    sys.stdout.write(str(kova_home() / "config.toml") + "\n")


@_config.command("show")
def config_show(
    as_json: bool = typer.Option(False, "--json", help="Print one machine-readable JSON object."),
):
    """Show every setting; API keys and other secrets are always redacted."""
    snapshot = _config_snapshot(load_config())
    if as_json:
        sys.stdout.write(json.dumps(snapshot, indent=2, ensure_ascii=False, default=str) + "\n")
        return
    table = _table(caption=f'{snapshot["path"]} · secrets redacted')
    table.add_column("Key", style=theme.ACCENT_SOFT, no_wrap=True)
    table.add_column("Value", overflow="fold")
    for key, value in _config_rows(snapshot):
        table.add_row(key, Text(value))
    console.print(table)


@_config.command("set")
def config_set(key: str, value: str):
    """Validate and save one setting. Use a dotted key, such as memory.budget."""
    cfg = load_config()
    try:
        stored = _set_config_value(cfg, key, value)
    except KeyError:
        suggestion = _suggest_config_key(key)
        if key in ("models", "providers") or key.startswith(("models.", "providers.")):
            hint = "Use: kova model ... or kova providers ..."
        elif suggestion:
            hint = f'Did you mean "{suggestion}"?'
        else:
            hint = "Run: kova config show  (docs/CONFIG.md lists every key)"
        _fail(f'Unknown config key "{key}".', hint=hint)
    except _ConfigValueError as exc:
        _fail(str(exc))
    save_config(cfg)
    _ok(f"Saved {key} = {_config_display_value(stored)}.")
    _muted("A running server picks this up on its next start: kova stop")


_providers = typer.Typer(help="Manage provider connections.", invoke_without_command=True)
app.add_typer(_providers, name="providers")


@_providers.callback()
def providers_root(ctx: typer.Context):
    if ctx.invoked_subcommand is None:
        providers_list()


@_providers.command('list')
def providers_list():
    rows = get_client().providers_list()
    if not rows:
        _empty('No providers connected.', 'Run: kova providers add')
        return
    table = _table()
    for heading in ('Connection', 'Provider', 'Access', 'Address', 'Capabilities'):
        table.add_column(heading)
    for p in rows:
        table.add_row(p['id'], p['name'], 'Connected' if p['authenticated'] else 'No credentials',
                      p['base_url'], ', '.join(p.get('capabilities') or []) or '-')
    console.print(table)


@_providers.command('add')
def provider_add(kind: str | None = typer.Argument(None, help='Optional provider type: openai, chatgpt, local, custom, or this-pc to download a fitting model.')):
    try:
        onboarding.run_setup(load_config(), provider_kind=kind)
    except onboarding.SetupError as exc:
        _fail(str(exc))


@_providers.command('remove')
def provider_remove(name: str):
    get_client().provider_remove(name)
    _ok(f'Removed provider {name} and its selected models.')


@_providers.command('logout')
def provider_logout(name: str):
    result = get_client().provider_logout(name)
    _ok(f'Signed out of {name}.')
    if not result.get('remote_revocation_confirmed', True):
        _muted('Remote revocation was not confirmed. Disconnect Kova in ChatGPT Settings to revoke access.')


@_providers.command('login')
def provider_login(name: str):
    from kova import provider_auth
    from kova.providers import ProviderError
    cfg = load_config()
    p = next((p for p in cfg.providers if p.id == name), None)
    if p is None:
        _fail(f'No provider named "{name}".')
    try:
        if providers.supports(p, 'harness_login'):
            consent = onboarding._ask('Read the installed Codex login without changing it? [y/N]:').lower() == 'y'
            provider_auth.reuse_codex(p, consent=consent)
            get_client().provider_login(name, consent=consent)
        elif providers.supports(p, 'browser_login'):
            console.print('Continue with ChatGPT in your browser…')
            provider_auth.login(p)
            get_client().provider_login(name, auth=p.auth)
        else:
            key = onboarding._secret('API key:')
            get_client().provider_login(name, api_key=key)
    except (ProviderError, onboarding.SetupError) as exc:
        _fail(str(exc))
    _ok(f'Connected {name}.')


PHONE_EXPLAINER = (
    "Phone access lets a phone on the same Wi-Fi use Kova after you pair it with a QR code. "
    "Kova then also listens on this computer's local network address (never the internet), "
    "and only paired phones get in. Unpair any time with: kova devices revoke"
)


@app.command()
def pair(
    off: bool = typer.Option(False, "--off", help="Turn phone access off. Paired phones stay paired but cannot connect."),
    yes: bool = typer.Option(False, "--yes", "-y", help="Turn phone access on without asking."),
):
    """Show a QR code to use Kova from your phone on the same Wi-Fi."""
    from kova.pairing import qr_terminal
    client = get_client()
    if off:
        client.set_phone(False)
        _ok("Phone access is off. Kova listens only on this computer again.")
        return
    status = client.phone_status()
    if not status["enabled"]:
        console.print(Text(PHONE_EXPLAINER, style=theme.MUTED))
        if not yes and onboarding._ask("Turn on phone access? [Y/n]:").lower() not in ("", "y", "yes"):
            _muted("Nothing changed.")
            return
        status = client.set_phone(True)
    if not status["listening"]:
        _fail(status.get("error") or "Phone access is on but not listening.",
              "Check that this computer is on your Wi-Fi or network, then run kova pair again.")
    code = client.pair_start()
    console.print()
    console.print(Text(qr_terminal(code["url"])), no_wrap=True, crop=False, overflow="ignore")
    console.print(Text.assemble(("Scan this with your phone's camera", "bold"),
                                (" (same Wi-Fi as this computer).", "")))
    console.print(Text(f"  Or open {code['url']}", style=theme.MUTED), no_wrap=True, crop=False, overflow="ignore")
    console.print(Text(f"  The code works once and expires in {code['expires_in'] // 60} minutes. Ctrl+C cancels.",
                       style=theme.MUTED))
    try:
        while True:
            result = client.pair_status(code["id"])
            if result["status"] == "paired":
                name = (result.get("device") or {}).get("name", "your phone")
                _ok(f"Paired {name}. Add Kova to its home screen for one-tap access.")
                _muted("See or unpair devices with: kova devices")
                return
            if result["status"] == "expired":
                _fail("The pairing code expired.", "Run kova pair again for a fresh code.")
            time.sleep(1)
    except KeyboardInterrupt:
        client.pair_cancel(code["id"])
        _muted("Cancelled. The code no longer works.")
        raise typer.Exit(130)


_devices = typer.Typer(no_args_is_help=False, help="See and unpair phones paired with kova pair.")
app.add_typer(_devices, name="devices")


@_devices.callback(invoke_without_command=True)
def devices_root(ctx: typer.Context):
    """List paired devices."""
    if ctx.invoked_subcommand is None:
        devices_list()


@_devices.command("list")
def devices_list():
    """List paired devices."""
    status = get_client().phone_status()
    devices = status["devices"]
    state = "on" if status["listening"] else ("on, not listening" if status["enabled"] else "off")
    if not devices:
        _empty(f"No paired devices. Phone access is {state}.", "Pair a phone with: kova pair")
        return
    table = _table(caption=f"{len(devices)} paired · phone access {state}"
                   + (f" at {status['address']}" if status.get("address") else ""))
    table.add_column("ID", style=theme.ACCENT_SOFT, no_wrap=True)
    table.add_column("Device")
    table.add_column("Paired", style=theme.MUTED, no_wrap=True)
    table.add_column("Last seen", style=theme.MUTED, no_wrap=True)
    table.add_column("May")
    for device in devices:
        table.add_row(device["id"], device["name"], device["created"][:16].replace("T", " "),
                      device["last_seen"][:16].replace("T", " "),
                      ", ".join(device.get("capabilities", [])))
    console.print(table)


def _change_capabilities(device_id: str, names: list[str], grant: bool) -> None:
    from kova.pairing import CAPABILITIES
    unknown = [name for name in names if name not in CAPABILITIES]
    if unknown:
        _fail(f"Unknown capability: {', '.join(unknown)}.", f"Choose from: {', '.join(CAPABILITIES)}")
    if not grant and "chat" in names:
        _fail("Every paired device can chat.", f"To cut a device off, run: kova devices revoke {device_id}")
    client = get_client()
    device = next((d for d in client.devices_list() if d["id"] == device_id), None)
    if device is None:
        _fail(f"No paired device {device_id}.", "See the IDs with: kova devices")
    current = set(device.get("capabilities", []))
    wanted = current | set(names) if grant else current - set(names)
    try:
        updated = client.device_set_capabilities(device_id, sorted(wanted))
    except KovaError as exc:
        if exc.status == 404:
            _fail(f"No paired device {device_id}.", "See the IDs with: kova devices")
        raise
    _ok(f"{updated['name']} may now: {', '.join(updated['capabilities'])}.")


@_devices.command("allow")
def devices_allow(
    device_id: str = typer.Argument(..., help="The device ID from: kova devices"),
    capabilities: list[str] = typer.Argument(..., help="What to allow, such as models:write."),
):
    """Let a paired device do more, such as models:write to install local models.

    Capabilities: chat, models:read, models:write, providers:write,
    extensions:write, settings:write. A new phone gets chat and models:read.
    """
    _change_capabilities(device_id, capabilities, grant=True)


@_devices.command("deny")
def devices_deny(
    device_id: str = typer.Argument(..., help="The device ID from: kova devices"),
    capabilities: list[str] = typer.Argument(..., help="What to take away, such as models:write."),
):
    """Take capabilities away from a paired device; chat always stays."""
    _change_capabilities(device_id, capabilities, grant=False)


@_devices.command("revoke")
def devices_revoke(
    device_id: str | None = typer.Argument(None, help="The device ID from: kova devices"),
    all_devices: bool = typer.Option(False, "--all", help="Unpair every device."),
):
    """Unpair a device; it has to scan a new code to come back."""
    if bool(device_id) == all_devices:
        _fail("Give a device ID or --all.", "See the IDs with: kova devices")
    try:
        removed = get_client().device_revoke(None if all_devices else device_id)
    except KovaError as exc:
        if exc.status == 404:
            _fail(f"No paired device {device_id}.", "See the IDs with: kova devices")
        raise
    _ok(f"Unpaired {removed} device{'s' if removed != 1 else ''}.")


@app.command()
def doctor(
    as_json: bool = typer.Option(False, "--json", help="Print one JSON array for scripts."),
) -> None:
    """Check this install: config, sandbox, port, token, Node, disk and model."""
    from kova.diagnostics import diagnose
    checks = diagnose()
    if as_json:
        print(json.dumps([{"ok": check.ok, "check": check.name, "detail": check.detail}
                          for check in checks], ensure_ascii=False))
    else:
        for check in checks:
            console.print(Text((f'{SYMBOL_OK} ' if check.ok else f'{SYMBOL_ERROR} ') + check.detail))
        failed = [check for check in checks if not check.ok]
        if failed:
            next_step = next((check.next_step for check in failed if check.next_step),
                             "Run kova doctor again.")
            console.print(Text.assemble(
                (f"{SYMBOL_ARROW} ", theme.ACCENT_SOFT),
                (f"Next: {next_step}", theme.MUTED),
                (f"    ({len(checks) - len(failed)} ok, {len(failed)} "
                 f"need{'s' if len(failed) == 1 else ''} attention)", theme.MUTED),
            ))
        else:
            console.print(Text.assemble(
                (f"{SYMBOL_ARROW} ", theme.ACCENT_SOFT),
                ("All checks passed.", theme.SUCCESS),
                (f"    ({len(checks)} ok)", theme.MUTED),
            ))
    if any(not check.ok for check in checks):
        raise typer.Exit(1)
