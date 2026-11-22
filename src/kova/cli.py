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

from kova import (__version__, autostart, backup, bot_templates, lifecycle, model_picker,
                  onboarding, providers, themes, egress, ui_prefs)
from kova.bots import RESERVED_HANDLES
from kova.client import KovaClient, KovaError, ensure_server
from kova.config import (
    CONFIG_SCHEMA,
    ensure_token,
    kova_home,
    load_config,
    save_config,
    unknown_config_keys,
)
from kova.chat_appearance import CHAT_COLOURS, CHAT_ICONS
from kova.modes import MODES
from kova.persona import TONES, Persona
from kova.privacy import status_text as privacy_status_text
from kova.tools.reminders import format_relative, format_when
from kova.tui import brand
from kova.tui.keymap import DEFAULT_KEYS, Keymap
from kova import theme as kova_theme
from kova.tui.style import active_theme, colour
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
    if "no model is set up" in text:
        return "Run: kova setup"
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
    console.print(Text.assemble((f"{SYMBOL_ERROR} ", f"bold {colour('ERROR')}"), (text, colour('ERROR'))))


def _fail(message: str, hint: str | None = None) -> None:
    """Print one consistent error line, plus an optional dim hint, and exit 1."""
    text = " ".join(message.splitlines())
    _error_line(text)
    hint = hint if hint is not None else _hint_for(text)
    if hint:
        console.print(Text(f"  {hint}", style=colour('MUTED')))
    raise typer.Exit(1)


def _ok(message: str) -> None:
    console.print(Text.assemble((f"{SYMBOL_OK} ", f"bold {colour('SUCCESS')}"), (message, "")))


def _muted(message: str) -> None:
    console.print(Text(message, style=colour('MUTED')))


def _table(*, title: str | None = None, caption: str | None = None) -> Table:
    """A Kova table: muted header rule, accent title, no outer box."""
    return Table(
        box=box.SIMPLE_HEAD,
        border_style=colour('BORDER'),
        header_style=f"bold {colour('MUTED')}",
        title=Text(title, style=f"bold {colour('ACCENT')}") if title else None,
        title_justify="left",
        caption=Text(caption, style=colour('MUTED')) if caption else None,
        caption_justify="left",
        pad_edge=False,
        show_edge=False,
        expand=False,
    )


def _empty(message: str, hint: str) -> None:
    """A friendly empty state: one line, then a dim hint on how to fill it."""
    console.print(Text.assemble((f"{SYMBOL_BULLET} ", f"bold {colour('ACCENT_SOFT')}"), (message, "")))
    console.print(Text(f"  {hint}", style=colour('MUTED')))


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


@app.command("app", context_settings={"allow_extra_args": True, "ignore_unknown_options": True})
def desktop_app(
    ctx: typer.Context,
    browser: bool = typer.Option(False, "--browser", help="Open the web UI instead (same as kova web)."),
    prepare: bool = typer.Option(False, "--prepare", hidden=True),
    register: bool = typer.Option(False, "--register", hidden=True),
    data_home: str | None = typer.Option(None, "--data-home", hidden=True),
) -> None:
    """Open the Kova desktop app."""
    from .app import main as app_main

    args = []
    if prepare:
        args.append("--prepare")
    if register:
        args.append("--register")
    if data_home is not None:
        args += ["--data-home", data_home]
    if browser:
        args.append("--browser")
    app_main(args + list(ctx.args))


@app.command("web")
def web_app() -> None:
    """Open Kova's web UI in your default browser."""
    from .app import open_in_browser

    raise typer.Exit(open_in_browser())


def _version(value: bool) -> None:
    if value:
        console.print(Text.assemble(
            brand.gradient_text("Kova"),
            ("  ·  ", colour('BORDER')),
            (f"v{__version__}", colour('MUTED')),
        ))
        console.print(Text("Your own private AI that lives on your computer.", style=f"italic {colour('MUTED')}"))
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
        return Text.assemble((f"{SYMBOL_OK} ", f"bold {colour('SUCCESS')}"), (text[2:], ""))
    if text.startswith("✗ "):
        return Text.assemble(("✗ ", f"bold {colour('ERROR')}"), (text[2:], ""))
    if text.startswith(f"{SYMBOL_ARROW} "):
        return Text.assemble((f"{SYMBOL_ARROW} ", colour('ACCENT_SOFT')), (text[2:], colour('MUTED')))
    return Text(text, style=colour('MUTED'))


def _lifecycle_emit(message: str) -> None:
    console.print(_lifecycle_line(message))


def _print_update_check(message: str) -> None:
    """Give the update check a ✓ / ↑ / • line that matches the rest of the CLI."""
    lowered = message.casefold()
    if lowered.startswith("up to date"):
        console.print(Text.assemble((f"{SYMBOL_OK} ", f"bold {colour('SUCCESS')}"), (message, colour('SUCCESS'))))
    elif lowered.startswith("update available"):
        console.print(Text.assemble(("↑ ", f"bold {colour('WARNING')}"), (message, "")))
    else:
        console.print(Text.assemble((f"{SYMBOL_BULLET} ", colour('ACCENT_SOFT')), (message, colour('MUTED'))))


@app.command()
def update(
    ref: str | None = typer.Option(None, "--ref", help="GitHub branch, tag, or commit (default: KOVA_REF or main)."),
    check: bool = typer.Option(False, "--check", help="Only compare installed and remote versions/commits."),
    verbose: bool = typer.Option(False, "--verbose", help="Show full uv and desktop preparation output."),
):
    """Update Kova and its desktop app, or check without changing anything."""
    try:
        target = lifecycle.selected_ref(ref)
        if check:
            _print_update_check(lifecycle.check_update(target))
        else:
            lifecycle.update(target, emit=_lifecycle_emit, verbose=verbose,
                             progress=console.status if console.is_terminal and not verbose else None)
    except (lifecycle.LifecycleError, OSError) as exc:
        _fail(str(exc))
    except lifecycle.psutil.Error as exc:
        _fail(f"Could not inspect the running installation: {exc}")


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


_model = typer.Typer(no_args_is_help=True, help="Choose the provider and model Kova uses.")
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

_bots = typer.Typer(no_args_is_help=False, help="Add and manage bots, and chat with one.")
app.add_typer(_bots, name="bots")

_projects = typer.Typer(no_args_is_help=True, help="Manage projects and shared chat instructions.")
app.add_typer(_projects, name="projects")

_prefs = typer.Typer(no_args_is_help=True, help="Export and import UI preferences.")
app.add_typer(_prefs, name="prefs")

_themes = typer.Typer(no_args_is_help=False, help="List, inspect and install themes.")
app.add_typer(_themes, name="themes")


@_chats.callback(invoke_without_command=True)
def chats_root(ctx: typer.Context):
    """List chats, newest first."""
    if ctx.invoked_subcommand is None:
        chats_list(as_json=False)

@_bots.callback(invoke_without_command=True)
def bots_root(ctx: typer.Context):
    """List your bots. Add one with ``kova bots create``, chat with ``kova chat --bot <handle>``."""
    if ctx.invoked_subcommand is None:
        bots_list()


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
                      f"{ASK_INPUT_LIMIT // 1000} KB was sent.", style=colour('WARNING'), markup=False)
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


def _ask_error(message: str, *, as_json: bool = False) -> None:
    """One ask/compare failure; JSON for scripts when ``--json`` was asked for."""
    text = " ".join(message.splitlines())
    if as_json:
        print(json.dumps({"error": text, "hint": _hint_for(text)}, ensure_ascii=False))
    else:
        print(f"Error: {text}", file=sys.stderr)
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
        _ask_error("Use --incognito without --chat to start a private one-off question.",
                   as_json=as_json)
    if incognito:
        _stderr.print("Incognito: not saved on this PC. Your provider still receives messages.", markup=False)
    cleanup_id = None
    if think:
        reason = _reasoning_error(model)
        if reason:
            _ask_error(f"{reason}. Use a reasoning model, or drop --think.", as_json=as_json)
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
                                         first_turn=True, model=model,
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
                _stderr.print(event["text"], style=colour('MUTED'), end="", markup=False)
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
                    _stderr.print(privacy_status_text(event), style=colour('MUTED'), markup=False)
            elif kind == "tool_call":
                tools.append(event["name"])
            elif kind in {"approval", "question"}:
                # `ask` is the noninteractive/script interface. Do not silently
                # leave it waiting ten minutes for an approval it cannot show.
                guidance = "This action needs approval. Continue in interactive Kova chat to approve it."
                client.answer_approval(chat_id, event["id"], "deny", guidance)
                _stderr.print(guidance, style=colour('WARNING'), markup=False)
            elif kind == "error":
                if pieces and not as_json:
                    print()
                _ask_error(event["message"], as_json=as_json)
            elif kind == "done":
                used_model = event.get("model", used_model)
                usage = event.get("usage")
                break
    except KovaError as exc:
        _ask_error(str(exc), as_json=as_json)
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
            console.print(f"## {name} (failed in {ms} ms: {error})", style=colour('ERROR'))
        else:
            console.print(f"## {name} ({ms} ms)", style=colour('ACCENT'))
            console.print(entry.get("text", ""), markup=False)
        console.print()


@app.command(hidden=True)
def chat(
    bot: str | None = typer.Option(None, "--bot", help="Start this chat as a bot, by handle or id."),
    new: bool = typer.Option(False, "--new", help="Start a fresh chat instead of the latest."),
    chat: str | None = typer.Option(None, "--chat", help="Continue an existing chat by ID."),
    resume: str | None = typer.Option(None, "--resume", help="Continue a chat by its number from: kova chats"),
    no_think: bool = typer.Option(False, "--no-think", help="Hide the model's reasoning."),
    mode: str | None = typer.Option(None, "--mode", help="Start the chat in this mode: chat or work."),
    no_web: bool = typer.Option(False, "--no-web", help="Turn web search off for this chat."),
    incognito: bool = typer.Option(False, "--incognito", help="Keep this chat in memory only; no tools or memories."),
):
    """Old name for plain ``kova``; kept so existing habits and scripts still work."""
    _start_chat(new, chat, resume, no_think, mode, no_web, bot, **({"incognito": True} if incognito else {}))


