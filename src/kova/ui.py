"""Interactive terminal chat; the script-friendly ``ask`` command stays separate.

In a real terminal (POSIX, stdin and stdout both terminals) the chat is the
full-screen :class:`kova.tui.app.ChatApp`. Everywhere else, piped input,
Windows consoles, ``KOVA_SIMPLE=1``, the plain loop below runs instead: a
line prompt, Rich rendering, and no cursor tricks when output is a pipe.
"""

from __future__ import annotations

import os
import sys
import threading
from contextlib import nullcontext
from dataclasses import dataclass, field
from time import perf_counter

from prompt_toolkit import PromptSession
from prompt_toolkit.completion import CompleteEvent, Completion, Completer, WordCompleter
from prompt_toolkit.document import Document
from prompt_toolkit.history import InMemoryHistory
from prompt_toolkit.patch_stdout import patch_stdout
from prompt_toolkit.styles import Style
from rich.console import Console, Group
from rich.live import Live
from rich.markdown import Markdown
from rich.spinner import Spinner
from rich.table import Table
from rich.text import Text

from kova import theme
from kova.client import KovaClient, KovaError
from kova.config import load_config
from kova.modes import MODES
from kova.tui.app import COMMANDS, PERMISSIONS

_COMMANDS = {"/" + name: description for name, (_usage, description) in COMMANDS.items()}

# Arguments the commands offer for Tab completion, if any.
_COMMAND_ARGUMENTS = {"/mode": MODES, "/web": ("on", "off"), "/permissions": tuple(PERMISSIONS)}
_PLAN_MARKS = {"done": "☑", "in_progress": "◐", "pending": "☐"}

# Prompt_toolkit styling for the input line, the completion menu and the
# bottom toolbar. Hex colours mirror kova.theme; prompt_toolkit parses them.
_CHAT_STYLE = Style.from_dict({
    "prompt": f"bold {theme.USER}",
    "bottom-toolbar": f"bg:{theme.BORDER} {theme.MUTED}",
    "completion-menu.completion": f"bg:{theme.BORDER} {theme.ACCENT_SOFT}",
    "completion-menu.meta.completion": f"bg:{theme.BORDER} {theme.MUTED}",
    "completion-menu.completion.current": f"bg:{theme.ACCENT} #ffffff",
    "completion-menu.meta.completion.current": f"bg:{theme.ACCENT} #ffffff",
})


class _CommandCompleter(Completer):
    """Complete slash commands, and the argument of /mode and /web."""

    def __init__(self) -> None:
        self._commands = WordCompleter(list(_COMMANDS), sentence=True)
        self._arguments = {name: WordCompleter(list(words), sentence=True)
                           for name, words in _COMMAND_ARGUMENTS.items()}

    def get_completions(self, document: Document, event: CompleteEvent) -> list[Completion]:
        command, space, rest = document.text_before_cursor.lstrip().partition(" ")
        if space and command in self._arguments:
            return list(self._arguments[command].get_completions(Document(rest), event))
        return list(self._commands.get_completions(document, event))


def _short(text: str, limit: int = 100) -> str:
    """Keep server-supplied status text on one line, without Rich markup."""
    text = " ".join(text.split())
    return text if len(text) <= limit else text[:limit - 1] + "…"


def _format_args(args: object, limit: int = 60) -> str:
    """One-line preview of tool arguments for the live tool row."""
    if args is None:
        return ""
    if isinstance(args, dict):
        if len(args) == 1:
            value = next(iter(args.values()))
            preview = f'"{value}"' if isinstance(value, str) else str(value)
        else:
            preview = ", ".join(f"{key}={value}" for key, value in args.items())
    else:
        preview = str(args)
    return _short(preview, limit)


@dataclass
class _Block:
    kind: str
    chunks: list[str] = field(default_factory=list)
    name: str = ""
    args: str = ""
    ok: bool | None = None
    started: float = field(default_factory=lambda: perf_counter())
    elapsed: float | None = None
    items: list[dict] = field(default_factory=list)


