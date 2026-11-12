"""Friendly first-run setup: find a model server, test it, and save it.

``kova setup`` walks a new user through picking a local (or homelab) model
server and a model, checks that the pair actually answers, and writes the
same config ``kova model add`` would. Nothing here is required to use Kova:
``kova`` asks to run this once when no model answers.
"""

from __future__ import annotations

import asyncio
import re
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

import httpx
from rich import box
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from kova import server_finder, theme
from kova.client import KovaClient, KovaError
from kova.config import Config, ModelEntry, ensure_token, save_config
from kova.models import ModelClient, ModelError
from kova.server_finder import normalize_base  # re-exported: the base URL every probe builds on

console = Console()

SYMBOL_OK = "✓"
SYMBOL_ERROR = "✗"

PROBE_TIMEOUT = 0.8   # seconds for one GET /v1/models
CHAT_TIMEOUT = 20.0   # seconds for the one test completion
SERVER_TIMEOUT = 2.0  # seconds to notice a running kova server


@dataclass(frozen=True)
class KnownServer:
    """A model server many people already have running locally."""

    label: str
    url: str
    name: str


KNOWN_SERVERS: tuple[KnownServer, ...] = (
    KnownServer("Ollama", "http://127.0.0.1:11434", "ollama"),
    KnownServer("LM Studio", "http://127.0.0.1:1234", "lmstudio"),
    KnownServer("llama.cpp / PolyStrata", "http://127.0.0.1:8080", "llamacpp"),
    KnownServer("vLLM", "http://127.0.0.1:8000", "vllm"),
    KnownServer("Jan", "http://127.0.0.1:1337", "jan"),
)


@dataclass
class DetectedServer:
    """A reachable OpenAI-compatible server and the models it listed."""

    label: str
    base_url: str  # ends in /v1, ready for ModelEntry
    models: list[str] = field(default_factory=list)
    needs_key: bool = False


@dataclass
class ChatTest:
    """The outcome of the one-message smoke test."""

    ok: bool
    seconds: float = 0.0
    reply: str = ""
    error: str = ""


class SetupError(RuntimeError):
    """Setup could not finish; the CLI shows the message as one styled error line."""


def _ok(message: str) -> None:
    console.print(Text.assemble((f"{SYMBOL_OK} ", f"bold {theme.SUCCESS}"), (message, "")))


def _error(message: str) -> None:
    console.print(Text.assemble((f"{SYMBOL_ERROR} ", f"bold {theme.ERROR}"), (message, theme.ERROR)))


def _step(number: int, total: int, title: str) -> None:
    """One clear wizard section header, e.g. ``Step 1 of 3 · Model server``."""
    console.print()
    console.print(Text.assemble(
        (f"Step {number} of {total}", f"bold {theme.ACCENT}"),
        (" · ", theme.MUTED),
        (title, "bold"),
    ))
    console.print(Text("─" * 60, style=theme.BORDER))


@contextmanager
def _working(message: str):
    """A spinner while probing; a plain dim line when output is piped."""
    if console.is_terminal:
        with console.status(Text(message, style=theme.MUTED), spinner="dots"):
            yield
    else:
        console.print(Text(message, style=theme.MUTED))
        yield


def _pretty_path(path: Path) -> str:
    """Show paths under the home folder with a friendly ``~``."""
    try:
        return "~/" + str(Path(path).relative_to(Path.home()))
    except ValueError:
        return str(path)


# ---------------------------------------------------------------------------
# Probing
# ---------------------------------------------------------------------------

def _label_for(base_url: str) -> str:
    for known in KNOWN_SERVERS:
        if normalize_base(known.url) == base_url:
            return known.label
    return httpx.URL(base_url).host or base_url


def _default_name(base_url: str) -> str:
    """A short, shell-friendly name to save a server under."""
    for known in KNOWN_SERVERS:
        if normalize_base(known.url) == base_url:
            return known.name
    parsed = httpx.URL(base_url)
    host = parsed.host or "server"
    if host in ("127.0.0.1", "localhost", "::1"):
        host = "local"
    slug = re.sub(r"[^a-z0-9]+", "-", host.lower()).strip("-") or "server"
    if parsed.port and parsed.port not in (80, 443):
        slug += f"-{parsed.port}"
    return slug