def _start_chat(new: bool, chat: str | None, resume: str | None, no_think: bool,
                mode: str | None, no_web: bool, bot: str | None = None,
                incognito: bool = False) -> None:
    _check_mode(mode)
    if incognito and (chat or resume):
        _fail("Use --incognito for a new chat, without --chat or --resume.")
    if incognito and bot:
        _fail("Incognito chats cannot have bots.",
              hint="Drop --incognito, or leave out --bot.")
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
    bot_id = _resolve_bot_id(client, bot) if bot else None
    if resume:
        chat = _resolve_chat_id(client, resume)
    if bot_id is not None and chat is not None:
        try:
            existing = client.get_chat(chat)
        except KovaError as exc:
            if exc.status == 404:
                _fail(f'No chat "{chat}".')
            _fail(str(exc))
        if existing.get("kind") != "bot" or existing.get("bot_id") != bot_id:
            _fail(f'That chat is not with "@{bot}".',
                  hint=f"Start a new one with: kova chat --bot {bot_id} --new")
    _run_chat(client, chat_id=chat, new=new, no_think=no_think, mode=mode,
              web=False if no_web else None, console=console,
              bot_id=bot_id,
              **({"incognito": True} if incognito else {}))


@app.command()
def setup(
    url: str | None = typer.Option(None, "--url", help="Model server URL, for example http://192.168.1.20:8080."),
    model: str | None = typer.Option(None, "--model", help="Model name to use."),
    name: str | None = typer.Option(None, "--name", help="Name to save this model under."),
    key: str | None = typer.Option(None, "--key", help="API key, if the server needs one."),
    yes: bool = typer.Option(False, "--yes", "-y", help="Run without prompts. Requires --url; credentials are still validated."),
):
    """Connect a provider and choose models. No scan or test chat."""
    if not yes and not url and sys.stdin.isatty() and console.is_terminal:
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

    from filelock import FileLock, Timeout
    from kova.config import private_write

    cfg = load_config()
    pidfile = cfg.home / "server.pid"
    try:
        with FileLock(cfg.home / ".server-start.lock", timeout=0):
            private_write(pidfile, str(os.getpid()))
            try:
                uvicorn.run(create_app(cfg), host="127.0.0.1", port=cfg.port, timeout_graceful_shutdown=3)
            finally:
                pidfile.unlink(missing_ok=True)
    except Timeout:
        _fail("A Kova server is already running for this data directory.")


@app.command()
def restart():
    """Start a fresh background server and wait for it to answer."""
    from kova import build, update_runtime

    cfg = load_config()
    try:
        if process := update_runtime.running_server(cfg):
            update_runtime.stop(process)
        client = ensure_server(cfg)
        try:
            update_runtime.verify_server(cfg, build.installed())
        finally:
            client.http.close()
    except (lifecycle.LifecycleError, lifecycle.psutil.Error, KovaError, OSError) as exc:
        _fail(str(exc))
    _ok("Server is healthy on the installed build.")


@app.command("app-reconcile", hidden=True)
def app_reconcile() -> None:
    """Let the desktop shell reconcile an idle server before connecting."""
    from kova.update_runtime import restart_stale_server

    if restart_stale_server():
        _ok("Restarted the server on the installed build.")


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
        configured = any(m.model.strip() for m in cfg.models if m.name == info.get("model"))
        model_state = "reachable" if info.get("model_ok") else "not reachable" if configured else "not configured"
        model = Text.assemble(
            dot(colour('SUCCESS') if info.get("model_ok") else colour('WARNING')),
            ("  " + name, ""),
            ("  ·  " + model_state, colour('SUCCESS') if info.get("model_ok") else colour('WARNING')),
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
                                 "Kova status", tone=colour('ERROR')))
        _muted("It starts by itself when you run kova.")
        _show_update_hint(cfg)
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
                             tone=colour('ACCENT') if info.get("model_ok") else colour('WARNING')))
    if not info.get("model_ok"):
        _muted("To configure or reconnect your model, run: kova setup")
    if info.get("restart_available"):
        _muted("A newer Kova is installed. Run: kova restart")
    _show_update_hint(cfg)


def _show_update_hint(cfg) -> None:
    if console.is_terminal and (message := lifecycle.update_hint(cfg)):
        _muted(message)


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
    console.print(Text.assemble(("Today       ", colour('MUTED')), _spend_line(data["today"])))
    console.print(Text.assemble(("This month  ", colour('MUTED')), _spend_line(data["month"])))
    budget = data.get("budget") or {}
    if budget.get("monthly_usd"):
        line = f"Budget      ~{money(budget['spent_usd'])} of {money(budget['monthly_usd'])} this month"
        console.print(Text(line, style=colour('WARNING') if budget.get("warning") else colour('MUTED')))
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
        console.print(Text.assemble(("! ", f"bold {colour('WARNING')}"), (line, colour('WARNING'))))
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
        console.print(Text(f"Imported, but the server did not restart: {exc}", style=colour('WARNING')))
        return False
    return True


@app.command("export")
def export(path: str | None = typer.Argument(None, help="Zip path (default: ~/Kova-backup-YYYY-MM-DD.zip).")):
    """Write one zip with chats, projects, memory notes, reminders and a safe config."""
    try:
        stats = backup.export_archive(load_config(), path)
    except (backup.BackupError, OSError) as exc:
        _fail(str(exc))
    _ok(f"Backup written to {stats['path']}")
    _muted(f"  {stats['chats']} chats · {stats['projects']} projects · "
           f"{stats['notes']} notes · {stats['reminders']} reminders")
    _muted("API keys were left out of config.toml; the server token is not included.")


@app.command("import")
def import_archive(
    path: Path = typer.Argument(..., exists=True, dir_okay=False, readable=True, help="A Kova backup zip."),
    merge: bool = typer.Option(True, "--merge/--replace",
                               help="Merge by default; --replace clears existing chats, projects and Kova notes."),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip the replace confirmation."),
):
    """Restore a backup; merge is the default."""
    if not merge and not yes:
        if not typer.confirm("Replace all chats, projects, Kova memory notes and reminders?", default=False):
            _muted("Cancelled.")
            return
    cfg = load_config()
    try:
        stats = backup.import_archive(cfg, path, merge=merge)
    except (backup.BackupError, OSError) as exc:
        _fail(str(exc))
    _ok(f"Imported {stats['chats']} chats · {stats['projects']} projects · "
        f"{stats['notes']} notes · {stats['reminders']} reminders")
    if stats["projects_remapped"]:
        _muted(f"Restored {stats['projects_remapped']} projects under a new id because "
               "their id was already used by a different project.")
    if stats["chats_skipped"] or stats["notes_skipped"] or stats["reminders_skipped"]:
        _muted(f"Skipped {stats['chats_skipped']} chats, {stats['notes_skipped']} notes "
               f"and {stats['reminders_skipped']} reminders that already existed.")
    if stats["projects_skipped"]:
        _muted(f"Left {stats['projects_skipped']} projects alone: they were already here "
               "or could not be read.")
    if _restart_server(cfg):
        _muted("Server restarted to pick up the import.")
    else:
        _muted("Chats, memory and reminders are read live from disk; "
               "restart a foreground server if you changed its config.")


def _installed_theme_ids(client: KovaClient) -> list[str]:
    """Ids of the user's own theme files (built-ins are everywhere already)."""
    try:
        rows = client.themes()
    except KovaError:
        return []
    return sorted(row["id"] for row in rows if row.get("source") != "built-in" and row.get("id"))


def _format_changes(rows: list[tuple]) -> str:
    """One ``name: old -> new`` line per changed preference, for --dry-run."""
    return "  " + " · ".join(f"{name}: {old!r} -> {new!r}" for name, old, new in rows)


@_prefs.command("set")
def prefs_set(
    name: str = typer.Argument(..., help="A preference name, such as custom_css."),
    value: str = typer.Argument(..., help="Its new value, such as on or off."),
):
    """Change one UI preference from the terminal."""
    try:
        client = get_client()
        saved = client.set_preferences({name: value.strip()})
    except (KovaError, ui_prefs.UiPrefsError) as exc:
        _fail(str(exc))
    _ok(f"{name} = {saved.get(name, value.strip())}")


@_prefs.command("export")
def prefs_export(path: str | None = typer.Argument(
        None, help="JSON path (default: ~/Kova-ui-preferences-YYYY-MM-DD.json).")):
    """Save every UI preference to one validated JSON file."""
    try:
        client = get_client()
        target = ui_prefs.write_file(client.preferences(), path, _installed_theme_ids(client))
    except (ui_prefs.UiPrefsError, OSError) as exc:
        _fail(str(exc))
    _ok(f"UI preferences written to {target}")
    _muted(f"  Move them with: kova prefs import {target.name}")


@_prefs.command("import")
def prefs_import(
    path: Path = typer.Argument(..., exists=True, dir_okay=False, readable=True,
                                help="A preferences JSON file from `kova prefs export`."),
    dry_run: bool = typer.Option(False, "--dry-run",
                                 help="Show what would change without saving anything."),
):
    """Restore UI preferences, refusing a file that does not validate."""
    try:
        values, exported_themes = ui_prefs.read_export(path)
    except (ui_prefs.UiPrefsError, OSError) as exc:
        _fail(str(exc))
    try:
        client = get_client()
        current = client.preferences()
    except KovaError as exc:
        _fail(str(exc))
    rows = ui_prefs.diff_preferences(current, {**current, **values})
    missing = [name for name in exported_themes if name not in _installed_theme_ids(client)]
    if dry_run:
        if not rows:
            _muted("No changes: preferences already match.")
        else:
            _muted(f"Would change {len(rows)} preference{'s' if len(rows) != 1 else ''}:")
            _muted(_format_changes(rows))
        if missing:
            _muted(f"  Missing themes here: {', '.join(missing)}")
        elif exported_themes:
            _muted("  All installed themes are present here.")
        return
    try:
        saved = client.set_preferences(values)
    except KovaError as exc:
        _fail(str(exc))
    _ok(f"Restored {len(saved)} UI preferences")
    _muted("  " + " · ".join(f"{name} {value}" for name, value in sorted(saved.items())))
    if missing:
        _muted(f"  Missing themes here: {', '.join(missing)} (install the files to use them)")


@_themes.callback(invoke_without_command=True)
def themes_root(ctx: typer.Context):
    """List the themes Kova can use."""
    if ctx.invoked_subcommand is None:
        themes_list(as_json=False)