@dataclass
class _Turn:
    no_think: bool
    show_thinking: bool
    blocks: list[_Block] = field(default_factory=list)
    finished: bool = False

    def age(self) -> float | None:
        """Seconds since the first block started; None before the first token."""
        if not self.blocks:
            return None
        return perf_counter() - self.blocks[0].started

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
            self.blocks.append(_Block("tool", name=event["name"],
                                      args=_format_args(event.get("args"))))
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
        elif kind == "plan":
            self.blocks.append(_Block("plan", items=[item for item in event.get("items") or []
                                                     if isinstance(item, dict)]))
        elif kind == "stopped":
            self.blocks.append(_Block("stopped"))

    def render(self) -> Group:
        items: list = [Text("● Kova", style=f"bold {theme.ACCENT}")]
        for block in self.blocks:
            text = "".join(block.chunks)
            if block.kind == "text":
                items.append(Markdown(text, code_theme=theme.CODE_THEME))
            elif block.kind == "reasoning":
                if block.elapsed is None and not self.finished:
                    age = perf_counter() - block.started
                    items.append(Spinner("dots",
                                         text=Text(f"Thinking… {age:.1f}s", style=theme.MUTED),
                                         style=theme.ACCENT))
                elif not self.no_think:
                    items.append(Text(f"  thought for {block.elapsed or 0:.1f}s",
                                      style=theme.MUTED))
                if self.show_thinking and not self.no_think:
                    items.append(Text(text, style=theme.MUTED))
            elif block.kind == "tool":
                if block.ok is None and not self.finished:
                    label = Text(f"⚙ {block.name}", style=f"bold {theme.ACCENT}")
                    if block.args:
                        label.append(f"  {block.args}", style=theme.MUTED)
                    items.append(Spinner("dots", text=label, style=theme.ACCENT))
                else:
                    if block.ok is True:
                        icon_style = f"bold {theme.SUCCESS}"
                        detail_style = theme.MUTED
                    else:
                        # Plain "red": committed tests pin ANSI red (\x1b[31m)
                        # here, which the theme's truecolor red would not emit.
                        icon_style = "bold red"
                        detail_style = "red" if block.ok is False else theme.MUTED
                    icon = "✓" if block.ok else "✗" if block.ok is False else "·"
                    detail = text if block.ok is not None else "interrupted"
                    row = Text("  ")
                    row.append(f"{icon} ", style=icon_style)
                    row.append(block.name)
                    if detail:
                        row.append(f" · {detail}", style=detail_style)
                    items.append(row)
            elif block.kind == "error":
                # Plain "red" for the same committed-test reason as above.
                items.append(Text(f"  Error: {text}", style="red"))
            elif block.kind == "plan":
                items.append(Text.assemble(("  ● ", theme.ACCENT), ("Update Todos", "bold")))
                for item in block.items:
                    status = item.get("status")
                    style = {"done": f"strike {theme.MUTED}", "in_progress": f"bold {theme.ACCENT}"}
                    items.append(Text(f"    {_PLAN_MARKS.get(status, '☐')} "
                                      f"{_short(str(item.get('text', '')))}",
                                      style=style.get(status, "")))
            elif block.kind == "stopped":
                items.append(Text("  Interrupted.", style=theme.MUTED))
        # Keep activity visible before the first token and between tool calls.
        if not self.finished and (not self.blocks or
                                  (self.blocks[-1].kind == "tool" and self.blocks[-1].ok is not None)):
            age = self.age()
            thinking = Text("Thinking…", style=theme.MUTED) if age is None else \
                Text(f"Thinking… {age:.1f}s", style=theme.MUTED)
            items.append(Spinner("dots", text=thinking, style=theme.ACCENT))
        return Group(*items)


