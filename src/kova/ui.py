"""Interactive terminal chat; the script-friendly ``ask`` command stays separate."""

from __future__ import annotations

import sys
import threading
from contextlib import nullcontext
from dataclasses import dataclass, field
from time import perf_counter

from prompt_toolkit import PromptSession
from prompt_toolkit.completion import WordCompleter
from prompt_toolkit.formatted_text import HTML
from prompt_toolkit.history import InMemoryHistory
from prompt_toolkit.patch_stdout import patch_stdout
from rich.console import Console, Group
from rich.live import Live
from rich.markdown import Markdown
from rich.panel import Panel
from rich.spinner import Spinner
from rich.table import Table
from rich.text import Text

from kova.client import KovaClient, KovaError
from kova.config import load_config

_COMMANDS = {
    "/help": "Show commands and keyboard shortcuts",
    "/new": "Start a fresh conversation",
    "/model": "List models; /model <name> switches",
    "/think": "Toggle full reasoning (collapsed by default)",
    "/clear": "Clear the screen, keeping this conversation",
    "/exit": "Leave Kova (also /quit)",
    "/quit": "Leave Kova",
}


def _short(text: str, limit: int = 100) -> str:
    """Keep server-supplied status text on one line, without Rich markup."""
    text = " ".join(text.split())
    return text if len(text) <= limit else text[:limit - 1] + "…"


@dataclass
class _Block:
    kind: str
    chunks: list[str] = field(default_factory=list)
    name: str = ""
    ok: bool | None = None
    started: float = field(default_factory=lambda: perf_counter())
    elapsed: float | None = None


@dataclass
class _Turn:
    no_think: bool
    show_thinking: bool
    blocks: list[_Block] = field(default_factory=list)
    finished: bool = False

    def end_reasoning(self) -> None:
        if self.blocks and self.blocks[-1].kind == "reasoning":
            block = self.blocks[-1]
            if block.elapsed is None:
                block.elapsed = perf_counter() - block.started

    def accept(self, event: dict) -> None:
        kind = event["type"]
        if kind != "reasoning":
            self.end_reasoning()
        if kind in ("text", "reasoning"):
            if not self.blocks or self.blocks[-1].kind != kind:
                self.blocks.append(_Block(kind))
            self.blocks[-1].chunks.append(event["text"])
        elif kind == "tool_call":
            self.blocks.append(_Block("tool", name=event["name"]))
        elif kind == "tool_result":
            block = next((block for block in reversed(self.blocks)
                          if block.kind == "tool" and block.name == event["name"] and block.ok is None), None)
            if block is None:
                block = _Block("tool", name=event["name"])
                self.blocks.append(block)
            block.ok = event["ok"]
            block.chunks = [_short(event["summary"])]
        elif kind == "error":
            self.blocks.append(_Block("error", [_short(event["message"])]))

    def render(self) -> Group:
        items = [Text("● Kova", style="bold cyan")]
        for block in self.blocks:
            text = "".join(block.chunks)
            if block.kind == "text":
                items.append(Markdown(text, code_theme="monokai"))
            elif block.kind == "reasoning":
                if block.elapsed is None and not self.finished:
                    items.append(Spinner("dots", text="Thinking…", style="cyan"))
                elif not self.no_think:
                    items.append(Text(f"  thought for {block.elapsed or 0:.1f}s", style="dim"))
                if self.show_thinking and not self.no_think:
                    items.append(Text(text, style="dim"))
            elif block.kind == "tool":
                if block.ok is None and not self.finished:
                    items.append(Spinner("dots", text=Text(block.name, style="cyan"), style="cyan"))
                else:
                    icon = "✓" if block.ok else "✗" if block.ok is False else "·"
                    detail = text if block.ok is not None else "interrupted"
                    items.append(Text(f"  {icon} {block.name} · {detail}",
                                      style="dim" if block.ok else "red" if block.ok is False else "dim"))
            elif block.kind == "error":
                items.append(Text(f"  Error: {text}", style="red"))
        # Keep activity visible before the first token and between tool calls.
        if not self.finished and (not self.blocks or
                                  (self.blocks[-1].kind == "tool" and self.blocks[-1].ok is not None)):
            items.append(Spinner("dots", text="Thinking…", style="cyan"))
        return Group(*items)


def _render_turn(client: KovaClient, chat_id: str, text: str, no_think: bool,
                 console: Console, *, show_thinking: bool = False) -> None:
    """Repaint at most ten times a second, then commit the entire reply to scrollback."""
    turn = _Turn(no_think, show_thinking)
    events = client.stream_message(chat_id, text)
    interrupted = False
    # Pipes get a single final rendering, never cursor controls or animation frames.
    live = (Live(console=console, get_renderable=lambda: Text("") if turn.finished else turn.render(),
                 refresh_per_second=10,
                 transient=True, vertical_overflow="ellipsis")
            if console.is_terminal else nullcontext())
    try:
        with live:
            try:
                for event in events:
                    turn.accept(event)
                    if event["type"] == "done":
                        break
            except KeyboardInterrupt:
                interrupted = True
            finally:
                turn.end_reasoning()
                turn.finished = True
    finally:
        events.close()
        # Printing after Live clears its transient viewport preserves even replies
        # taller than the terminal, rather than leaving only the cropped live view.
        console.print(turn.render())
        if interrupted:
            console.print("  Reply stopped · send another message when ready.", style="dim")
        console.print()


