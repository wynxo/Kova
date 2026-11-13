"""What Kova's terminal chat prints: the header, streamed replies, tool rows,
plans, permission prompts, the spinner and per-turn totals.

The transcript is written through ``out`` — straight to the terminal, or, in
the full-screen UI, to a ``Screen`` that keeps the live region (spinner,
running tool, input box) below it. In live mode the spinner is not drawn here;
``live_lines`` supplies it for the live region instead.

Ported and adapted from Kaine's ``kaine/cli_render.py`` for Kova's event
protocol (``text``, ``reasoning``, ``tool_call``, ``tool_result``, ``error``,
``done``, ``plan``, ``approval``, ``stopped``). The look: the user's turn is a
``\u276f`` line on a faint teal tint; Kova's starts with a gradient
``\u25c6 Kova``; tools are small coloured chips (``\u2713 Search  query \u00b7 5
results``); reasoning collapses to one quiet ``\u273b Thought for 2s`` line.
"""
from __future__ import annotations

import json
import re
import os
import shutil
import sys
import time

from kova import theme

from .markdown import (
    ACCENT,
    BASH,
    ERROR,
    MEMORY,
    SUCCESS,
    USER,
    WARNING,
    MarkdownStream,
    Palette,
    clean,
    display_width,
    strip_ansi,
)
from .style import FAINT, bg, mix
from .widgets import pick_verb, pulse, spinner_line

# Each tool's chip: a short name, and the colour it is drawn in.
TOOL_NAMES = {"run_command": "Bash", "web_search": "Search", "web_fetch": "Fetch",
              "read_file": "Read", "write_file": "Write", "list_files": "List",
              "memory_save": "Remember", "memory_search": "Recall",
              "reminder_add": "Remind", "reminder_list": "Reminders", "system_info": "System"}
TOOL_COLOURS = {"run_command": theme.BASH, "web_search": theme.MEMORY, "web_fetch": theme.MEMORY,
                "read_file": theme.CODE, "write_file": theme.CODE, "list_files": theme.CODE,
                "memory_save": theme.USER, "memory_search": theme.USER,
                "reminder_add": theme.WARNING, "reminder_list": theme.WARNING}
ACTIVE_TITLES = {
    "run_command": "Running", "web_search": "Searching the web", "web_fetch": "Reading a page",
    "read_file": "Reading", "write_file": "Writing", "list_files": "Listing files",
    "memory_save": "Saving to memory", "memory_search": "Searching memory",
    "reminder_add": "Setting a reminder", "reminder_list": "Listing reminders",
    "system_info": "Checking your system",
}
PERMISSION_TITLES = {"run_command": "Run a shell command?", "write_file": "Write a file?"}
PLAN_MARKS = {"done": "\u2713", "in_progress": "\u25d0", "pending": "\u25cb"}
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