def _approve(client: KovaClient, chat_id: str, event: dict, console: Console,
             live: Live | None) -> None:
    """Answer an approval event: ask when someone is at a terminal, else decline."""
    tool = str(event.get("tool", "a tool"))
    destructive = event.get("risk") == "destructive"
    summary = _short(str(event.get("summary") or _format_args(event.get("args"))), 160)
    decision, message = "deny", None
    if sys.stdin.isatty() and console.is_terminal:
        if live is not None:
            live.stop()
        console.print(Text.assemble(("  ? ", f"bold {theme.WARNING}"), (f"Allow {tool}", "bold"),
                                    (f"  {summary}", theme.MUTED)))
        if destructive:
            console.print("    Destructive: can delete data or change the system for good",
                          style=theme.ERROR)
        choices = "[y]es · [n]o" if destructive else "[y]es · [a]lways this chat · [n]o"
        try:
            answer = console.input(f"    {choices} › ").strip().lower()
            if answer in ("y", "yes"):
                decision = "allow"
            elif answer in ("a", "always") and not destructive:
                decision = "allow_session"
            else:
                message = console.input("    What should Kova do instead? (Enter to skip) › ").strip()
        except (EOFError, KeyboardInterrupt):
            pass
        if live is not None:
            live.start()
    else:
        console.print(f"  Declined {tool}: approvals need an interactive terminal.",
                      style=theme.MUTED)
    answer_approval = getattr(client, "answer_approval", None)
    if answer_approval is not None:
        answer_approval(chat_id, str(event.get("id", "")), decision, message or None)


def _stop_reply(client: KovaClient, chat_id: str) -> None:
    """Cancel the turn on the server too, so the model stops working on it."""
    stop = getattr(client, "stop_chat", None)
    if stop is None:
        return
    try:
        stop(chat_id)
    except KovaError:
        pass


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
            if console.is_terminal else None)
    try:
        with live or nullcontext():
            try:
                for event in events:
                    if event["type"] == "approval":
                        _approve(client, chat_id, event, console, live)
                        continue
                    turn.accept(event)
                    if event["type"] == "done":
                        break
            except KeyboardInterrupt:
                interrupted = True
                _stop_reply(client, chat_id)
            finally:
                turn.end_reasoning()
                turn.finished = True
    finally:
        events.close()
        # Printing after Live clears its transient viewport preserves even replies
        # taller than the terminal, rather than leaving only the cropped live view.
        console.print(turn.render())
        if interrupted:
            console.print("  Reply stopped · send another message when ready.", style=theme.MUTED)
        console.print()


def _header(client: KovaClient, console: Console, chat_id: str | None = None) -> None:
    health = client.health()
    cfg = load_config()
    notes = len(client.memory_list())
    chat = client.get_chat(chat_id) if chat_id else None
    mode = (chat.get("mode") if chat else None) or "work"
    web = (chat.get("web", True) if chat else True)

    status = Text("◆ Kova", style=f"bold {theme.ACCENT}")
    status.append("  ·  model ", style=theme.MUTED)
    status.append(str(health["model"]), style="bold")
    if not health["model_ok"]:
        status.append("  ·  offline", style=f"bold {theme.WARNING}")
    status.append("  ·  mode ", style=theme.MUTED)
    status.append(mode, style="bold")
    if not web:
        status.append("  ·  web off", style=theme.MUTED)
    status.append("  ·  ", style=theme.MUTED)
    status.append(f"{notes} {'note' if notes == 1 else 'notes'}", style=theme.MUTED)
    console.print(status)
    console.print(f"  Memory  {cfg.memory_path}", style=theme.MUTED)
    if not health["model_ok"]:
        console.print("  The model is unreachable — replies may fail. "
                      "Check kova setup or /model.", style=theme.MUTED)
    console.print("  /help commands  ·  /think reasoning  ·  Ctrl+D leave", style=theme.MUTED)
    console.print()


def _help(console: Console) -> None:
    table = Table(title="Chat commands", box=None, show_header=False, padding=(0, 2, 0, 0))
    table.add_column(style=theme.ACCENT, no_wrap=True)
    table.add_column()
    for command, description in _COMMANDS.items():
        table.add_row(command, description)
    console.print(table)
    console.print("↑ history · Tab complete · Ctrl+C stop reply / clear input · Ctrl+D leave",
                  style=theme.MUTED)
    console.print()


def _models(client: KovaClient, console: Console) -> None:
    active = client.health()["model"]
    table = Table(box=None, padding=(0, 2, 0, 0))
    table.add_column("Model", style=theme.ACCENT)
    table.add_column("Identifier")
    table.add_column("Provider", style=theme.MUTED)
    table.add_column("Server", style=theme.MUTED)
    for entry in sorted(client.models_list(), key=lambda m: (m.get("provider_name", ""), m["name"])):
        name = Text(entry["name"])
        if entry["name"] == active:
            name.append(" (active)", style=theme.SUCCESS)
        table.add_row(name, Text(entry["model"]), Text(entry.get("provider_name", "Custom")), Text(entry["base_url"]))
    console.print(table)
    console.print()