def probe_url(
    url: str,
    *,
    label: str | None = None,
    api_key: str | None = None,
    timeout: float = PROBE_TIMEOUT,
    transport: httpx.BaseTransport | None = None,
) -> DetectedServer | None:
    """GET ``<url>/v1/models`` once; return None if nothing answers.

    A 401 still counts as found: the server is there, it just wants a key.
    """
    base = normalize_base(url)
    if not base:
        return None
    found = server_finder.probe(base, api_key=api_key, timeout=timeout, transport=transport)
    if found.error:
        return None
    try:
        name = label or _label_for(base)
    except httpx.InvalidURL:
        name = base
    return DetectedServer(name, base, found.models, needs_key=found.needs_key)


def detect_servers(
    servers: tuple[KnownServer, ...] | list[KnownServer] | None = None,
    *,
    timeout: float = PROBE_TIMEOUT,
    transport: httpx.BaseTransport | None = None,
) -> list[DetectedServer]:
    """Probe every known local server in parallel; keep the ones that answer."""
    candidates = tuple(KNOWN_SERVERS if servers is None else servers)
    if not candidates:
        return []

    def probe(known: KnownServer) -> DetectedServer | None:
        return probe_url(known.url, label=known.label, timeout=timeout, transport=transport)

    with ThreadPoolExecutor(max_workers=len(candidates)) as pool:
        results = list(pool.map(probe, candidates))
    return [server for server in results if server is not None]


def _extract_reply(chunks: list[str]) -> str:
    return "".join(chunks).strip()


def _chat_error(message: str, entry: ModelEntry) -> str:
    """Turn a ModelError into something a first-time user can act on."""
    if "HTTP 401" in message or "HTTP 403" in message:
        return "The server refused the API key. Check the key and try again."
    if "HTTP 404" in message:
        return (
            f"The server answered, but {entry.base_url}/chat/completions wasn't found. "
            "Check the URL: OpenAI-compatible servers usually end in /v1."
        )
    if "HTTP 5" in message:
        return f"The server had a problem answering ({message}). Try again in a moment."
    if "Can't reach" in message:
        return f"Couldn't connect to {entry.base_url}. Is the server running?"
    return message


def test_chat(
    entry: ModelEntry,
    *,
    timeout: float = CHAT_TIMEOUT,
    transport: httpx.AsyncBaseTransport | None = None,
) -> ChatTest:
    """Send one tiny completion and time it; never raises for model errors."""
    async def run() -> str:
        chunks: list[str] = []
        client = ModelClient(entry, transport=transport, timeout=timeout)
        async for event in client.stream([{"role": "user", "content": "Reply with just the word: pong"}]):
            if event.type == "text":
                chunks.append(event.text)
        return _extract_reply(chunks)

    started = time.perf_counter()
    try:
        reply = asyncio.run(run())
    except ModelError as exc:
        return ChatTest(False, time.perf_counter() - started, error=_chat_error(str(exc), entry))
    except Exception as exc:  # a broken server must become a friendly message, not a traceback
        return ChatTest(False, time.perf_counter() - started, error=f"Unexpected error: {exc}")
    return ChatTest(True, time.perf_counter() - started, reply=reply)


def needs_setup(
    cfg: Config,
    *,
    timeout: float = PROBE_TIMEOUT,
    transport: httpx.BaseTransport | None = None,
) -> bool:
    """True when Kova has no usable model: none configured or none reachable."""
    if not cfg.models:
        return True
    try:
        entry = cfg.active_model()
    except KeyError:
        return True
    if not entry.base_url or not entry.model:
        return True
    server = probe_url(entry.base_url, api_key=entry.api_key, timeout=timeout, transport=transport)
    return server is None or server.needs_key


# ---------------------------------------------------------------------------
# Small prompts (plain input(), so tests and pipes are easy)
# ---------------------------------------------------------------------------

def _ask(prompt: str, default: str | None = None) -> str:
    console.print(prompt, end=" ")
    try:
        answer = input()
    except EOFError:
        if default is None:
            raise SetupError("Input ended. Run kova setup again any time.") from None
        return default
    answer = answer.strip()
    return answer or (default or "")


def _confirm(prompt: str, default: bool = True) -> bool:
    suffix = "[Y/n]" if default else "[y/N]"
    answer = _ask(f"{prompt} {suffix}", default="y" if default else "n")
    return answer.strip().lower().startswith("y")


def _choose(prompt: str, count: int, default: int = 1) -> int:
    while True:
        answer = _ask(prompt, default=str(default))
        if not answer:
            return default
        try:
            choice = int(answer)
        except ValueError:
            _error("Type a number, please.")
            continue
        if 1 <= choice <= count:
            return choice
        _error(f"Pick a number from 1 to {count}.")