@_themes.command("list")
def themes_list(
    as_json: bool = typer.Option(False, "--json", help="Print one JSON array for scripts."),
):
    """List the built-in themes and the theme files in Kova's home."""
    try:
        entries = get_client().themes()
    except KovaError as exc:
        _fail(str(exc))
    if as_json:
        print(json.dumps(entries, ensure_ascii=False))
        return
    active = kova_theme.theme_id(active_theme())
    table = _table(caption=f"{len(entries)} theme(s) · built-in and user files · ● the terminal's theme")
    table.add_column("", no_wrap=True, width=1)
    table.add_column("Theme", no_wrap=True)
    table.add_column("Name")
    table.add_column("Appearance", no_wrap=True)
    table.add_column("Source", style=colour('MUTED'), no_wrap=True)
    for entry in entries:
        source = "built-in" if entry.get("source") == "built-in" else "file"
        mark = Text("●", style=colour('ACCENT')) if entry["id"] == active else ""
        if entry.get("error"):
            table.add_row(mark, Text(entry["id"], style=colour('ERROR')),
                          Text("broken", style=colour('ERROR')), "—", source)
        else:
            table.add_row(mark, Text(entry["id"], style="bold"), entry.get("name") or "—",
                          entry.get("appearance") or "—", source)
    console.print(table)
    for entry in entries:
        if entry.get("error"):
            _error_line(entry["error"])


def _gallery_matches(entry: dict, query: str) -> bool:
    """Every word of ``query`` appears in the theme's id, name, description, author or look."""
    haystack = " ".join(str(entry.get(key) or "") for key in
                        ("id", "name", "description", "author", "appearance")).lower()
    return all(word in haystack for word in query.lower().split())


def _gallery_status(entry: dict) -> str:
    if not entry.get("installed"):
        return "Available"
    return f"Update to {entry['version']}" if entry.get("update") else "Installed"


@_themes.command("gallery")
def themes_gallery(query: list[str] = typer.Argument(
        None, help="Show only themes whose name, description or look contains every word.")):
    """Browse bundled themes and themes from an explicitly configured catalog."""
    try:
        entries = get_client().theme_gallery()
    except KovaError as exc:
        _fail(str(exc))
    words = " ".join(query or []).strip()
    shown = [entry for entry in entries if _gallery_matches(entry, words)] if words else entries
    if not shown:
        _muted(f'No gallery theme matches "{words}". Run `kova themes gallery` to see them all.')
        return
    table = _table(caption="Install with: kova themes install <id> · "
                           "update with: kova themes update · remove with: kova themes remove <id>")
    table.add_column("ID", no_wrap=True)
    table.add_column("Name")
    table.add_column("Appearance", no_wrap=True)
    table.add_column("Version", no_wrap=True)
    table.add_column("Status", no_wrap=True)
    table.add_column("Description", overflow="fold")
    for entry in shown:
        table.add_row(Text(entry["id"], style="bold"), Text(entry["name"]), entry["appearance"],
                      entry.get("version") or "—", _gallery_status(entry), Text(entry["description"]))
    console.print(table)


@_themes.command("install")
def themes_install(theme_id: str = typer.Argument(..., help="A theme ID from `kova themes gallery`.")):
    """Install one gallery theme without changing the active theme."""
    try:
        installed = get_client().install_gallery_theme(theme_id)
    except KovaError as exc:
        _fail(str(exc))
    _ok(f"Installed {installed['id']}")


@_themes.command("update")
def themes_update(theme_id: str | None = typer.Argument(
        None, help="One installed gallery theme; leave out to update every one that has a newer copy.")):
    """Replace installed gallery themes with their newer gallery copies."""
    client = get_client()
    try:
        if theme_id is None:
            pending = [entry["id"] for entry in client.theme_gallery() if entry.get("update")]
            if not pending:
                _muted("Every installed gallery theme is up to date.")
                return
        else:
            pending = [theme_id]
        for name in pending:
            client.update_gallery_theme(name)
            _ok(f"Updated {name}")
    except KovaError as exc:
        _fail(str(exc))


@_themes.command("remove")
def themes_remove(theme_id: str = typer.Argument(..., help="An installed theme ID from `kova themes list`.")):
    """Delete an installed theme file. Built-in themes cannot be removed."""
    try:
        get_client().remove_theme(theme_id)
    except KovaError as exc:
        _fail(str(exc))
    _ok(f"Removed {theme_id}")
    _muted("If it was your chosen theme, Kova follows your device until you pick another.")


@_themes.command("show")
def themes_show(
        theme_id: str = typer.Argument(..., help="A theme ID."),
        gallery: bool = typer.Option(False, "--gallery", help="Preview a gallery theme without installing it.")):
    """Print one theme's tokens, resolved over the base it extends."""
    try:
        client = get_client()
        data = client.preview_gallery_theme(theme_id) if gallery else client.theme(theme_id)
    except KovaError as exc:
        _fail(str(exc))
    header = " ".join(str(part) for part in (data.get("name"), data.get("version")) if part)
    _ok(f"{header or data['id']} · {data['appearance']}")
    merged = data.get("tokens") or {}
    terminal = data.get("terminal") or []
    table = _table(caption=f"{len(merged)} semantic tokens · resolved over {data.get('extends') or 'its base'}")
    table.add_column("Token", style=colour('ACCENT_SOFT'), no_wrap=True)
    table.add_column("Value", overflow="fold")
    for name in sorted(merged):
        table.add_row(name, str(merged[name]))
    console.print(table)
    if terminal:
        _muted("Terminal palette: " + " ".join(terminal))


@_themes.command("try")
def themes_try(theme_id: str = typer.Argument(..., help="An installed or gallery theme ID.")):
    """Try a terminal theme for 30 seconds without changing it unless kept."""
    from kova.tui.theme_trial import keep_preview

    if not sys.stdin.isatty() or not console.is_terminal:
        _fail("Trying a theme needs an interactive terminal.",
              hint="Run: kova themes show ID --gallery")
    client = None
    try:
        data = themes.get(kova_theme.theme_id(theme_id), kova_home())
    except themes.ThemeError:
        try:
            client = get_client()
            data = client.preview_gallery_theme(theme_id)
        except KovaError as exc:
            _fail(str(exc))

    if not keep_preview(data):
        _muted("Preview reverted. Your terminal theme did not change.")
        return
    if client is not None:
        try:
            client.save_theme(data["document"])
        except KovaError as exc:
            _fail(str(exc))
    themes_use(data["id"], app_too=False)


def _terminal_theme_setting(name: str, home: Path) -> str:
    """Validate a ``[tui] theme`` value: ``dark``, ``light`` or an installed theme id."""
    text = name.strip().lower()
    themes.get(kova_theme.theme_id(text), home)  # raises ThemeError naming the problem
    return text


#: The shared UI preference that paints the same built-in as a terminal theme id.
_APP_THEME = {"kova-dark": "dark", "kova-light": "light"}


@_themes.command("use")
def themes_use(
        theme_id: str = typer.Argument(..., help="A theme id from `kova themes list`, or dark/light."),
        app_too: bool = typer.Option(False, "--app",
                                     help="Also make it the app's theme on every paired device.")):
    """Paint the terminal with a theme; saved as [tui] theme."""
    cfg = load_config()
    try:
        value = _terminal_theme_setting(theme_id, cfg.home)
    except themes.ThemeError as exc:
        _fail(str(exc), hint="Run: kova themes list")
    data = themes.get(kova_theme.theme_id(value), cfg.home)
    cfg.tui_theme = value
    save_config(cfg)
    if app_too:
        try:
            get_client().set_preferences({"theme": _APP_THEME.get(data["id"], data["id"])})
        except KovaError as exc:
            _fail(f"The terminal now uses {data['name']}, but the app's theme did not change: {exc}")
    _ok(f"{data['name']} is now the terminal's theme" + (" and the app's" if app_too else "") + ".")
    if os.environ.get("KOVA_THEME", "").strip():
        _muted("KOVA_THEME is set in this shell and still wins; unset it to see the change.")


@_themes.command("check")
def themes_check(path: Path = typer.Argument(
        ..., exists=True, dir_okay=False, readable=True,
        help="A theme JSON file to validate before installing it.")):
    """Validate a theme file and print the tokens it resolves to."""
    try:
        data = themes.load_file(path)
    except themes.ThemeError as exc:
        _fail(str(exc))
    _ok(f"{data['name']} {data['version']} is valid")
    base = data.get("extends") or themes.default_base(data["appearance"])
    merged = {name: value for name, value in themes.resolve(data).items() if name != "terminal"}
    table = _table(caption=f"{len(merged)} tokens resolved over {base}")
    table.add_column("Token", style=colour('ACCENT_SOFT'), no_wrap=True)
    table.add_column("Value", overflow="fold")
    for name in sorted(merged):
        table.add_row(name, str(merged[name]))
    console.print(table)
    _muted(f"Install it by copying the file into {themes.theme_dir(kova_home())} "
           "and running: kova themes list")


# ---------------------------------------------------------------------------
# Command groups
# ---------------------------------------------------------------------------


def _list_models(client: KovaClient) -> None:
    active = client.health(probe=False)["model"]
    models = client.models_list()
    if not models:
        _empty("No models configured.", "Run: kova setup to find a model server.")
        return
    table = _table(caption=f"{len(models)} configured · active: {active} · switch: kova model use")
    table.add_column("#", justify="right", style=colour('ACCENT_SOFT'), no_wrap=True)
    table.add_column("Name", no_wrap=True)
    table.add_column("Model", no_wrap=True)
    table.add_column("Provider", style=colour('MUTED'), overflow="fold")
    table.add_column("Server", style=colour('MUTED'), overflow="fold")
    for number, entry in enumerate(sorted(models, key=lambda m: (m.get("provider_name", ""), m["name"])), start=1):
        selected = entry["name"] == active
        badge = Text.assemble(
            ("● " if selected else "  ", colour('SUCCESS') if selected else ""),
            (entry["name"], f"bold {colour('ACCENT')}" if selected else ""),
        )
        table.add_row(str(number), badge, entry["model"], entry.get("provider_name", "Custom"), entry["base_url"])
    console.print(table)


@_model.command("list")
def model_list():
    """List every configured model and mark the active one."""
    _list_models(get_client())


@_model.command("add")
def model_add(
    name: str = typer.Argument(..., help="Name to save this server under, for example lab."),
    address: str = typer.Argument(..., help="Server address: 192.168.1.50, homelab:8080, a URL, or openrouter."),
    model: str | None = typer.Argument(None, help="Model id; optional when the server has only one."),
    key: str | None = typer.Option(None, "--key", help="API key, if the server needs one."),
):
    """Add a model server by hand. Loose addresses work; Kova finds the right URL.

    For a hosted provider, `kova providers add` is the easier path: it asks for
    the key, lists the provider's models and makes the chosen one active.
    """
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


def _model_rows(client: KovaClient) -> tuple[list[model_picker.Choice], str]:
    """Every configured model as picker rows, plus the saved active name."""
    return (model_picker.choices(client.models_list()),
            str(client.health(probe=False).get("model") or ""))