def compact_number(count: float) -> str:
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
        self.name = "Kova"
        self.verbose = verbose
        self.tools_expanded = False
        self.reasoning_text = ""
        self.tool_drafts: dict = {}
        self.tool_history: list = []
        self.detail_offset = 0
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
        self.last_stats = ""      # the last turn's tokens and time, for the status bar
        self.last_tokens = 0      # the last turn's output tokens, for the status bar
        self.last_elapsed = 0.0   # the last turn's wall-clock time, for the status bar

    # ------------------------------------------------------------ basics
    @property
    def columns(self) -> int:
        return max(20, shutil.get_terminal_size((100, 24)).columns) if self.tty else 100

    @property
    def width(self) -> int:
        """Where text wraps: the terminal, but never wider than is comfortable to read."""
        return max(10, min(self.columns - 1, 104))

    def write(self, text: str) -> None:
        self.out.write(text)
        self.out.flush()

    def line(self, text: str = "") -> None:
        self.write(text + "\n")

    def error(self, text: str) -> None:
        """A failure, wrapped rather than cut off, with room after it."""
        p = self.p
        for index, row in enumerate(wrap_words(text, self.width - 4)):
            lead = p.paint("1;" + ERROR, "\u2717 ") if index == 0 else "  "
            self.line("  " + lead + p.red(row))
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
        lines.append("  " + spinner_line(self.p, verb=self.activity or self.verb, frame=frame,
                                  elapsed=now - (self.turn_started or now), tokens=self.tokens,
                                  stopping=self.stopping,
                                  thinking=bool(self.think_started and not self.stream)))
        return lines

    def _on_tool_call_delta(self, event) -> None:
        # Snapshots are keyed by model call index; they never execute a tool.
        self.end_text()
        self.tool_drafts[event.get("index", 0)] = event

    def detail_lines(self) -> list[str]:
        """Replaceable live details, never appended token-by-token to scrollback."""
        from pathlib import Path
        from .markdown import _highlighter
        rows = []
        if self.reasoning_text and not self.no_think:
            if self.verbose:
                rows.append(self.p.faint("  Thinking · ctrl+t collapse"))
                rows.extend(self.p.faint("  " + row) for row in
                            wrap_words(self.reasoning_text, self.width - 4))
            elif self.think_started:
                rows.append(self.p.faint("  thinking…"))
        for draft in self.tool_drafts.values():
            name = draft.get("name", "tool")
            raw = draft.get("arguments", "")
            args = partial_arguments(raw)
            filename = args.get("path", args.get("filename", ""))
            rows.append(self.p.dim("  " + name + (" · " + filename if filename else "") + " · generating…"))
            code = next((args[k] for k in ("command", "content", "new_text", "new_string", "replacement") if k in args), raw)
            language = "bash" if name == "run_command" else Path(filename).suffix.lstrip(".") or "text"
            highlight = _highlighter(language, self.p.level) if self.p.color else None
            for row in code.split("\n"):
                for piece in wrap_literal(row, self.width - 6):
                    rows.append("    " + (highlight(piece) if highlight else piece))
        if self.tools_expanded:
            if self.pending_tool:
                name, args = self.pending_tool
                rows.append(self.p.dim("  " + name + " · running"))
                rows.extend("    " + row for row in wrap_words(json.dumps(args, ensure_ascii=False, indent=2), self.width - 6))
            for name, args, ok, output in self.tool_history:
                rows.append(self.p.dim("  " + ("✓ " if ok else "✗ ") + name))
                for value in (json.dumps(args, ensure_ascii=False, indent=2), output):
                    rows.extend("    " + row for row in wrap_literal(value, self.width - 6))
        if self.detail_offset:
            rows = rows[:-self.detail_offset] if self.detail_offset < len(rows) else rows[:1]
        return rows

    # ------------------------------------------------------------ prompts
    def user(self, text: str, *, mark: str = "\u276f", colour: str = USER,
             tint: str = theme.USER) -> None:
        """Echo a submitted prompt: a ``mark`` and the text on a faint tint of
        ``tint``, so the user's turns stand apart from Kova's at a glance."""
        p = self.p
        rows: list[str] = []
        for paragraph in clean(str(text)).splitlines() or [""]:
            rows += wrap_words(paragraph, self.width - 4) if paragraph.strip() else [""]
        if len(rows) > 40:  # a pasted wall of text: show its start and end
            rows = [*rows[:18], f"\u2026 {len(rows) - 36} more lines", *rows[-18:]]
        size = max(display_width(row) for row in rows) + 4
        shade = bg(mix(tint, theme.SURFACE, 0.86)) if p.level >= 2 else ""
        for index, row in enumerate(rows):
            lead = mark if index == 0 else " "
            if not p.color:
                self.line(f"{lead} {row}")
                continue
            pad = " " * (size - display_width(row) - 3)
            if shade:
                self.line(p.paint(f"1;{colour};{shade}", " " + lead + " ")
                          + p.paint(shade, row + pad))
            else:
                self.line(p.paint("1;" + colour, lead + " ") + row)
        self.line()

    def shell(self, command: str) -> None:
        self.user(command, mark="!", colour=BASH, tint=theme.BASH)

    def remember(self, note: str) -> None:
        self.user(note, mark="#", colour=MEMORY, tint=theme.MEMORY)

    def reminder(self, text: str) -> None:
        """A reminder that popped in while chatting."""
        self.line(self.p.pill("Reminder", theme.SUCCESS) + " " + shorten(text, self.width - 14))
        self.line()

    # ------------------------------------------------------------ events
    def begin_turn(self) -> None:
        self.reasoning_text = ""
        self.tool_drafts.clear()
        self.tool_history.clear()
        self.detail_offset = 0
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
            p = self.p
            self.line(p.paint("1;" + ACCENT, "\u25c6") + " " + p.gradient(self.name, bold=True)
                      if p.color else "\u25c6 " + self.name)

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
        self.reasoning_text += text

    def _end_thinking(self) -> None:
        if self.thinking is not None:
            self.thinking.finish()
            self.thinking = None
            self.line()
        elif self.think_started and not self.no_think:
            self.reply_header()
            took = duration(time.monotonic() - self.think_started)
            self.line(self.p.faint("  \u273b ") + self.p.paint(f"3;{FAINT}" if self.p.level >= 2 else "2;3",
                                                            f"Thought for {took}"))
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

    def _on_compacted(self, event) -> None:
        self.end_text()
        self.line(f"  Compacted {event.get('count', 0)} messages:")
        self.line(str(event.get('summary', '')))

    def _on_compact_skipped(self, event) -> None:
        self.line(str(event.get('message', 'Nothing to compact.')))

    def _on_done(self, _event) -> None:
        self.end_text()
        self.status("")

    def _on_tool_call(self, event) -> None:
        self.end_text()
        name = str(event.get("name", ""))
        self.tool_drafts = {i: draft for i, draft in self.tool_drafts.items()
                            if draft.get("id") != event.get("id") and draft.get("name") != name}
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
        self.tool_history.append((name, args, ok, str(event.get("output", summary))))
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
            self.line("  " + self.p.yellow("\u25fc Interrupted")
                      + self.p.dim(" \u00b7 send another message when ready"))

    def _on_plan(self, event) -> None:
        self.end_text()
        self.plan(event.get("items") or [])

    # ------------------------------------------------------------ tool rows
    def chip(self, name: str, ok: bool = True) -> str:
        """The tool's coloured name chip: ``Bash``, ``Search``, ``Read``..."""
        label = TOOL_NAMES.get(name, name or "tool")
        return self.p.pill(label, TOOL_COLOURS.get(name, theme.ACCENT) if ok else theme.ERROR)

    def _detail(self, name: str, args: dict, room: int) -> str:
        detail = tool_detail(name, args) or args_preview(args, room)
        return shorten(detail, room) if detail else ""

    def _call(self, chip: str, detail: str, paint=None) -> str:
        """A chip and its detail: side by side in colour, ``Name(detail)`` without."""
        if not self.p.color:
            return chip + (f"({detail})" if detail else "")
        return chip + (" " + (paint or (lambda text: text))(detail) if detail else "")

    def tool_header(self, name: str, args: dict) -> str:
        """A running tool: its chip and what it is working on."""
        chip = self.chip(name)
        detail = self._detail(name, args, max(10, self.width - display_width(strip_ansi(chip)) - 8))
        return self._call(chip, detail, self.p.dim)

    def tool_row(self, name, args, ok, summary) -> None:
        """A finished tool: ``\u2713 Search  query \u00b7 result``; commands get
        their output underneath on a quiet rule."""
        p = self.p
        self.reply_header()
        icon = p.paint("1;" + (SUCCESS if ok else ERROR), "\u2713" if ok else "\u2717")
        chip = self.chip(name, ok)
        used = display_width(strip_ansi(chip)) + 6
        if name == "run_command":
            command = tool_detail(name, args) or "command"
            self.line(f"  {icon} " + self._call(chip, shorten(command, self.width - used)))

            self.after_block = True
            return
        detail = self._detail(name, args, max(8, (self.width - used) // 2))
        result = shorten(summary, max(8, self.width - used - display_width(detail) - 3))
        row = f"  {icon} " + self._call(chip, detail)
        if result:
            row += p.faint(" \u00b7 ") + (p.dim if ok else p.red)(result)
        self.line(row)
        self.after_block = True

    def _run_command_body(self, summary: str, ok: bool) -> list[str]:
        p = self.p
        lines = [line.rstrip() for line in command_output(summary).splitlines()]
        while lines and not lines[-1]:
            lines.pop()
        paint = p.dim if ok else p.red
        shown = [paint(shorten(line, self.width - 8)) for line in lines[:OUTPUT_PREVIEW_LINES]]
        if len(lines) > OUTPUT_PREVIEW_LINES:
            more = len(lines) - OUTPUT_PREVIEW_LINES
            shown.append(p.faint(f"\u2026 +{more} line{'' if more == 1 else 's'}"))
        elif not lines:
            shown.append(p.faint("(No output)") if ok else p.red("Failed"))
        return shown

    def plan(self, items) -> None:
        """The agent's checklist: \u2713 done, \u25d0 doing, \u25cb to do."""
        if not items:
            return
        p = self.p
        self.reply_header()
        done = sum(1 for item in items if isinstance(item, dict) and item.get("status") == "done")
        self.line(f"  {p.paint(ACCENT, '\u25c6')} {p.bold('Plan')} " + p.dim(f"{done}/{len(items)}"))
        for item in items:
            status = item.get("status") if isinstance(item, dict) else None
            text = (item.get("text") if isinstance(item, dict) else str(item)) or ""
            mark = PLAN_MARKS.get(status, PLAN_MARKS["pending"])
            title = shorten(text, self.width - 8)
            if status == "done":
                row = p.green(mark) + " " + p.paint(f"9;{FAINT}" if p.level >= 2 else "2;9", title)
            elif status == "in_progress":
                row = p.paint("1;" + ACCENT, f"{mark} {title}")
            else:
                row = p.dim(mark) + " " + title
            self.line("    " + row)
        self.after_block = True

    # -------------------------------------------------------- permission
    def permission_card(self, name: str, args: dict, risk: str, summary: str) -> \
            tuple[str, list[str], str]:
        """Title, body lines and frame colour for the approval dialog."""
        p = self.p
        title = PERMISSION_TITLES.get(name, f"Allow {name}?")
        body = ["", "  " + p.dim(f"Operation: {name} · Risk: {risk}")]
        if name == "run_command":
            for line in (clean(str(args.get("command", ""))).splitlines() or [""]):
                body.extend("  " + p.code(row) for row in wrap_words(line, self.width - 10))
        else:
            body.extend("  " + p.code(row) for row in wrap_words(clean(str(args.get("path", ""))), self.width - 10))
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
            parts.append(f"{compact_number(self.tokens)} tokens")
        self.last_tokens = self.tokens if self.pass_chars else 0
        self.last_elapsed = elapsed
        if elapsed:
            self.last_stats = " \u00b7 ".join(
                ([f"{compact_number(self.tokens)} tokens"] if self.pass_chars else [])
                + [duration(elapsed)])
        if parts and self.headed:
            self.line(self.p.faint("  \u273b " + " \u00b7 ".join(parts)))
        if self.headed:
            self.line()
        self.spinner = ""
        self.activity = ""
        self.pending_tool = None
        self.tool_drafts.clear()
        self.stopping = False
        self.in_turn = False


def wrap_literal(text: str, width: int) -> list[str]:
    """Wrap code/output without discarding indentation or repeated spaces."""
    rows = []
    for line in clean(str(text)).expandtabs(4).split("\n"):
        piece, used = "", 0
        for char in line:
            size = display_width(char)
            if used + size > max(1, width) and piece:
                rows.append(piece)
                piece, used = "", 0
            piece += char
            used += size
        rows.append(piece)
    return rows


def partial_arguments(raw: str) -> dict[str, str]:
    """Decode completed or in-progress JSON string values, including split escapes.

    The trailing incomplete escape is held until its next delta arrives.
    This is display-only: execution always uses the model's final valid JSON.
    """
    values = {}
    pattern = r'"((?:[^"\\]|\\.)*)"\s*:\s*"((?:[^"\\]|\\.)*)'
    for match in re.finditer(pattern, raw):
        value = match[2]
        while value:
            try:
                values[json.loads('"' + match[1] + '"')] = json.loads('"' + value + '"')
                break
            except ValueError:
                value = value[:-1]
        else:
            values[match[1]] = ""
    return values


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
    'TOOL_COLOURS',
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