def _ask_required(prompt: str) -> str:
    while True:
        answer = _ask(prompt)
        if answer:
            return answer
        _error("That can't be empty.")


# ---------------------------------------------------------------------------
# Wizard pieces
# ---------------------------------------------------------------------------

def _print_found(found: list[DetectedServer]) -> None:
    table = Table(
        title=Text("Model servers found", style=f"bold {theme.ACCENT}"),
        title_justify="left",
        caption=Text(f"{len(found)} reachable · pick one below", style=theme.MUTED),
        caption_justify="left",
        box=box.SIMPLE_HEAD,
        border_style=theme.BORDER,
        header_style=f"bold {theme.MUTED}",
        pad_edge=False,
        show_edge=False,
        expand=False,
    )
    table.add_column("#", justify="right", style=theme.ACCENT_SOFT, no_wrap=True)
    table.add_column("Server", style="bold", no_wrap=True)
    table.add_column("URL", style=theme.MUTED, overflow="fold")
    table.add_column("Models", justify="right", no_wrap=True)
    for index, server in enumerate(found, 1):
        count = Text("needs key", style=theme.WARNING) if server.needs_key else Text(str(len(server.models)))
        table.add_row(str(index), server.label, server.base_url, count)
    console.print(table)


def _found_to_detected(found: server_finder.Found) -> DetectedServer:
    label = found.label or found.kind or _label_for(found.base_url)
    return DetectedServer(label, found.base_url, list(found.models), needs_key=found.needs_key)


def _scan_network(transport: httpx.BaseTransport | None) -> DetectedServer | None:
    """Look for model servers on this computer and the local network, then let
    the user pick one. Returns None when nothing was found."""
    with _working("Scanning this computer and your network (takes a few seconds)…"):
        found = server_finder.discover(transport=transport)
    if not found:
        console.print(Text("No model servers found on your network.", style=theme.WARNING))
        return None
    servers = [_found_to_detected(item) for item in found]
    _print_found(servers)
    index = _choose(f"Which server? [bold]1-{len(servers)}[/bold], Enter = 1:", len(servers))
    return servers[index - 1]


def _ask_for_server(
    *,
    timeout: float,
    transport: httpx.BaseTransport | None,
) -> DetectedServer:
    """Prompt for an address until one answers; "scan" searches the network."""
    console.print(Text("Type its address the way you think of it: 192.168.1.50, homelab:8080,", style=theme.MUTED))
    console.print(Text("http://10.0.0.5:8080/v1 or openrouter. Or type scan to search your network.",
                       style=theme.MUTED))
    while True:
        raw = _ask("Server address, scan, or blank to cancel:")
        if not raw:
            raise SetupError("Setup cancelled. Run kova setup again any time.")
        if raw.strip().lower() == "scan":
            server = _scan_network(transport)
            if server is not None:
                return server
            continue
        try:
            with _working(f"Looking for a model server at {raw}…"):
                found = server_finder.connect(raw, timeout=timeout, transport=transport)
        except ValueError as exc:
            _error(str(exc))
            continue
        if found.answered:
            return _found_to_detected(found)
        _error(found.error)


def _ask_for_key(
    server: DetectedServer,
    *,
    timeout: float,
    transport: httpx.BaseTransport | None,
) -> tuple[DetectedServer, str]:
    """Ask for the key a 401 server wants, then look at its models again."""
    console.print(Text(f"{server.label} at {server.base_url} wants an API key.", style=theme.WARNING))
    key = _ask_required("Paste the API key:")
    with _working("Checking the key…"):
        again = probe_url(server.base_url, label=server.label, api_key=key, timeout=timeout, transport=transport)
    if again is not None and not again.needs_key:
        _ok("Key accepted.")
        return again, key
    _error("The server still said no. You can save it now and fix the key later.")
    return DetectedServer(server.label, server.base_url, [], needs_key=True), key


def _pick_model(server: DetectedServer) -> str:
    if server.models:
        console.print(Text(f"Models on {server.label}", style=f"bold {theme.ACCENT}"))
        for index, name in enumerate(server.models, 1):
            console.print(Text.assemble((f"  {index}.", theme.ACCENT_SOFT), (f" {name}", "")))
        choice = _choose(f"Which model? [bold]1-{len(server.models)}[/bold], Enter = 1:", len(server.models))
        return server.models[choice - 1]
    console.print(Text("That server didn't list any models.", style=theme.WARNING))
    return _ask_required("Model name (for example qwen3:8b):")