def _pick_model(rows: list[model_picker.Choice], active: str) -> model_picker.Choice | None:
    """The arrow-key picker; a pipe gets the numbered list and a one-line hint."""
    from kova.tui import prompts

    try:
        return model_picker.pick(rows, active=active)
    except prompts.Unavailable:
        for line in model_picker.numbered(rows, active=active):
            console.print(Text(line, style=colour("MUTED")))
        _fail("This terminal cannot draw the picker.",
              hint="Name one instead, for example: kova model use openai/gpt-4o")


@_model.command("use")
def model_use(
    name: str | None = typer.Argument(
        None, help="A saved name, <provider>/<model>, or a model id. Omit to pick from a list."),
):
    """Switch the active provider and model. Saved for this and every future chat.

    With no name, opens a picker of every connected provider and its models:
    arrow keys move, a number picks, Enter saves. The current choice is marked.
    """
    client = get_client()
    rows, active = _model_rows(client)
    if not rows:
        _empty("No models configured.", "Run: kova providers add to connect one.")
        raise typer.Exit(1)
    if name is None:
        chosen = _pick_model(rows, active)
        if chosen is None:
            _muted("Nothing changed.")
            return
    else:
        chosen = model_picker.resolve(rows, name)
        if chosen is None:
            options = model_picker.similar(rows, name)
            _fail(f'No model named "{name}".',
                  hint=f"Try one of: {', '.join(options)}" if options else "Run: kova model list")
    try:
        client.model_use(chosen.name)
    except KovaError as exc:
        if exc.status == 404:
            _fail(f'No model named "{name}".', hint="Run: kova model list")
        raise
    _ok(f"Now using {chosen.shown} on {chosen.provider}.")
    _muted("Saved for this and every future chat. Change it any time with: kova model use")


@_model.command("remove-local")
def model_remove_local(
    provider: str = typer.Argument(..., help="The local provider's saved name, from kova model list."),
):
    """Remove a managed local model's weights and connection (not an engine install)."""
    result = get_client().local_model_remove(provider)
    _ok(result['message'])


_mcp = typer.Typer(no_args_is_help=True, help="Use the tools of MCP (Model Context Protocol) servers.")
app.add_typer(_mcp, name="mcp")

_MCP_STATE = {"connected": "SUCCESS", "connecting": "WARNING", "error": "ERROR", "off": "MUTED"}


def _mcp_error_line(message: str) -> None:
    """A failed server's reason; a clean exit is a note, not a red failure."""
    if "(exit code 0)" in message:
        _muted(message)
    else:
        _error_line(message)


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
        _mcp_error_line(row["error"])
    if not row["tools"]:
        if row["state"] == "connected":
            _muted(f"{row['name']} offers no tools.")
        return
    table = _table(caption=f"{len(row['tools'])} tools · Kova asks before each call unless always allowed")
    table.add_column("Tool", no_wrap=True)
    table.add_column("Kova name", style=colour('MUTED'), no_wrap=True)
    table.add_column("On", no_wrap=True)
    table.add_column("Description", style=colour('MUTED'), overflow="fold", max_width=60)
    for tool in row["tools"]:
        table.add_row(tool["name"], tool["kova_name"], "yes" if tool["enabled"] else "no",
                      _clip(tool["description"].split("\n")[0], 60))
    console.print(table)


@_mcp.command("list")
def mcp_list(
    as_json: bool = typer.Option(False, "--json", help="Print one JSON array for scripts."),
):
    """Show MCP servers, whether they are connected, and their tools."""
    rows = get_client().mcp_list()
    if as_json:
        print(json.dumps(rows, ensure_ascii=False))
        return
    if not rows:
        _empty("No MCP servers yet.",
               "Add one: kova mcp add NAME -- COMMAND [ARGS...]   or   kova mcp add NAME --url URL")
        return
    table = _table(caption=f"{len(rows)} server(s) · docs/CONFIG.md#mcp-servers")
    table.add_column("Server", no_wrap=True)
    table.add_column("Status", no_wrap=True)
    table.add_column("Tools", justify="right", no_wrap=True)
    table.add_column("Runs", style=colour('MUTED'), overflow="fold")
    table.add_column("Source", style=colour('MUTED'), no_wrap=True)
    for row in rows:
        enabled = sum(1 for tool in row["tools"] if tool["enabled"])
        tools = f"{enabled}/{len(row['tools'])}" if row["tools"] else "-"
        table.add_row(row["name"], Text(row["state"], style=colour(_MCP_STATE[row["state"]]) if row["state"] in _MCP_STATE else ""), tools,
                      f"{row['transport']} · {_clip(row['target'], 70)}",
                      f"plugin: {row['plugin']}" if row.get("plugin") else "")
    console.print(table)
    for row in rows:
        if row["error"]:
            _mcp_error_line(row["error"])


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
            _mcp_error_line(row["error"])
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
    table.add_column("#", justify="right", style=colour('ACCENT_SOFT'), no_wrap=True)
    table.add_column("Note", no_wrap=True, max_width=32, overflow="ellipsis")
    table.add_column("Tags", style=colour('MUTED'), no_wrap=True, max_width=24, overflow="ellipsis")
    table.add_column("From", style=colour('MUTED'), no_wrap=True)
    table.add_column("Updated", style=colour('MUTED'), no_wrap=True)
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
def memory_list(q: str | None = typer.Argument(None),
                project: str | None = typer.Option(None, "--project",
                                                    help="List one project's own memory by ID.")):
    notes = get_client().memory_list(q, project_id=project)
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
def memory_search(q: str, project: str | None = typer.Option(None, "--project",
                                                             help="Search one project's own memory.")):
    notes = get_client().memory_list(q, project_id=project)
    if not notes:
        _empty(f'No notes match "{q}".', "Try a different word, or run: kova memory list")
        return
    word = "note" if len(notes) == 1 else "notes"
    console.print(_memory_table(notes, caption=f'{len(notes)} {word} · matching "{q}"'))


@_memory.command("show")
def memory_show(slug: str, project: str | None = typer.Option(None, "--project",
                                                              help="Read from one project's memory.")):
    for note in get_client().memory_list(None, project_id=project):
        if note["slug"] == slug:
            console.print(Text(note["title"], style=f"bold {colour('ACCENT')}"))
            console.print(Text(note["slug"], style=colour('MUTED')))
            console.print(Text(
                f"{_origin_label(note)}"
                + (f" · from chat {note['source']}" if note.get("source") else "")
                + (f" · first recorded {_relative_when(note.get('created'))}"
                   if note.get("created") else "")
                + f" · updated {_relative_when(note.get('updated'))}",
                style=colour('MUTED')))
            console.print()
            console.print(note["body"], markup=False, soft_wrap=True)
            return
    _fail(f'No note named "{slug}".')


@_memory.command("add")
def memory_add(title: str, body: str, tags: list[str] = typer.Option([], "--tags"),
               project: str | None = typer.Option(None, "--project",
                                                   help="Save into one project's own memory.")):
    note = get_client().memory_add(title, body, tags, project_id=project)
    _ok(f"Saved {note['title']}.")


@_memory.command("edit")
def memory_edit(
    slug: str,
    body: str | None = typer.Option(None, "--body", help="Replacement text for the note."),
    title: str | None = typer.Option(None, "--title", help="New title for the note."),
    tag: list[str] = typer.Option([], "--tag", help="Replace the note's tags; repeatable."),
    project: str | None = typer.Option(None, "--project", help="Edit one project's memory."),
):
    """Rewrite one note in place; the slug and where it came from are kept."""
    if body is None and title is None and not tag:
        _fail("Nothing to change.", hint=f"Usage: kova memory edit {slug} --body \"new text\"")
    note = get_client().memory_edit(slug, title=title, body=body, tags=tag or None,
                                    project_id=project)
    _ok(f"Updated {note['title']}.")


@_memory.command("rm")
def memory_rm(slug: str, project: str | None = typer.Option(None, "--project",
                                                             help="Remove from one project's memory.")):
    if get_client().memory_delete(slug, project_id=project):
        _ok(f"Removed {slug}.")
    else:
        _fail(f'No note named "{slug}".')


