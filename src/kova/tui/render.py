"""What Kova's terminal chat prints: the header, streamed replies, tool rows,
plans, permission prompts, the spinner and per-turn totals.

The transcript is written through ``out`` — straight to the terminal, or, in
the full-screen UI, to a ``Screen`` that keeps the live region (spinner,
running tool, input box) below it. In live mode the spinner is not drawn here;
``live_lines`` supplies it for the live region instead.

Ported and adapted from Kaine's ``kaine/cli_render.py`` for Kova's event
protocol (``text``, ``reasoning``, ``tool_call``, ``tool_result``, ``error``,
``done``, ``plan``, ``approval``, ``stopped``) and Kova's visual language
(``\u25c6 Kova`` header, ``\u203a`` prompt, ``\u25cf Kova`` replies, tool rows
``\u2699 name "args"`` running and ``\u2713 name \u00b7 result`` done).
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import time

from .markdown import ACCENT, BASH, ERROR, MEMORY, SUCCESS, USER, WARNING, MarkdownStream, Palette, \
    clean, display_width
from .widgets import pick_verb, pulse, spinner_line

# Tool rows read like calls for run_command: Bash(git status).
TOOL_NAMES = {"run_command": "Bash", "web_search": "Web", "web_fetch": "Read",
              "read_file": "Read", "write_file": "Write", "list_files": "List"}
ACTIVE_TITLES = {
    "run_command": "Running", "web_search": "Searching the web", "web_fetch": "Reading a page",
    "read_file": "Reading", "write_file": "Writing", "list_files": "Listing files",
    "memory_save": "Saving to memory", "memory_search": "Searching memory",
    "reminder_add": "Setting a reminder", "reminder_list": "Listing reminders",
    "system_info": "Checking your system",
}
PERMISSION_TITLES = {"run_command": "Run a shell command?", "write_file": "Write a file?"}
PLAN_MARKS = {"done": "\u2611", "in_progress": "\u25d0", "pending": "\u2610"}
OUTPUT_PREVIEW_LINES = 4


def tool_detail(name: str, args: dict | None) -> str:
    """The argument shown between a tool's parentheses."""
    args = args if isinstance(args, dict) else {}
    if name == "run_command":
        return str(args.get("command", "")).strip()
    if name in ("web_search", "memory_search"):
        return str(args.get("query", ""))
    if name == "web_fetch":
        return str(args.get("url", ""))
    if name in ("read_file", "write_file", "list_files"):
        return str(args.get("path", ""))
    if name == "memory_save":
        return str(args.get("title", ""))
    if name == "reminder_add":
        return str(args.get("text", ""))
    return ""


def args_preview(args: object, limit: int = 60) -> str:
    """One-line preview of tool arguments for the running tool row."""
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
    return shorten(preview, limit)


def compact_number(count: int | float) -> str:
    count = int(count or 0)
    if count >= 1_000_000:
        return f"{count / 1_000_000:.1f}M"
    if count >= 10_000:
        return f"{count / 1000:.0f}K"
    return f"{count / 1000:.1f}K" if count >= 1000 else str(count)


def duration(seconds: float) -> str:
    seconds = max(0.0, float(seconds))
    if seconds < 10:
        return f"{seconds:.1f}s"
    if seconds < 60:
        return f"{seconds:.0f}s"
    return f"{int(seconds // 60)}m {int(seconds % 60):02d}s"


def wrap_words(text: str, width: int) -> list[str]:
    """Plain text wrapped at word boundaries into rows of at most ``width`` columns."""
    import textwrap
    rows: list[str] = []
    for paragraph in clean(str(text)).split("\n"):
        rows += textwrap.wrap(paragraph, max(10, width), break_on_hyphens=False) or [""]
    return rows


def shorten(text: str, width: int) -> str:
    text = " ".join(clean(str(text)).split())
    if display_width(text) <= width:
        return text
    out, size = "", 0
    for char in text:
        step = display_width(char)
        if size + step > max(1, width - 1):
            break
        out += char
        size += step
    return out + "\u2026"