def _watch_reminders(client: KovaClient, console: Console) -> None:
    try:
        for event in client.events():
            if event.get("type") == "reminder":
                console.print(Text(f"⏰ {_short(event['text'], 200)}", style=theme.SUCCESS))
    except Exception:  # A dead event connection must never crash chat.
        pass


def _full_screen(console: Console) -> bool:
    """The full-screen chat needs a POSIX terminal on both ends; KOVA_SIMPLE=1 opts out."""
    return (os.name == "posix" and sys.stdin.isatty() and sys.stdout.isatty()
            and console.is_terminal and os.environ.get("TERM") != "dumb"
            and not os.environ.get("KOVA_SIMPLE"))


def _run_chat(client: KovaClient, chat_id: str | None = None, no_think: bool = False,
              *, new: bool = False, mode: str | None = None, web: bool | None = None,
              console: Console) -> None:
    if chat_id is None:
        chats = client.list_chats() if not new else []
        if chats:
            chat_id = chats[0]["id"]
            if mode is not None or web is not None:  # explicit flags win over stored settings
                client.update_chat(chat_id, mode=mode, web=web)
        else:
            chat_id = client.create_chat(mode=mode, web=web, cwd=os.getcwd())["id"]
    if _full_screen(console):
        from kova.tui.app import ChatApp

        cfg = load_config()
        ChatApp(client, chat_id, no_think=no_think, history_path=cfg.home / "chat_history.jsonl",
                memory_path=str(cfg.memory_path)).run()
        return
    _header(client, console, chat_id)
    current = client.get_chat(chat_id) or {}
    toolbar_state = {"mode": current.get("mode") or "work",
                     "web": current.get("web", True),
                     "model": str(client.health()["model"])}

    def _toolbar() -> list[tuple[str, str]]:
        state = "on" if toolbar_state["web"] else "off"
        return [("class:bottom-toolbar",
                 f" {toolbar_state['mode']} · {toolbar_state['model']} "
                 f"· web {state} · /help for commands ")]

    interactive = sys.stdin.isatty() and console.is_terminal
    session = None
    if interactive:
        session = PromptSession(history=InMemoryHistory(),
                                completer=_CommandCompleter(),
                                complete_while_typing=False,
                                style=None if "NO_COLOR" in os.environ else _CHAT_STYLE,
                                bottom_toolbar=None if "NO_COLOR" in os.environ else _toolbar)
        threading.Thread(target=_watch_reminders, args=(client, console), daemon=True).start()
    show_thinking = False
    while True:
        try:
            if session is None:
                line = console.input(f"[bold {theme.USER}]› [/]")
            else:
                with patch_stdout(raw=True):
                    line = session.prompt([("class:prompt", "› ")])
        except EOFError:
            break
        except KeyboardInterrupt:
            console.print("Input cleared · Ctrl+D to leave.", style=theme.MUTED)
            continue
        stripped = line.strip()
        if not stripped:
            continue
        command, *args = stripped.split(maxsplit=1)
        if command in ("/exit", "/quit") and not args:
            break
        if command == "/new" and not args:
            current = client.get_chat(chat_id) or {}
            chat_id = client.create_chat(mode=current.get("mode"), web=current.get("web"),
                                         permission=current.get("permission"),
                                         cwd=current.get("cwd") or os.getcwd())["id"]
            toolbar_state["mode"] = current.get("mode") or "work"
            toolbar_state["web"] = current.get("web", True)
            console.print("Started a new chat.", style=theme.MUTED)
        elif command == "/help" and not args:
            _help(console)
        elif command == "/clear" and not args:
            if console.is_terminal:
                console.clear()
            _header(client, console, chat_id)
        elif command == "/think" and not args:
            show_thinking = not show_thinking
            no_think = False
            console.print("Reasoning expanded." if show_thinking else "Reasoning collapsed.",
                          style=theme.MUTED)
        elif command == "/mode":
            if not args:
                chat = client.get_chat(chat_id) or {}
                console.print(f"Current mode: {chat.get('mode') or 'work'}. "
                              f"Switch with /mode {' or '.join(MODES)}.", style=theme.MUTED)
            elif args[0] in MODES:
                client.update_chat(chat_id, mode=args[0])
                toolbar_state["mode"] = args[0]
                console.print(f"Mode: {args[0]}. Takes effect on your next message.",
                              style=theme.MUTED)
            else:
                console.print(f'Unknown mode "{args[0]}". Use /mode {" or ".join(MODES)}.',
                              style=theme.ERROR)
        elif command == "/web":
            if not args:
                chat = client.get_chat(chat_id) or {}
                state = "on" if chat.get("web", True) else "off"
                console.print(f"Web search is {state}. Toggle with /web on or /web off.",
                              style=theme.MUTED)
            elif args[0] in ("on", "off"):
                client.update_chat(chat_id, web=args[0] == "on")
                toolbar_state["web"] = args[0] == "on"
                console.print(f"Web search {args[0]}. Takes effect on your next message.",
                              style=theme.MUTED)
            else:
                console.print("Use /web on or /web off.", style=theme.ERROR)
        elif command == "/model":
            if not args:
                _models(client, console)
            else:
                try:
                    client.model_use(args[0])
                    toolbar_state["model"] = args[0]
                    console.print(Text(f"Now using {args[0]}.", style=theme.MUTED))
                except KovaError as exc:
                    if exc.status != 404:
                        raise
                    console.print(Text(f'No model named "{args[0]}". Type /model to see them.',
                                       style=theme.ERROR))
        elif command == "/permissions":
            chat = client.get_chat(chat_id) or {}
            if not args:
                current_level = chat.get("permission")
                state = f"Permissions: {current_level}. " if current_level else ""
                console.print(f"{state}Switch with /permissions {' | '.join(PERMISSIONS)}.",
                              style=theme.MUTED)
            elif args[0] in PERMISSIONS:
                updated = client.update_chat(chat_id, permission=args[0])
                if "permission" in updated:
                    console.print(f"Permissions: {args[0]} · {PERMISSIONS[args[0]]}.",
                                  style=theme.MUTED)
                else:
                    console.print("This Kova server doesn't support permission modes yet.",
                                  style=theme.ERROR)
            else:
                console.print(f"Use /permissions {' | '.join(PERMISSIONS)}.", style=theme.ERROR)
        elif command == "/cd":
            target = os.path.abspath(os.path.expanduser(args[0] if args else "~"))
            if os.path.isdir(target):
                client.update_chat(chat_id, cwd=target)
                console.print(f"Folder: {target}", style=theme.MUTED)
            else:
                console.print(f"Not a folder: {target}", style=theme.ERROR)
        elif command == "/resume":
            chats = client.list_chats()[:15]
            if args and args[0].isdigit() and 1 <= int(args[0]) <= len(chats):
                chat_id = chats[int(args[0]) - 1]["id"]
                resumed = client.get_chat(chat_id) or {}
                toolbar_state["mode"] = resumed.get("mode") or "work"
                toolbar_state["web"] = resumed.get("web", True)
                console.print(f"Resumed {chats[int(args[0]) - 1].get('title') or 'Untitled'}.",
                              style=theme.MUTED)
            else:
                for index, chat in enumerate(chats, 1):
                    mark = "›" if chat["id"] == chat_id else " "
                    console.print(Text(f"{mark}{index:>3}  {chat.get('title') or 'Untitled'}"))
                console.print("/resume <n> opens one.", style=theme.MUTED)
        elif stripped.startswith("/"):
            console.print("Unknown command. Type /help to see commands.", style=theme.ERROR)
        else:
            if session is None:
                # Piped input leaves no visible line behind, so echo it as the
                # user's turn; an interactive prompt already stays on screen.
                console.print(Text(f"› {stripped}", style=f"bold {theme.USER}"))
            else:
                console.print()
            _render_turn(client, chat_id, stripped, no_think, console, show_thinking=show_thinking)
    console.print("Until next time.", style=theme.MUTED)