@_memory.command("forget")
def memory_forget(slug: str, project: str | None = typer.Option(None, "--project",
                                                                 help="Forget from one project's memory.")):
    """Delete one note; the same as `memory rm`, named for the review notices."""
    if get_client().memory_forget(slug, project_id=project):
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
    table.add_column("When", style=colour('MUTED'), no_wrap=True)
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
        table.add_column("#", justify="right", style=colour('ACCENT_SOFT'), no_wrap=True)
        table.add_column("When", style=colour('MUTED'), no_wrap=True)
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
    table.add_column("#", justify="right", style=colour('ACCENT_SOFT'), no_wrap=True)
    table.add_column("When", no_wrap=True)
    table.add_column("In", style=colour('MUTED'), no_wrap=True)
    table.add_column("Reminder", overflow="ellipsis")
    for reminder in reminders:
        try:
            when = _reminder_time(reminder["when"], now)
        except (TypeError, ValueError):
            table.add_row(str(reminder["id"]), Text(reminder["when"], style=colour('WARNING')), "—",
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
        (f"Reminder #{result['id']}", f"bold {colour('ACCENT')}"),
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
    table.add_column("#", justify="right", style=colour('ACCENT_SOFT'), no_wrap=True)
    table.add_column("Job", overflow="ellipsis")
    table.add_column("Kind", style=colour('MUTED'), no_wrap=True)
    table.add_column("Schedule", no_wrap=True)
    table.add_column("Next", no_wrap=True)
    table.add_column("Runs", justify="right", style=colour('MUTED'), no_wrap=True)
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
    console.print(Text(f"{title}:", style=f"bold {colour('MUTED')}"))
    if not lines:
        console.print(Text("  (none)", style=colour('FAINT')))
        return
    for line in lines:
        console.print(Text("  " + line, style=colour('MUTED')))


def _plugin_summary(plugin) -> None:
    """The install-time confirmation: exactly what this plugin adds."""
    from kova import plugins

    info = plugins.describe(plugin)
    console.print()
    console.print(Text(f"{info['name']} {info['version']}", style=f"bold {colour('ACCENT')}"))
    if info["description"]:
        console.print(Text(info["description"], style=colour('MUTED')))
    meta = [part for part in (f"by {info['author']}" if info["author"] else "",
                              info["homepage"]) if part]
    if meta:
        console.print(Text("  ".join(meta), style=colour('FAINT')))
    if info["min_kova"]:
        console.print(Text(f"  requires Kova >= {info['min_kova']}", style=colour('FAINT')))

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
def plugin_list(
    as_json: bool = typer.Option(False, "--json", help="Print one JSON array for scripts."),
):
    """List installed plugins and their enabled state."""
    from kova import plugins

    items = plugins.installed()
    if as_json:
        print(json.dumps([{
            "name": plugin.name,
            "version": plugin.version,
            "description": plugin.description,
            "author": plugin.author,
            "homepage": plugin.homepage,
            "source": plugin.source,
            "enabled": plugin.enabled,
        } for plugin in items], ensure_ascii=False))
        return
    if not items:
        _empty("No plugins installed.", "Add one with: kova plugin install <path|git url>")
        return
    table = _table(caption=f"{len(items)} installed")
    table.add_column("Plugin", no_wrap=True)
    table.add_column("Version", no_wrap=True)
    table.add_column("State", no_wrap=True)
    table.add_column("About", overflow="fold")
    for plugin in items:
        state = Text("enabled", style=colour('SUCCESS')) if plugin.enabled else Text("disabled", style=colour('MUTED'))
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
        return Text("coming soon", style=colour('FAINT'))
    installed = catalog.installed_version(entry, home)
    if installed is None:
        return Text("available", style=colour('MUTED'))
    if installed != entry.version:
        return Text(f"{installed} → {entry.version}", style=colour('ACCENT'))
    return Text("installed", style=colour('SUCCESS'))


def _catalog_summary(staged) -> None:
    """The confirmation screen: where it comes from, its checksum, what it adds."""
    info = staged.describe()
    if staged.plugin is not None:
        _plugin_summary(staged.plugin)
    else:
        skill = info["skill"]
        console.print()
        console.print(Text(f"{skill['name']} (skill) {info['version']}", style=f"bold {colour('ACCENT')}"))
        console.print(Text(skill["description"], style=colour('MUTED')))
        console.print()
        _plugin_section("Instructions added to the prompt when the skill is loaded",
                        skill["body"].splitlines())
        console.print()
    _plugin_section("Catalog says", info["permissions"])
    console.print(Text(f"Source: {info['source']}", style=colour('FAINT')))
    console.print(Text(f"SHA-256: {info['sha256']} (verified)", style=colour('FAINT')))
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
    console.print(Text.assemble((entry.name, f"bold {colour('ACCENT')}"), (f"  {entry.kind} {entry.version}  ", colour('MUTED')),
                                _catalog_state(entry, home)))
    if entry.description:
        console.print(Text(entry.description))
    meta = [part for part in (f"by {entry.author}" if entry.author else "", entry.homepage) if part]
    if meta:
        console.print(Text("  ".join(meta), style=colour('FAINT')))
    if entry.tags:
        console.print(Text("Tags: " + ", ".join(entry.tags), style=colour('FAINT')))
    console.print()
    _plugin_section("What it may do", entry.permissions)
    if entry.available:
        console.print(Text(f"Source: {entry.url}", style=colour('FAINT')))
        console.print(Text(f"SHA-256: {entry.sha256}", style=colour('FAINT')))
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
    console.print(Text.assemble((row["name"], f"bold {colour('ACCENT')}"), (f"  {_skill_origin(row)}", colour('MUTED'))))
    console.print(Text(row["description"], style=colour('MUTED')))
    console.print(Text(row["id"], style=colour('FAINT')))
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


_CEILINGS = {"manual": "asks before each tool call",
             "auto": "runs tools without asking when the chat allows it",
             "full": "no sandbox; every tool, no questions"}

#: A bot's colour key is a semantic token; the terminal paints each one with the
#: role that carries the same meaning (``info`` is ``--status-info``).
_BOT_ROLES = {"accent": "ACCENT", "success": "SUCCESS", "warning": "WARNING",
              "danger": "ERROR", "info": "MEMORY", "keyword": "CODE",
              "string": "SUCCESS", "function": "CODE"}

#: Every field ``kova bots set`` and ``kova bots edit`` can change, with its path
#: in ``bot.toml`` and how its value is read from the command line. ``id`` is
#: absent on purpose: it is the folder name, so a bot is recreated, never moved.
_BOT_FIELDS: dict[str, tuple[tuple[str, ...], str]] = {
    "name": (("name",), "text"),
    "handle": (("handle",), "text"),
    "description": (("description",), "text"),
    "colour": (("avatar", "colour"), "text"),
    "icon": (("avatar", "icon"), "text"),
    "model": (("model", "name"), "text"),
    "mode": (("model", "mode"), "text"),
    "effort": (("model", "reasoning_effort"), "text"),
    "web": (("model", "web"), "bool"),
    "permission": (("permissions", "ceiling"), "text"),
    "ceiling": (("permissions", "ceiling"), "text"),
    "tools": (("permissions", "tools"), "list"),
    "deny": (("permissions", "deny"), "list"),
    "folder": (("permissions", "folder"), "text"),
    "inherit-rules": (("permissions", "inherit_rules"), "bool"),
    "memory": (("memory", "own"), "bool"),
    "budget": (("memory", "budget"), "int"),
    "user-memory": (("memory", "user_memory"), "text"),
    "answers": (("group", "answers"), "text"),
    "max-rounds": (("group", "max_rounds"), "int"),
    "max-output-tokens": (("group", "max_output_tokens"), "int"),
}
_BOT_PATHS = {".".join(path): (path, kind) for path, kind in _BOT_FIELDS.values()}


def _bot_role(bot: dict) -> str:
    """The terminal role a bot's colour key paints with."""
    return _BOT_ROLES.get(str(bot.get("colour") or ""), "MUTED")


def _slug(name: str) -> str:
    """A handle from a display name: lowercase words joined by dashes."""
    return re.sub(r"[^a-z0-9]+", "-", name.casefold()).strip("-")[:32].strip("-")


def _bot_handle(bot: dict) -> str:
    return str(bot.get("handle") or bot.get("id") or "")


def _resolve_bot_id(client: KovaClient, ref: str) -> str:
    """A bot's id from its id, its @handle, or a unique part of either or its name."""
    needle = ref[1:] if ref.startswith("@") else ref
    needle = needle.strip().casefold()
    bots = client.bots_list()
    exact = [bot for bot in bots
             if needle in (str(bot.get("id") or "").casefold(),
                           str(bot.get("handle") or "").casefold())]
    found = exact or [bot for bot in bots
                      if needle in str(bot.get("id") or "").casefold()
                      or needle in str(bot.get("handle") or "").casefold()
                      or needle in str(bot.get("name") or "").casefold()]
    if len(found) == 1:
        return str(found[0]["id"])
    if not found:
        _fail(f'No bot "{ref}".', hint="Run 'kova bots list' to see them.")
    names = ", ".join(f"@{_bot_handle(bot)}" for bot in found[:6])
    _fail(f'"{ref}" matches more than one bot.', hint=f"Pick one of: {names}")
    return ""  # unreachable


def _unique_handle(client: KovaClient, wanted: str) -> str:
    """A free handle close to ``wanted``: the name itself, else ``name-2``, ``name-3``."""
    existing = client.bots_list()
    taken = {str(bot.get("handle") or "").casefold() for bot in existing}
    taken |= {str(bot.get("id") or "").casefold() for bot in existing}
    if wanted not in taken and wanted not in RESERVED_HANDLES:
        return wanted
    number = 2
    while f"{wanted}-{number}" in taken or f"{wanted}-{number}" in RESERVED_HANDLES:
        number += 1
    return f"{wanted}-{number}"


def _tools_from(text: str) -> list[str]:
    """A comma-separated tool allowlist; ``none`` (or nothing) means no tools."""
    cleaned = text.strip()
    if cleaned.casefold() in ("", "none", "-"):
        return []
    return [part.strip() for part in cleaned.split(",") if part.strip()]


def _bot_value(kind: str, value: str, field: str):
    """Read one command-line value as the field's type, or fail in one line."""
    if kind == "list":
        return _tools_from(value)
    if kind == "bool":
        lowered = value.strip().casefold()
        if lowered in ("on", "true", "yes", "1"):
            return True
        if lowered in ("off", "false", "no", "0"):
            return False
        _fail(f'"{field}" takes on or off, not "{value}".')
    if kind == "int":
        try:
            return int(value.strip())
        except ValueError:
            _fail(f'"{field}" takes a whole number, not "{value}".')
    return value.strip()


def _nested(path: tuple[str, ...], value) -> dict:
    """One changed field as the nested table ``PATCH /bots`` expects."""
    for key in reversed(path):
        value = {key: value}
    return value


def _bot_field(field: str) -> tuple[tuple[str, ...], str]:
    key = field.strip().casefold().replace("_", "-")
    if key in _BOT_FIELDS:
        return _BOT_FIELDS[key]
    if key in _BOT_PATHS:
        return _BOT_PATHS[key]
    _fail(f'Unknown bot field "{field}".',
          hint="Use " + ", ".join(sorted(_BOT_FIELDS)) + " (or a dotted path like model.name).")


def _read_persona(path: Path) -> str:
    try:
        return Path(path).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        _fail(f'Cannot read "{path}": {exc}')


def _first_line(text: str, limit: int = 80) -> str:
    line = " ".join(text.split())
    return line if len(line) <= limit else line[:limit - 1] + "…"


def _tools_summary(patterns) -> str:
    """A short human summary of a tool allowlist, for tables."""
    parts: list[str] = []
    for pattern in patterns or ():
        lowered = str(pattern).casefold()
        label = ("web" if lowered.startswith("web") else
                 "memory" if lowered.startswith("memory") else
                 "commands" if lowered in ("run_command", "bash") else
                 "files" if lowered.endswith("_file") or lowered in
                 ("list_files", "search_files") else
                 "jobs" if lowered.startswith("job") else
                 "skills" if lowered.startswith("skill") else None)
        if label is None:
            label = str(pattern)
        if label not in parts:
            parts.append(label)
    return ", ".join(parts) or "no tools"


@_bots.command("list")
def bots_list(
    as_json: bool = typer.Option(False, "--json", help="Print one JSON array for scripts."),
):
    """List your bots. Add one with ``kova bots create``, chat with ``kova chat --bot``."""
    bots = get_client().bots_list()
    if as_json:
        print(json.dumps(bots, indent=2, ensure_ascii=False))
        return
    if not bots:
        _empty("No bots yet.", "Add a starter bot with: kova bots create --from researcher")
        return
    busy = any(bot.get("status") in ("working", "blocked-on-approval")
               for bot in bots if bot.get("state") == "ok")
    caption = ("● answering  ◌ waiting for you  ·  " if busy else "") + \
        "Chat with one: kova chat --bot <handle>"
    table = _table(title="Bots", caption=caption)
    table.add_column("", no_wrap=True)
    table.add_column("Handle", no_wrap=True)
    table.add_column("Name", no_wrap=True)
    table.add_column("Model", style=colour('MUTED'), no_wrap=True)
    table.add_column("Permission", style=colour('MUTED'), no_wrap=True)
    table.add_column("What it does", style=colour('MUTED'))
    for bot in bots:
        handle = f"@{_bot_handle(bot)}"
        if bot.get("state") != "ok":
            table.add_row(Text(SYMBOL_ERROR, style=f"bold {colour('ERROR')}"),
                          Text(handle, style=colour('ERROR')),
                          Text("file is broken", style=colour('ERROR')),
                          "", "", _first_line(str(bot.get("error") or ""), 70))
            continue
        status = bot.get("status") or "idle"
        marker = (Text("●", style=f"bold {colour('ACCENT')}") if status == "working"
                  else Text("◌", style=colour('WARNING'))
                  if status == "blocked-on-approval" else Text(" "))
        table.add_row(marker, handle,
                      Text(str(bot.get("name") or ""), style=f"bold {colour(_bot_role(bot))}"),
                      Text(str(bot.get("model") or "active"), style=colour('MUTED')),
                      Text(str(bot.get("ceiling") or "manual"), style=colour('MUTED')),
                      _first_line(str(bot.get("description") or ""), 60))
    console.print(table)


@_bots.command("show")
def bots_show(ref: str = typer.Argument(..., help="The bot: its handle or id.")):
    """Show one bot: its model, permissions, tools, memory and role prompt."""
    client = get_client()
    bot_id = _resolve_bot_id(client, ref)
    try:
        bot = client.bot_get(bot_id)
    except KovaError as exc:
        if exc.status == 404:
            _fail(f'No bot "{ref}".', hint="Run 'kova bots list'.")
        _fail(f'"{bot_id}" is broken: {exc}',
              hint="Fix bots/" + bot_id + "/bot.toml by hand, or remove it with: kova bots rm " + bot_id)
    if bot.get("state") != "ok":
        _fail(f'"{bot_id}" is broken: {bot.get("error")}',
              hint="Fix bots/" + bot_id + "/bot.toml by hand, or remove it with: kova bots rm " + bot_id)
    definition = bot.get("definition") or {}
    permissions = definition.get("permissions") or {}
    model = definition.get("model") or {}
    memory = definition.get("memory") or {}
    group = definition.get("group") or {}

    head = Text(f"{bot.get('name')} ", style=f"bold {colour(_bot_role(bot))}")
    head.append(f"@{_bot_handle(bot)}", style=colour('MUTED'))
    console.print(head)
    if bot.get("description"):
        console.print(Text(str(bot["description"]), style=colour('MUTED')))
    console.print()

    def row(label: str, value: Text | str) -> None:
        line = Text("  ")
        line.append(label.ljust(11), style=colour('FAINT'))
        line.append_text(value if isinstance(value, Text) else Text(str(value)))
        console.print(line)

    ceiling = str(permissions.get("ceiling") or "manual")
    model_text = Text(str(model.get("name") or "the active model"), style="bold")
    model_text.append(f"  {model.get('mode') or 'work'}", style=colour('MUTED'))
    model_text.append(f"  web {'on' if model.get('web', True) else 'off'}", style=colour('MUTED'))
    if model.get("reasoning_effort"):
        model_text.append(f"  effort {model['reasoning_effort']}", style=colour('MUTED'))
    row("model", model_text)
    permission_text = Text(ceiling, style="bold")
    permission_text.append(f"  {_CEILINGS.get(ceiling, '')}", style=colour('MUTED'))
    row("permission", permission_text)
    row("tools", Text(", ".join(permissions.get("tools") or []) or "no tools",
                      style=colour('MUTED')))
    if permissions.get("deny"):
        row("never", Text(", ".join(permissions["deny"]), style=colour('MUTED')))
    row("folder", Text(str(permissions.get("folder") or f"Kova Files/bots/{bot_id}"),
                       style=colour('MUTED')))
    budget = int(memory.get("budget") or 0)
    memory_text = Text(f"{budget:,} characters" if budget else "unlimited",
                       style=colour('MUTED'))
    memory_text.append("  " + ("reads your notes" if memory.get("user_memory", "read") == "read"
                               else "your notes are private"), style=colour('MUTED'))
    row("memory", memory_text)
    row("in groups", Text(f"answers {'every message' if group.get('answers') == 'always' else 'when chosen'}"
                          f"  ·  {group.get('max_rounds')} model calls"
                          f"  ·  {int(group.get('max_output_tokens') or 0):,} output tokens",
                          style=colour('MUTED')))
    row("id", Text(bot_id, style=colour('MUTED')))
    for warning in bot.get("warnings") or []:
        console.print(Text(f"  {warning}", style=colour('WARNING')))
    if bot.get("persona"):
        console.print()
        console.print(Markdown(bot["persona"]))
    console.print()
    _muted(f"Change it with: kova bots set {bot_id} <field> <value>  ·  "
           f"chat with it: kova chat --bot {_bot_handle(bot)}")


@_bots.command("templates")
def bots_templates():
    """List the starter bots ``kova bots create --from`` can add."""
    table = _table(title="Starter bots", caption="Add one: kova bots create --from <handle>")
    table.add_column("Handle", no_wrap=True)
    table.add_column("Name", no_wrap=True)
    table.add_column("Tools", style=colour('MUTED'))
    table.add_column("What it does", style=colour('MUTED'))
    for template in bot_templates.list_templates():
        tools = template.definition()["permissions"]["tools"]
        table.add_row(f"@{template.handle}", Text(template.name, style="bold"),
                      _tools_summary(tools), template.description)
    console.print(table)


@_bots.command("create")
def bots_create(
    name: str | None = typer.Argument(None, help="The bot's name. Defaults to the starter's name."),
    from_template: str | None = typer.Option(None, "--from",
                                             help="Start from a starter bot: kova bots templates."),
    handle: str | None = typer.Option(None, "--handle",
                                      help="Its @handle, also its folder name. Defaults to the name."),
    description: str | None = typer.Option(None, "--description",
                                           help="One line about what it does."),
    model: str | None = typer.Option(None, "--model",
                                     help="Its model (see: kova model list). Default: the active one."),
    colour: str | None = typer.Option(None, "--colour",
                                      help=f"Its colour: {', '.join(CHAT_COLOURS)}."),
    icon: str | None = typer.Option(None, "--icon", help=f"Its icon: {', '.join(CHAT_ICONS)}."),
    tools: str | None = typer.Option(None, "--tools",
                                     help='Tools it may use, comma-separated; "none" for no tools.'),
    deny: str | None = typer.Option(None, "--deny",
                                    help="Tools it may never use, applied after --tools."),
    permission: str | None = typer.Option(None, "--permission",
                                          help="Its ceiling: manual, auto or full."),
    folder: str | None = typer.Option(None, "--folder",
                                      help="Its working folder. Default: the bot's own folder in Kova Files."),
    persona_file: Path | None = typer.Option(None, "--persona-file", exists=True, dir_okay=False,
                                             resolve_path=True,
                                             help="A Markdown file with its role prompt."),
):
    """Add a bot, either from a starter or from scratch.

    ``kova bots create --from researcher`` adds the starter as it is.
    """
    client = get_client()
    template = None
    if from_template is not None:
        template = bot_templates.get_template(from_template)
        if template is None:
            names = ", ".join(item.handle for item in bot_templates.list_templates())
            _fail(f'No starter bot "{from_template}".', hint=f"Available: {names}")

    label = name or (template.name if template else None)
    wanted = handle or ((_slug(label) if label else "") or (template.handle if template else ""))
    if not wanted:
        _fail("Give the bot a name or a handle.",
              hint="For example: kova bots create Researcher")
    wanted = wanted.strip().casefold()
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,31}", wanted):
        _fail(f'"{wanted}" cannot be a handle.',
              hint="Use lowercase letters, digits and dashes (1-32).")
    if wanted in RESERVED_HANDLES:
        _fail(f'The handle @{wanted} is reserved.',
              hint="Pick another name, or pass --handle with a different one.")
    if handle is not None and any(
            str(bot.get("handle") or bot.get("id") or "").casefold() == wanted
            for bot in client.bots_list()):
        _fail(f'The handle @{wanted} is already used by another bot.',
              hint="Pick another --handle.")
    defined = _unique_handle(client, wanted)
    label = label or defined.replace("-", " ").title()

    payload: dict = {"schema": 1, "id": defined, "name": label, "handle": defined,
                     "description": (description if description is not None
                                     else (template.description if template else ""))}
    if template:
        for table_name in ("avatar", "model", "permissions", "memory", "group"):
            table = template.definition().get(table_name)
            if table:
                payload[table_name] = dict(table)
    if colour is not None or icon is not None:
        avatar = payload.setdefault("avatar", {})
        if colour is not None:
            avatar["colour"] = colour
        if icon is not None:
            avatar["icon"] = icon or None
    if model is not None:
        payload.setdefault("model", {})["name"] = model
    if tools is not None:
        payload.setdefault("permissions", {})["tools"] = _tools_from(tools)
    if deny is not None:
        payload.setdefault("permissions", {})["deny"] = _tools_from(deny)
    if permission is not None:
        payload.setdefault("permissions", {})["ceiling"] = permission
    if folder is not None:
        payload.setdefault("permissions", {})["folder"] = folder

    if persona_file is not None:
        persona = _read_persona(persona_file)
    elif template is not None:
        persona = template.persona
    else:
        summary = payload["description"] or "Help the user with their work."
        persona = (f"You are {label}.\n\n{summary}\n\n"
                   "Be direct and concrete: give the answer first, then the reason. "
                   "Say plainly when you are unsure, and ask one clear question when it "
                   "would change what you do.\n")
    try:
        created = client.bot_create(payload, persona=persona)
    except KovaError as exc:
        _fail(str(exc))
    _ok(f'Added bot "@{_bot_handle(created)}" ({created.get("name")}).')
    _muted(f"Chat with it: kova chat --bot {_bot_handle(created)}")