class Renderer:
    """Writes a reply to ``out`` as its events arrive; the spinner and the
    running tool are not written but offered to the live region by ``live_lines``."""

    def __init__(self, out=None, *, color: bool | None = None, verbose: bool = False):
        self.out = out or sys.stdout
        self.tty = bool(getattr(self.out, "isatty", lambda: False)())
        if color is None:
            color = self.tty and not os.environ.get("NO_COLOR") and os.environ.get("TERM") != "dumb"
        self.p = Palette(color)
        self.verbose = verbose
        self.stream: MarkdownStream | None = None
        self.thinking: MarkdownStream | None = None
        self.spinner = ""
        self.activity = ""        # what a running tool is doing, for the spinner
        self.verb = "Working"
        self.turn_started = 0.0
        self.think_started = 0.0
        self.pending_tool: tuple[str, dict] | None = None
        self.stopping = False
        self.asking = False
        self.pass_chars = 0       # the pass streaming now, estimated from its text
        self.no_think = False     # hide reasoning entirely (--no-think)
        self.in_turn = False      # between begin_turn and finish_turn
        self.headed = False       # this turn's "\u25cf Kova" line is out
        self.stopped = False      # the turn ended with a "stopped" event
        self.after_block = False  # a tool row or plan was the last thing shown

    # ------------------------------------------------------------ basics
    @property
    def columns(self) -> int:
        return max(20, shutil.get_terminal_size((100, 24)).columns) if self.tty else 100

    @property
    def width(self) -> int:
        """Where text wraps: the terminal, but never wider than is comfortable to read."""
        return max(40, min(self.columns - 1, 120))

    def write(self, text: str) -> None:
        self.out.write(text)
        self.out.flush()

    def line(self, text: str = "") -> None:
        self.write(text + "\n")

    def error(self, text: str) -> None:
        """A failure, wrapped rather than cut off, with room after it."""
        for index, row in enumerate(wrap_words(text, self.width - 2)):
            self.line(self.p.red("\u2717 " if index == 0 else "  ") + self.p.red(row))
        self.line()

    @property
    def tokens(self) -> int:
        return self.pass_chars // 4

    # ----------------------------------------------------------- spinner
    def status(self, text: str) -> None:
        self.spinner = str(text or "").rstrip("\u2026. ") or "Working"

    def live_lines(self, frame: int, now: float | None = None) -> list[str]:
        """The running tool and the spinner, for the live region."""
        if not self.spinner:
            return []
        now = time.monotonic() if now is None else now
        lines = []
        if self.pending_tool and not self.asking:
            name, args = self.pending_tool
            lines.append("  " + pulse(self.p, frame) + " " + self.tool_header(name, args))
        lines.append(spinner_line(self.p, verb=self.activity or self.verb, frame=frame,
                                  elapsed=now - (self.turn_started or now), tokens=self.tokens,
                                  stopping=self.stopping,
                                  thinking=bool(self.think_started and not self.stream)))
        return lines

    # ------------------------------------------------------------ prompts
    def user(self, text: str, *, mark: str = "\u203a", colour: str = USER) -> None:
        """Echo a submitted prompt, one ``mark`` line per paragraph, in Kova's green."""
        lines = clean(str(text)).splitlines() or [""]
        for index, line in enumerate(lines):
            lead = mark if index == 0 else " "
            self.line(self.p.paint("1;" + colour, lead + " ") + line if self.p.color
                      else f"{lead} {line}")
        self.line()

    def shell(self, command: str) -> None:
        self.user(command, mark="!", colour=BASH)

    def remember(self, note: str) -> None:
        self.user(note, mark="#", colour=MEMORY)

    def reminder(self, text: str) -> None:
        """A reminder that popped in while chatting."""
        self.line(self.p.paint("1;" + SUCCESS, "\u23f0 ") + shorten(text, self.width - 4))
        self.line()

    # ------------------------------------------------------------ events
    def begin_turn(self) -> None:
        self.turn_started = time.monotonic()
        self.think_started = 0.0
        self.stopping = False
        self.activity = ""
        self.verb = pick_verb()
        self.pass_chars = 0
        self.in_turn, self.headed, self.stopped, self.after_block = True, False, False, False
        self.status("Working")

    def reply_header(self) -> None:
        """The turn's ``\u25cf Kova`` line, once, before the first thing it shows."""
        if self.in_turn and not self.headed:
            self.headed = True
            self.line(self.p.paint("1;" + ACCENT, "\u25cf Kova") if self.p.color else "\u25cf Kova")

    def handle(self, event: dict) -> None:
        kind = event.get("type")
        handler = getattr(self, f"_on_{kind}", None)
        if handler is not None:
            handler(event)

    def _on_reasoning(self, event) -> None:
        text = clean(str(event.get("text", "")))
        self.pass_chars += len(text)
        if not self.think_started:
            self.think_started = time.monotonic()
            self.status("Thinking")
        if self.verbose and not self.no_think:
            if self.thinking is None:
                self.reply_header()
                self.thinking = MarkdownStream(self.write, self.width, self.p, first="\u273b ",
                                               base=["2", "3"])
            self.thinking.feed(text)

    def _end_thinking(self) -> None:
        if self.thinking is not None:
            self.thinking.finish()
            self.thinking = None
            self.line()
        elif self.think_started and not self.no_think:
            self.reply_header()
            self.line(self.p.dim(f"  thought for {duration(time.monotonic() - self.think_started)}"))
        self.think_started = 0.0

    def _on_text(self, event) -> None:
        text = str(event.get("text", ""))
        self.pass_chars += len(text)
        if self.stream is None:
            self._end_thinking()
            self.reply_header()
            if self.after_block:
                self.line()
            self.after_block = False
            self.stream = MarkdownStream(self.write, self.width, self.p, margin="  ")
        self.stream.feed(text)

    def end_text(self) -> None:
        if self.stream is not None:
            self.stream.finish()
            self.stream = None
            self.line()
        if self.thinking is not None or self.think_started:
            self._end_thinking()

    def _on_done(self, _event) -> None:
        self.end_text()
        self.status("")

    def _on_tool_call(self, event) -> None:
        self.end_text()
        name = str(event.get("name", ""))
        args = event.get("args") if isinstance(event.get("args"), dict) else {}
        self.pending_tool = (name, args)
        self.activity = ACTIVE_TITLES.get(name, "Working")

    def _on_tool_result(self, event) -> None:
        name = str(event.get("name", ""))
        args = self.pending_tool[1] if self.pending_tool and self.pending_tool[0] == name else {}
        self.pending_tool = None
        self.activity = ""
        ok = bool(event.get("ok", True))
        summary = str(event.get("summary") or "")
        self.tool_row(name, args, ok, summary)

    def _on_error(self, event) -> None:
        self.end_text()
        self.reply_header()
        self.error(event.get("message", "The request failed"))

    def _on_stopped(self, _event) -> None:
        self.end_text()
        self.interrupted()

    def interrupted(self) -> None:
        if not self.stopped:
            self.stopped = True
            self.line(self.p.dim("  \u23bf  Interrupted \u00b7 send another message when ready"))

    def _on_plan(self, event) -> None:
        self.end_text()
        self.plan(event.get("items") or [])

    # ------------------------------------------------------------ tool rows
    def tool_header(self, name: str, args: dict) -> str:
        """A running tool: ``Bash(git status)`` for commands, else ``name "args"``."""
        p = self.p
        if name == "run_command":
            room = self.width - 14
            return p.bold(TOOL_NAMES[name]) + p.dim("(" + shorten(tool_detail(name, args), room) + ")")
        preview = args_preview(args, max(10, self.width - display_width(name) - 10))
        return p.paint("1;" + ACCENT, name) + (p.dim("  " + preview) if preview else "")

    def tool_row(self, name, args, ok, summary) -> None:
        """A finished tool: ``\u2713 name \u00b7 result``; commands get their output underneath."""
        p = self.p
        self.reply_header()
        icon = p.paint("1;" + (SUCCESS if ok else ERROR), "\u2713" if ok else "\u2717")
        if name == "run_command":
            command = tool_detail(name, args) or "command"
            self.line(f"  {icon} {p.bold(TOOL_NAMES[name])}"
                      + p.dim("(" + shorten(command, self.width - 14) + ")"))
            for index, text in enumerate(self._run_command_body(summary, ok)):
                self.line(p.dim("    \u23bf  " if index == 0 else "       ") + text)
            self.after_block = True
            return
        detail = shorten(summary, self.width - display_width(name) - 8)
        row = f"  {icon} {name}"
        if detail:
            row += (p.dim if ok else p.red)(f" \u00b7 {detail}")
        self.line(row)
        self.after_block = True

    def _run_command_body(self, summary: str, ok: bool) -> list[str]:
        p = self.p
        lines = [line.rstrip() for line in command_output(summary).splitlines()]
        while lines and not lines[-1]:
            lines.pop()
        paint = (lambda text: text) if ok else p.red
        shown = [paint(shorten(line, self.width - 9)) for line in lines[:OUTPUT_PREVIEW_LINES]]
        if len(lines) > OUTPUT_PREVIEW_LINES:
            more = len(lines) - OUTPUT_PREVIEW_LINES
            shown.append(p.dim(f"\u2026 +{more} line{'' if more == 1 else 's'}"))
        elif not lines:
            shown.append(p.dim("(No output)") if ok else p.red("Failed"))
        return shown

    def plan(self, items) -> None:
        """The agent's checklist as an ``Update Todos`` block: \u2610 / \u25d0 / \u2611."""
        if not items:
            return
        p = self.p
        self.reply_header()
        self.line(f"  {p.paint('1;' + ACCENT, '\u25cf')} {p.bold('Update Todos')}")
        for index, item in enumerate(items):
            status = item.get("status") if isinstance(item, dict) else None
            text = (item.get("text") if isinstance(item, dict) else str(item)) or ""
            mark = PLAN_MARKS.get(status, "\u2610")
            title = shorten(text, self.width - 12)
            if status == "done":
                row = p.paint("2;9", f"{mark} {title}")
            elif status == "in_progress":
                row = p.paint("1;" + ACCENT, f"{mark} {title}")
            else:
                row = f"{mark} {title}"
            self.line(p.dim("    \u23bf  " if index == 0 else "       ") + row)
        self.after_block = True

    # -------------------------------------------------------- permission
    def permission_card(self, name: str, args: dict, risk: str, summary: str) -> \
            tuple[str, list[str], str]:
        """Title, body lines and frame colour for the approval dialog."""
        p = self.p
        title = PERMISSION_TITLES.get(name, f"Allow {name}?")
        body = [""]
        if name == "run_command":
            for line in (clean(tool_detail(name, args)).splitlines() or [""])[:6]:
                body.append("  " + p.code(shorten(line, self.width - 10)))
        else:
            preview = args_preview(args, self.width - 10)
            body.append("  " + p.bold(name) + (p.dim("  " + preview) if preview else ""))
        if summary:
            body += ["  " + p.dim(row) for row in wrap_words(summary, self.width - 10)[:3]]
        if risk == "destructive":
            body.append("  " + p.red("Destructive: can delete data or change the system for good"))
        body += ["", "Do you want to proceed?"]
        colour = ERROR if risk == "destructive" else WARNING
        return title, body, colour

    # ------------------------------------------------------------ totals
    def finish_turn(self, *, stopped: bool = False) -> None:
        self.end_text()
        if stopped:
            self.interrupted()
        elapsed = time.monotonic() - self.turn_started if self.turn_started else 0
        parts = [f"Worked for {duration(elapsed)}"] if elapsed else []
        if self.pass_chars:
            parts.append(f"\u2193 {compact_number(self.tokens)} tokens")
        if parts and self.headed:
            self.line(self.p.dim("  \u273b " + " \u00b7 ".join(parts)))
        if self.headed:
            self.line()
        self.spinner = ""
        self.activity = ""
        self.pending_tool = None
        self.stopping = False
        self.in_turn = False


def command_output(summary: str) -> str:
    """The output text of a command result, which may arrive as JSON or plain text."""
    text = clean(str(summary or ""))
    try:
        data = json.loads(text)
    except ValueError:
        return text
    if not isinstance(data, dict):
        return text
    parts = [str(data[key]) for key in ("output", "stdout", "stderr", "error") if data.get(key)]
    code = data.get("exit_code", data.get("returncode"))
    if code not in (None, 0):
        parts.append(f"exit code {code}")
    return "\n".join(parts)


__all__ = [
    'ACTIVE_TITLES',
    'PERMISSION_TITLES',
    'PLAN_MARKS',
    'TOOL_NAMES',
    'Renderer',
    'args_preview',
    'command_output',
    'compact_number',
    'duration',
    'shorten',
    'tool_detail',
    'wrap_words',
]