def _header(client: KovaClient, console: Console) -> None:
    health = client.health()
    cfg = load_config()
    notes = len(client.memory_list())
    status = Text("● server online", style="green")
    status.append("  ·  model ", style="dim")
    status.append(str(health["model"]), style="bold")
    if not health["model_ok"]:
        status.append(" (unreachable)", style="red")
    memory = Text(f"Memory  {cfg.memory_path}  ·  {notes} {'note' if notes == 1 else 'notes'}", style="dim")
    hint = Text("/help commands   /think reasoning   Ctrl+D leave", style="dim")
    console.print(Panel(Group(status, memory, hint), title=Text("Kova", style="bold cyan"),
                        title_align="left", border_style="bright_black", padding=(0, 1)))
    console.print()


def _help(console: Console) -> None:
    table = Table(title="Chat commands", box=None, show_header=False, padding=(0, 2, 0, 0))
    table.add_column(style="cyan", no_wrap=True)
    table.add_column()
    for command, description in _COMMANDS.items():
        table.add_row(command, description)
    console.print(table)
    console.print("↑ history · Tab complete · Ctrl+C stop reply / clear input · Ctrl+D leave", style="dim")
    console.print()


def _models(client: KovaClient, console: Console) -> None:
    active = client.health()["model"]
    table = Table(box=None, padding=(0, 2, 0, 0))
    table.add_column("Model", style="cyan")
    table.add_column("Identifier")
    table.add_column("Server", style="dim")
    for entry in client.models_list():
        name = Text(entry["name"])
        if entry["name"] == active:
            name.append(" (active)", style="green")
        table.add_row(name, Text(entry["model"]), Text(entry["base_url"]))
    console.print(table)
    console.print()


def _watch_reminders(client: KovaClient, console: Console) -> None:
    try:
        for event in client.events():
            if event.get("type") == "reminder":
                console.print(Text(f"⏰ {_short(event['text'], 200)}", style="green"))
    except Exception:  # A dead event connection must never crash chat.
        pass


def _run_chat(client: KovaClient, chat_id: str | None = None, no_think: bool = False,
              *, new: bool = False, console: Console) -> None:
    if chat_id is None:
        chats = client.list_chats() if not new else []
        chat_id = chats[0]["id"] if chats else client.create_chat()["id"]
    _header(client, console)
    interactive = sys.stdin.isatty() and console.is_terminal
    session = None
    if interactive:
        session = PromptSession(history=InMemoryHistory(),
                                completer=WordCompleter(list(_COMMANDS), sentence=True),
                                complete_while_typing=False)
        threading.Thread(target=_watch_reminders, args=(client, console), daemon=True).start()
    show_thinking = False
    while True:
        try:
            if session is None:
                line = console.input("[bold cyan]› [/bold cyan]")
            else:
                with patch_stdout(raw=True):
                    line = session.prompt(HTML('<ansicyan><b>› </b></ansicyan>'))
        except EOFError:
            break
        except KeyboardInterrupt:
            console.print("Input cleared · Ctrl+D to leave.", style="dim")
            continue
        stripped = line.strip()
        if not stripped:
            continue
        command, *args = stripped.split(maxsplit=1)
        if command in ("/exit", "/quit") and not args:
            break
        if command == "/new" and not args:
            chat_id = client.create_chat()["id"]
            console.print("Started a new chat.", style="dim")
        elif command == "/help" and not args:
            _help(console)
        elif command == "/clear" and not args:
            if console.is_terminal:
                console.clear()
            _header(client, console)
        elif command == "/think" and not args:
            show_thinking = not show_thinking
            no_think = False
            console.print("Reasoning expanded." if show_thinking else "Reasoning collapsed.", style="dim")
        elif command == "/model":
            if not args:
                _models(client, console)
            else:
                try:
                    client.model_use(args[0])
                    console.print(Text(f"Now using {args[0]}.", style="dim"))
                except KovaError as exc:
                    if exc.status != 404:
                        raise
                    console.print(Text(f'No model named "{args[0]}". Type /model to see them.', style="red"))
        elif stripped.startswith("/"):
            console.print("Unknown command. Type /help to see commands.", style="red")
        else:
            console.print()
            _render_turn(client, chat_id, stripped, no_think, console, show_thinking=show_thinking)
    console.print("Until next time.", style="dim")