@_bots.command("edit")
def bots_edit(
    ref: str = typer.Argument(..., help="The bot to change: its handle or id."),
    name: str | None = typer.Option(None, "--name", help="A new name."),
    handle: str | None = typer.Option(None, "--handle", help="A new @handle."),
    description: str | None = typer.Option(None, "--description",
                                           help="One line about what it does."),
    model: str | None = typer.Option(None, "--model", help="Its model (see: kova model list)."),
    colour: str | None = typer.Option(None, "--colour", help=f"Its colour: {', '.join(CHAT_COLOURS)}."),
    icon: str | None = typer.Option(None, "--icon", help=f"Its icon: {', '.join(CHAT_ICONS)}."),
    tools: str | None = typer.Option(None, "--tools", help='Tools it may use, comma-separated; "none" for none.'),
    deny: str | None = typer.Option(None, "--deny", help="Tools it may never use."),
    permission: str | None = typer.Option(None, "--permission",
                                           help="Its ceiling: manual, auto or full."),
    folder: str | None = typer.Option(None, "--folder", help="Its working folder."),
    web: bool | None = typer.Option(None, "--web/--no-web", help="Whether it may search the web."),
    persona_file: Path | None = typer.Option(None, "--persona-file", exists=True, dir_okay=False,
                                             resolve_path=True,
                                             help="A Markdown file with its new role prompt."),
):
    """Change several parts of a bot at once. For one value: ``kova bots set``."""
    client = get_client()
    changes: dict = {}
    if name is not None:
        changes["name"] = name
    if handle is not None:
        changes["handle"] = handle.strip().casefold()
    if description is not None:
        changes["description"] = description
    if model is not None:
        changes.setdefault("model", {})["name"] = model
    if web is not None:
        changes.setdefault("model", {})["web"] = web
    if colour is not None or icon is not None:
        avatar = changes.setdefault("avatar", {})
        if colour is not None:
            avatar["colour"] = colour
        if icon is not None:
            avatar["icon"] = icon or None
    if tools is not None:
        changes.setdefault("permissions", {})["tools"] = _tools_from(tools)
    if deny is not None:
        changes.setdefault("permissions", {})["deny"] = _tools_from(deny)
    if permission is not None:
        changes.setdefault("permissions", {})["ceiling"] = permission
    if folder is not None:
        changes.setdefault("permissions", {})["folder"] = folder
    if not changes and persona_file is None:
        _fail("Nothing to change.",
              hint="Name at least one option, e.g. --model, --tools or --persona-file.")
    try:
        bot_id = _resolve_bot_id(client, ref)
        persona = _read_persona(persona_file) if persona_file is not None else None
        updated = client.bot_update(bot_id, changes, persona=persona)
    except KovaError as exc:
        if exc.status == 404:
            _fail(f'No bot "{ref}".', hint="Run 'kova bots list'.")
        _fail(str(exc))
    _ok(f'Updated bot "@{_bot_handle(updated)}" ({updated.get("name")}).')