def _ask_memory(cfg: Config) -> tuple[Path, str | None] | None:
    console.print(Text("Where should Kova keep its memory?", style="bold"))
    console.print(Text.assemble(("  1.", theme.ACCENT_SOFT), (f" Default ({_pretty_path(cfg.memory_path)})", "")))
    console.print(Text.assemble(("  2.", theme.ACCENT_SOFT),
                                (" An Obsidian vault (Kova only writes inside <vault>/Kova/)", "")))
    answer = _ask("Pick 1 or 2, Enter = 1:", default="1")
    if not answer.strip().startswith("2"):
        return None
    while True:
        raw = _ask("Path to your vault:")
        if raw:
            break
        _error("Type the vault path, or press Ctrl+C to cancel.")
    path = Path(raw).expanduser().resolve()
    if not path.is_dir():
        console.print(Text(f"{_pretty_path(path)} doesn't exist yet; Kova will create it.", style=theme.MUTED))
    return path, "Kova"


# ---------------------------------------------------------------------------
# Saving
# ---------------------------------------------------------------------------

def _running_client(cfg: Config) -> KovaClient | None:
    """A client for a live kova server, or None when none is running."""
    base = f"http://127.0.0.1:{cfg.port}"
    http = httpx.Client(base_url=base, timeout=SERVER_TIMEOUT, trust_env=False)
    client = KovaClient(base, ensure_token(cfg.home), http=http)
    try:
        client.health()
    except KovaError:
        http.close()
        return None
    return client


def _save(
    cfg: Config,
    entry: ModelEntry,
    memory: tuple[Path, str | None] | None,
) -> bool:
    """Save through a running server when possible, else straight to the file.

    Returns True when a live server picked the change up.
    """
    client = _running_client(cfg)
    if client is not None:
        try:
            client.model_add(entry.name, entry.base_url, entry.model, entry.api_key)
            client.model_use(entry.name)
            if memory is not None:
                client.memory_use(str(memory[0]), memory[1] is not None)
            return True
        except KovaError:
            pass  # fall through to the file, which the server reads on restart
        finally:
            client.http.close()
    cfg.models[:] = [m for m in cfg.models if m.name != entry.name]
    cfg.models.append(entry)
    cfg.active = entry.name
    if memory is not None:
        cfg.memory_path = memory[0]
        cfg.memory_write_subdir = memory[1]
    save_config(cfg)
    return False


def _finish(cfg: Config, entry: ModelEntry, memory: tuple[Path, str | None] | None) -> None:
    live = _save(cfg, entry, memory)
    if live:
        _ok(f"Saved. The running Kova server is using {entry.name} now.")
    else:
        _ok(f"Saved {entry.name} to {_pretty_path(cfg.home / 'config.toml')}.")

    memory_path = memory[0] if memory is not None else cfg.memory_path
    rows = Table.grid(padding=(0, 2))
    rows.add_column(no_wrap=True)
    rows.add_column(no_wrap=True)
    rows.add_row(Text("Model", style=theme.MUTED), Text(f"{entry.name} · {entry.model}"))
    rows.add_row(Text("Server", style=theme.MUTED), Text(entry.base_url))
    rows.add_row(Text("Memory", style=theme.MUTED),
                 Text(_pretty_path(memory_path) + (" (Obsidian)" if memory and memory[1] else "")))
    rows.add_row(Text("Config", style=theme.MUTED), Text(_pretty_path(cfg.home / "config.toml")))
    rows.add_row(Text("Run", style=theme.MUTED), Text("kova", style=f"bold {theme.ACCENT}"))
    console.print()
    console.print(Panel(
        rows,
        title=Text.assemble((f"{SYMBOL_OK} ", f"bold {theme.SUCCESS}"), ("You're set.", f"bold {theme.SUCCESS}")),
        title_align="left",
        subtitle=Text("Chat any time; /help lists the commands.", style=theme.MUTED),
        subtitle_align="left",
        border_style=theme.SUCCESS,
        box=box.ROUNDED,
        padding=(0, 1),
        expand=False,
    ))


# ---------------------------------------------------------------------------
# Entry points
# ---------------------------------------------------------------------------