@_bots.command("set")
def bots_set(
    ref: str = typer.Argument(..., help="The bot to change: its handle or id."),
    field: str = typer.Argument(..., help="What to change, e.g. model, permission, tools, persona."),
    value: str | None = typer.Argument(None, help="The new value (not needed with --file for persona)."),
    file: Path | None = typer.Option(None, "--file", exists=True, dir_okay=False, resolve_path=True,
                                     help="Read the value from this file, e.g. a persona's Markdown."),
):
    """Change one value on a bot.

    Fields: name, handle, description, colour, icon, model, mode, effort, web,
    permission, tools, deny, folder, inherit-rules, memory, budget, user-memory,
    answers, max-rounds, max-output-tokens, persona. Dotted paths such as
    ``model.name`` or ``permissions.ceiling`` work too.
    """
    client = get_client()
    bot_id = _resolve_bot_id(client, ref)
    key = field.strip().casefold().replace("_", "-")
    if key == "persona":
        if file is not None:
            persona_text = _read_persona(file)
        elif value is not None:
            persona_text = value
        else:
            _fail("Give the persona text, or a file with --file.")
        try:
            updated = client.bot_update(bot_id, {}, persona=persona_text)
        except KovaError as exc:
            _fail(str(exc))
        _ok(f'Updated the role prompt of "@{_bot_handle(updated)}".')
        return
    if value is None:
        _fail(f'Give a value for "{field}".',
              hint=f"For example: kova bots set {bot_id} {field} <value>")
    path, kind = _bot_field(field)
    parsed = _bot_value(kind, value, field)
    try:
        updated = client.bot_update(bot_id, _nested(path, parsed))
    except KovaError as exc:
        if exc.status == 404:
            _fail(f'No bot "{ref}".', hint="Run 'kova bots list'.")
        _fail(str(exc))
    shown = ", ".join(parsed) if isinstance(parsed, list) else (
        "on" if parsed is True else "off" if parsed is False else str(parsed) or "(empty)")
    _ok(f'@{_bot_handle(updated)}: {".".join(path)} = {shown}')


@_bots.command("rm")
def bots_rm(
    ref: str = typer.Argument(..., help="The bot to remove: its handle or id."),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip the confirmation."),
):
    """Remove a bot. Its folder and private notes go; its chats stay."""
    client = get_client()
    bot_id = _resolve_bot_id(client, ref)
    handle = bot_id
    try:
        handle = _bot_handle(client.bot_get(bot_id)) or bot_id
    except KovaError:
        pass
    if not yes and not typer.confirm(
            f'Remove "@{handle}"? Its files and private notes go; its chats stay.',
            default=False):
        _muted("Cancelled.")
        return
    if client.bot_delete(bot_id):
        _ok(f'Removed bot "@{handle}".')
    else:
        _fail(f'No bot "{ref}".')


@_chats.command("list")
def chats_list(
    as_json: bool = typer.Option(False, "--json", help="Print one JSON array for scripts."),
):
    """List chats, newest first."""
    chats = get_client().list_chats()
    if as_json:
        print(json.dumps(chats, ensure_ascii=False))
        return
    if not chats:
        _empty("No chats yet.", "Start one with: kova")
        return
    word = "conversation" if len(chats) == 1 else "conversations"
    table = _table(caption=f"{len(chats)} {word} · newest first")
    table.add_column("#", justify="right", style=colour('ACCENT_SOFT'), no_wrap=True)
    table.add_column("Title", no_wrap=True, max_width=40, overflow="ellipsis")
    table.add_column("Mode", no_wrap=True)
    table.add_column("When", style=colour('MUTED'), no_wrap=True)
    table.add_column("Messages", justify="right", style=colour('MUTED'), no_wrap=True)
    for number, chat in enumerate(chats, start=1):
        title = Text(chat.get("title") or "(untitled)")
        if not chat.get("title"):
            title.stylize(colour('MUTED'))
        mode = Text(chat.get("mode") or "work",
                    style=colour('USER') if (chat.get("mode") or "work") == "chat" else colour('ACCENT_SOFT'))
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
    table.add_column("#", justify="right", style=colour('ACCENT_SOFT'), no_wrap=True)
    table.add_column("Title", no_wrap=True, max_width=32, overflow="ellipsis")
    table.add_column("When", style=colour('MUTED'), no_wrap=True)
    table.add_column("Snippet", overflow="fold")
    for result in results:
        number = numbers.get(result["id"])
        label = str(number) if number is not None else result["id"]
        title = Text(result.get("title") or "(untitled)")
        if not result.get("title"):
            title.stylize(colour('MUTED'))
        snippet = Text(result.get("snippet") or "")
        lowered = snippet.plain.casefold()
        for word in query.split():
            needle = word.casefold()
            start = 0
            while needle and (position := lowered.find(needle, start)) != -1:
                snippet.stylize(f"bold {colour('WARNING')}", position, position + len(needle))
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
    console.print(Text(chat.get("title") or "Untitled chat", style=f"bold {colour('ACCENT')}"))
    console.print(Text(chat["id"], style=colour('MUTED')))
    for message in chat.get("messages", []):
        role, content = message.get("role"), message.get("content")
        if not content:
            continue
        if role == "user":
            console.print()
            console.print(Text("You", style=f"bold {colour('USER')}"))
            console.print(content, markup=False, soft_wrap=True)
        elif role == "assistant":
            console.print()
            console.print(Text(message.get("author_name") or "Kova",
                               style=f"bold {colour('ACCENT')}"))
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


@_chats.command("move")
def chats_move(ref: str, project: str = typer.Argument(
        ..., help='A project ID from `kova projects list`, or "none" to take the chat out.')):
    """Move a chat into a project, or out with "none". Its folder and model stay."""
    client = get_client()
    chat = _resolve_chat(client, ref)
    title = chat.get("title") or "Untitled chat"
    target = None if project.strip().lower() == "none" else project.strip()
    name = _find_project(client, target)["name"] if target else None
    try:
        client.move_chat(chat["id"], target)
    except KovaError as exc:
        _fail(str(exc))
    _ok(f'Moved "{title}" to project "{name}".' if name else f'Took "{title}" out of its project.')


@_projects.command("list")
def projects_list(
    as_json: bool = typer.Option(False, "--json", help="Print one JSON array for scripts."),
):
    """List projects and their IDs."""
    try:
        projects = get_client().list_projects()
    except KovaError as exc:
        _fail(str(exc))
    if as_json:
        print(json.dumps(projects, ensure_ascii=False))
        return
    if not projects:
        _empty("No projects yet.", "Create one with: kova projects create NAME")
        return
    table = _table()
    table.add_column("Name")
    table.add_column("ID", style=colour('MUTED'), no_wrap=True)
    for project in projects:
        table.add_row(Text(project["name"]), project["id"])
    console.print(table)


@_projects.command("create")
def projects_create(
    name: str,
    instructions: str = typer.Option("", "--instructions", help="Shared instructions for project chats."),
    folder: Path | None = typer.Option(None, "--folder", exists=True, file_okay=False, resolve_path=True,
                                       help="Default working folder for new chats in this project."),
    colour: str | None = typer.Option(None, "--colour", help=f"Sidebar colour: {', '.join(CHAT_COLOURS)}."),
    icon: str | None = typer.Option(None, "--icon", help=f"Sidebar icon: {', '.join(CHAT_ICONS)}."),
    model: str | None = typer.Option(None, "--model", help="Default model for new chats (see kova model list)."),
    pin: list[str] = typer.Option([], "--pin", help="A file to pin, relative to the working folder. Repeatable."),
    memory: bool = typer.Option(False, "--memory/--no-memory",
                                help="Give this project its own memory, separate from global memory."),
):
    """Create a project."""
    if pin and folder is None:
        _fail("A project needs --folder before it can pin files.")
    try:
        project = get_client().create_project(
            name, instructions, str(folder) if folder is not None else None,
            colour=colour, icon=icon, model=model, files=pin or None,
            memory_scope=memory)
    except KovaError as exc:
        _fail(str(exc))
    _ok(f'Created project "{project["name"]}" ({project["id"]}).')