def _run_quick(
    cfg: Config,
    *,
    url: str,
    model: str | None,
    name: str | None,
    api_key: str | None,
    yes: bool,
    transport: httpx.BaseTransport | None,
    timeout: float,
    chat_timeout: float,
) -> None:
    """The flag-driven path: ``kova setup --url ... --model ...``."""
    with _working(f"Checking {url}…"):
        server = probe_url(url, api_key=api_key, timeout=timeout, transport=transport)
    if server is None:
        raise SetupError(f"Couldn't reach a model server at {url}. Check the URL and that the server is running.")
    if server.needs_key:
        raise SetupError("That server needs an API key. Add --key KEY.")
    chosen = model or (server.models[0] if server.models else None)
    if not chosen:
        raise SetupError("The server didn't list any models. Pass --model NAME.")

    entry = ModelEntry(
        name=name or _default_name(server.base_url),
        base_url=server.base_url,
        model=chosen,
        api_key=api_key,
    )
    with _working(f"Sending one quick test message to {chosen}…"):
        result = test_chat(entry, timeout=chat_timeout, transport=transport)
    if result.ok:
        _ok(f"Works! Answered in {result.seconds:.2f}s.")
    elif yes:
        _error(f"The test message failed. {result.error}")
        console.print(Text("  Saving it anyway because --yes was given.", style=theme.MUTED))
    else:
        raise SetupError(f"The test message failed: {result.error} Re-run with --yes to save it anyway.")
    _finish(cfg, entry, None)


def _run_wizard(
    cfg: Config,
    *,
    yes: bool,
    transport: httpx.BaseTransport | None,
    timeout: float,
    chat_timeout: float,
) -> None:
    console.print()
    console.print(Text("Welcome to Kova.", style=f"bold {theme.ACCENT}"))
    console.print(Text("Let's connect a model server so you can chat.", style=theme.MUTED))

    _step(1, 3, "Model server")
    with _working("Looking for model servers on this computer…"):
        found = detect_servers(timeout=timeout, transport=transport)
    if found:
        _print_found(found)
        index = _choose(f"Which server? [bold]1-{len(found)}[/bold], Enter = 1:", len(found))
        server = found[index - 1]
    else:
        console.print(Text("No model server found on this computer.", style=theme.WARNING))
        console.print(Text("That's fine if your server runs on another machine.", style=theme.MUTED))
        server = _ask_for_server(timeout=timeout, transport=transport)

    api_key: str | None = None
    if server.needs_key:
        server, api_key = _ask_for_key(server, timeout=timeout, transport=transport)

    _step(2, 3, "Model")
    model_name = _pick_model(server)
    default_name = _default_name(server.base_url)
    console.print()
    saved_name = _ask(
        f"Save it as [bold]{default_name}[/bold]? Enter to accept, or type another name:",
        default=default_name,
    ).strip()
    entry = ModelEntry(
        name=saved_name or default_name,
        base_url=server.base_url,
        model=model_name,
        api_key=api_key,
    )

    console.print()
    with _working(f"Sending one quick test message to {model_name}…"):
        result = test_chat(entry, timeout=chat_timeout, transport=transport)
    if result.ok:
        _ok(f"It works! The model answered in {result.seconds:.2f}s.")
        if result.reply:
            console.print(Text(f"  It said: {_clip_reply(result.reply)}", style=theme.MUTED))
    else:
        _error(f"The test message failed. {result.error}")
        if not (yes or _confirm("Save it anyway?", default=False)):
            raise SetupError("Nothing saved. Run kova setup again when the server is ready.")

    _step(3, 3, "Memory")
    memory = _ask_memory(cfg)
    _finish(cfg, entry, memory)


def _clip_reply(reply: str, limit: int = 80) -> str:
    """Keep the model's test reply to one tidy line."""
    text = " ".join(reply.split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def run_setup(
    cfg: Config,
    *,
    url: str | None = None,
    model: str | None = None,
    name: str | None = None,
    api_key: str | None = None,
    yes: bool = False,
    transport: httpx.BaseTransport | None = None,
    timeout: float = PROBE_TIMEOUT,
    chat_timeout: float = CHAT_TIMEOUT,
) -> None:
    """Find (or accept) a model server, test it, and make it Kova's active model.

    With ``url`` this runs without prompts. Without it, the interactive wizard
    asks for a server, a model, and a memory folder.
    """
    if url:
        _run_quick(
            cfg,
            url=url,
            model=model,
            name=name,
            api_key=api_key,
            yes=yes,
            transport=transport,
            timeout=timeout,
            chat_timeout=chat_timeout,
        )
        return
    if model or name or api_key:
        raise SetupError("Pass --url along with --model, --name, or --key.")
    _run_wizard(cfg, yes=yes, transport=transport, timeout=timeout, chat_timeout=chat_timeout)