@_projects.command("edit")
def projects_edit(
    project_id: str,
    name: str | None = typer.Option(None, "--name", help="A new name."),
    instructions: str | None = typer.Option(None, "--instructions", help="New shared instructions."),
    folder: Path | None = typer.Option(None, "--folder", exists=True, file_okay=False, resolve_path=True,
                                       help="A new default working folder for new chats."),
    colour: str | None = typer.Option(None, "--colour", help='Sidebar colour, or "none" to clear it.'),
    icon: str | None = typer.Option(None, "--icon", help='Sidebar icon, or "none" to clear it.'),
    model: str | None = typer.Option(None, "--model", help='Default model, or "none" to clear it.'),
    pin: list[str] = typer.Option([], "--pin", help="A file to pin, relative to the working folder. Repeatable."),
    unpin: list[str] = typer.Option([], "--unpin", help="A pinned file to remove. Repeatable."),
    clear_pins: bool = typer.Option(False, "--clear-pins", help="Remove every pinned file."),
    memory: bool | None = typer.Option(None, "--memory/--no-memory",
                                       help="Turn this project's own memory scope on or off."),
):
    """Change a project's name, instructions, folder, look, model, pins or memory."""
    changes: dict = {}
    if name is not None:
        changes["name"] = name
    if instructions is not None:
        changes["instructions"] = instructions
    if folder is not None:
        changes["cwd"] = str(folder)
    if memory is not None:
        changes["memory_scope"] = memory
    for key, value in (("colour", colour), ("icon", icon), ("model", model)):
        if value is not None:
            changes[key] = None if value.strip().lower() in ("none", "") else value
    if not changes and not pin and not unpin and not clear_pins:
        _fail("Nothing to change.", hint="Pass at least one option, for example: --name NAME")
    client = get_client()
    project = _find_project(client, project_id)
    if pin or unpin or clear_pins:
        pinned = [] if clear_pins else list(project.get("files") or [])
        gone = {item.strip().replace("\\", "/") for item in unpin}
        pinned = [path for path in pinned if path not in gone]
        pinned.extend(path for path in pin if path not in pinned)
        changes["files"] = pinned
    try:
        project = client.update_project(project_id, **changes)
    except KovaError as exc:
        _fail(str(exc))
    _ok(f'Updated project "{project["name"]}".')


def _find_project(client: KovaClient, project_id: str) -> dict:
    """A project by ID, or a clear exit that names what was asked for."""
    wanted = project_id.strip()
    if not wanted:
        _fail('No project named "".', hint="See the IDs with: kova projects list")
    try:
        project = client.get_project(wanted)
    except KovaError as exc:
        if exc.status == 404:
            _fail(f'No project named "{wanted}".', hint="See the IDs with: kova projects list")
        _fail(str(exc))
    if not isinstance(project, dict):
        # An empty ID can be redirected to the /projects list; never treat that as one.
        _fail(f'No project named "{wanted}".', hint="See the IDs with: kova projects list")
    return project


@_projects.command("show")
def projects_show(project_id: str):
    """Show a project by ID, including its folder, instructions and pinned files."""
    project = _find_project(get_client(), project_id)
    console.print(Text(project["name"], style=f"bold {colour('ACCENT')}"))
    _muted(project["id"])
    console.print(Text(f'Working folder: {project["cwd"] or "(default)"}'))
    console.print(Text(f'Default model: {project.get("model") or "(the active model)"}'))
    console.print(Text("Project memory: " + ("on (isolated)" if project.get("memory_scope")
                                             else "off (global memory)")))
    look = " · ".join(part for part in (project.get("colour"), project.get("icon")) if part)
    if look:
        console.print(Text(f"Look: {look}"))
    if project.get("files"):
        console.print(Text("Pinned files (references only):"))
        for path in project["files"]:
            console.print(Text(f"  {path}"))
    if project["instructions"]:
        console.print()
        console.print(Text(project["instructions"]))


@_projects.command("rm")
def projects_rm(project_id: str, yes: bool = typer.Option(False, "--yes", "-y", help="Skip the confirmation.")):
    """Remove a project by ID. Its chats are kept and ungrouped."""
    client = get_client()
    project = _find_project(client, project_id)
    name = project["name"]
    if not yes and not typer.confirm(f'Remove project "{name}"? Its chats will be kept, but its project memory will be deleted.', default=False):
        _muted("Cancelled.")
        return
    try:
        removed = client.delete_project(project_id)
    except KovaError as exc:
        _fail(str(exc))
    if not removed:
        _fail(f'No project named "{project_id}".', hint="See the IDs with: kova projects list")
    _ok(f'Removed project "{name}".')


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
        _ConfigField("tui.theme", "tui_theme", "theme"),
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
    if field.kind == "theme":
        try:
            return _terminal_theme_setting(text, cfg.home)
        except themes.ThemeError as exc:
            raise _ConfigValueError(f'Invalid value for {key}: {exc}')
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
    table.add_column("Key", style=colour('ACCENT_SOFT'), no_wrap=True)
    table.add_column("Value", overflow="fold")
    for key, value in _config_rows(snapshot):
        table.add_row(key, Text(value))
    console.print(table)


@_config.command("set", context_settings={"ignore_unknown_options": True})
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


_providers = typer.Typer(help="Connect providers and manage their sign-ins.", invoke_without_command=True)
app.add_typer(_providers, name="providers")


@_providers.callback()
def providers_root(ctx: typer.Context):
    if ctx.invoked_subcommand is None:
        providers_list()


@_providers.command('list')
def providers_list():
    """List connected providers with their address, access and capabilities.

    One short block per provider, so a long address stays readable in a
    narrow terminal instead of being cut off.
    """
    rows = get_client().providers_list()
    if not rows:
        _empty('No providers connected.', 'Run: kova providers add to connect one.')
        return
    for index, p in enumerate(rows):
        if index:
            console.print()
        connected = bool(p['authenticated'])
        account = str(p.get('account') or '')
        console.print(Text.assemble(
            ("● " if connected else "◇ ", colour('SUCCESS') if connected else colour('WARNING')),
            (p['name'], f"bold {colour('ACCENT')}"),
            (f"  {p['id']}" if p['id'] != p['name'] else "", colour('MUTED'))))
        console.print(Text(f"  {p['base_url']}", style=colour('MUTED')))
        access = f"Connected as {account}" if connected and account else \
            'Connected' if connected else 'No credentials'
        capabilities = ', '.join(p.get('capabilities') or []) or '-'
        console.print(Text(f"  {access} · {capabilities}", style=colour('FAINT')))
    console.print()
    _muted("Switch models with: kova model use  ·  connect another with: kova providers add")


@_providers.command('add')
def provider_add(
    kind: str | None = typer.Argument(
        None, help='Provider type to skip the menu: openai, anthropic, opencode, local, custom, this-pc, and so on.'),
):
    """Connect a provider: pick one, paste its key, choose models.

    With no argument this walks through the catalog (OpenAI, Anthropic,
    OpenRouter, OpenCode, a local Ollama/LM Studio/llama.cpp server, or any
    OpenAI-compatible address), asks for the key only when the provider needs
    one, and makes the chosen model active. Keys are read with a hidden prompt
    and never printed.
    """
    try:
        onboarding.run_setup(load_config(), provider_kind=kind, persona=False)
    except onboarding.SetupError as exc:
        _fail(str(exc))
    _muted('Switch provider and model any time with: kova model use')


@_providers.command('remove')
def provider_remove(
    name: str = typer.Argument(..., help='The provider id from kova providers list.'),
):
    """Remove a provider connection and the models selected from it."""
    get_client().provider_remove(name)
    _ok(f'Removed provider {name} and its selected models.')


@_providers.command('logout')
def provider_logout(
    name: str = typer.Argument(..., help='The provider id from kova providers list.'),
):
    """Sign out of a provider and forget its stored credential."""
    result = get_client().provider_logout(name)
    _ok(f'Signed out of {name}.')
    if not result.get('remote_revocation_confirmed', True):
        _muted('Remote revocation was not confirmed. Disconnect Kova in ChatGPT Settings to revoke access.')


@_providers.command('login')
def provider_login(
    name: str = typer.Argument(..., help='The provider id from kova providers list.'),
):
    """Sign in to or re-authenticate a connected provider."""
    from kova import provider_auth
    from kova.providers import ProviderError
    cfg = load_config()
    p = next((p for p in cfg.providers if p.id == name), None)
    if p is None:
        _fail(f'No provider named "{name}".', hint='Run: kova providers list')
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
        console.print(Text(PHONE_EXPLAINER, style=colour('MUTED')))
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
    console.print(Text(f"  Or open {code['url']}", style=colour('MUTED')), no_wrap=True, crop=False, overflow="ignore")
    console.print(Text(f"  The code works once and expires in {code['expires_in'] // 60} minutes. Ctrl+C cancels.",
                       style=colour('MUTED')))
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
        devices_list(as_json=False)


@_devices.command("list")
def devices_list(
    as_json: bool = typer.Option(False, "--json", help="Print one JSON array for scripts."),
):
    """List paired devices."""
    status = get_client().phone_status()
    devices = status["devices"]
    if as_json:
        print(json.dumps(devices, ensure_ascii=False))
        return
    state = "on" if status["listening"] else ("on, not listening" if status["enabled"] else "off")
    if not devices:
        _empty(f"No paired devices. Phone access is {state}.", "Pair a phone with: kova pair")
        return
    table = _table(caption=f"{len(devices)} paired · phone access {state}"
                   + (f" at {status['address']}" if status.get("address") else ""))
    table.add_column("ID", style=colour('ACCENT_SOFT'), no_wrap=True)
    table.add_column("Device")
    table.add_column("Paired", style=colour('MUTED'), no_wrap=True)
    table.add_column("Last seen", style=colour('MUTED'), no_wrap=True)
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
                (f"{SYMBOL_ARROW} ", colour('ACCENT_SOFT')),
                (f"Next: {next_step}", colour('MUTED')),
                (f"    ({len(checks) - len(failed)} ok, {len(failed)} "
                 f"need{'s' if len(failed) == 1 else ''} attention)", colour('MUTED')),
            ))
        else:
            console.print(Text.assemble(
                (f"{SYMBOL_ARROW} ", colour('ACCENT_SOFT')),
                ("All checks passed.", colour('SUCCESS')),
                (f"    ({len(checks)} ok)", colour('MUTED')),
            ))
    if any(not check.ok for check in checks):
        raise typer.Exit(1)
