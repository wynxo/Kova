"""What Kova's terminal chat prints: the step trace of each turn.

A turn is drawn as a *trace* (docs/TERMINAL-DESIGN.md): the speaker's tile,
then one node per step down the left edge, its shape and colour saying what
kind of step it was, its time at the right edge::

    K Kova
    ∴ thought   The user wants the tests run. First I should…        1.0s
    ≡ plan      1 of 3 done   Run the test suite
    ├┬ 2 reads at once                                               0.1s
    │├ list     .                          6 entries                 0.0s
    │└ read     pyproject.toml             4 lines                   0.0s
    ▸ run       ./run-tests -q             exit 1                    0.4s
    │           2 failed, 46 passed in 0.41s

The answer's prose sits beside the trace, indented, with no spine, so it copies
cleanly. Everything is written through ``out``: straight to the terminal or,
in the full-screen UI, to a ``Screen`` that keeps the live region below it.
What is still running is never written; ``live_lines`` offers it to the live
region instead, where its node turns in the accent colour.

Event protocol: ``text``, ``reasoning``, ``tool_call``, ``tool_call_delta``,
``tool_result``, ``task_start``/``task_progress``/``task_done``/``task_error``
(parallel tools and sub-agents), ``plan``, ``review``, ``privacy``, ``steer``,
``compacted``, ``error``, ``stopped`` and ``done``.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import sys
import time
import zlib

from kova import theme
from kova.privacy import status_text as privacy_status_text
from kova.usage import footer as usage_footer

from .glyphs import glyphs
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
from .style import CODE, FAINT, MUTED, THINK, bg, mix

# Each tool's step word: a short verb, lower case, at most nine columns.
TOOL_NAMES = {"run_command": "run", "web_search": "search", "web_fetch": "fetch",
              "read_file": "read", "write_file": "write", "edit_file": "edit",
              "list_files": "list", "search_files": "find", "read_attachment": "read",
              "memory_save": "remember", "memory_search": "recall", "memory_list": "recall",
              "recall": "recall", "search_chats": "search", "reminder_add": "remind",
              "reminder_list": "reminders", "system_info": "system", "job_add": "schedule",
              "ask_user": "ask", "delegate_analysis": "delegate", "update_plan": "plan"}
# Tools that only read. Everything else (writes, commands, other servers' tools,
# whose effects Kova cannot know) is drawn as a step that can change things.
LOOK_TOOLS = frozenset({"read_file", "list_files", "search_files", "read_attachment",
                        "web_search", "web_fetch", "memory_search", "memory_list", "recall",
                        "search_chats", "reminder_list", "system_info"})
# What a running tool is doing, for the live node.
ACTIVE_TITLES = {
    "run_command": "running", "web_search": "searching", "web_fetch": "fetching",
    "read_file": "reading", "write_file": "writing", "edit_file": "editing",
    "list_files": "listing", "search_files": "finding", "memory_save": "saving",
    "memory_search": "recalling", "reminder_add": "setting", "reminder_list": "listing",
    "system_info": "checking",
}
PERMISSION_TITLES = {"run_command": "run a command", "write_file": "write a file",
                     "edit_file": "edit a file", "delegate_analysis": "hand work to another model"}
WORD = 10           # the step word and its gap: arguments start at column WORD + 2
TIME = 6            # the time column at the right edge
OUTPUT_PREVIEW_LINES = 3
THINK_PREVIEW_ROWS = 2
DRAFT_ROWS = 8
# A fixed ring of roles for speakers other than Kova (bots, group chats): a
# name always lands on the same colour.
IDENTITY_RING = (theme.MEMORY, theme.THINK, theme.CODE, theme.USER, theme.SUCCESS, theme.BASH)


def identity(name: str, *, own: bool = False) -> str:
    """A speaker's colour (a ``kova.theme`` role): the accent for Kova itself."""
    if own:
        return theme.ACCENT
    return IDENTITY_RING[zlib.crc32(str(name).strip().lower().encode()) % len(IDENTITY_RING)]


def step_kind(name: str) -> str:
    """``look`` for a tool that only reads, ``change`` for one that may not."""
    return "look" if name in LOOK_TOOLS else "change"


KIND_COLOURS = {"think": THINK, "look": MEMORY, "change": CODE, "ask": WARNING,
                "plan": MUTED, "group": MUTED, "failed": ERROR, "stopped": WARNING,
                "steer": USER, "note": FAINT}


def step_word(name: str) -> str:
    word = TOOL_NAMES.get(name)
    if word:
        return word
    tail = re.split(r"[.:/]", str(name or "tool"))[-1]
    return tail.replace("_", " ")[:WORD - 1] or "tool"


def tool_detail(name: str, args: dict | None) -> str:
    """The one argument that says what a tool is working on."""
    args = args if isinstance(args, dict) else {}
    if name == "run_command":
        return " ".join(str(args.get("command", "")).split())
    if name in ("web_search", "memory_search", "search_files", "search_chats", "recall"):
        return str(args.get("query", args.get("pattern", "")))
    if name == "web_fetch":
        return str(args.get("url", ""))
    if name in ("read_file", "write_file", "edit_file", "list_files"):
        return str(args.get("path", ""))
    if name == "memory_save":
        return str(args.get("title", ""))
    if name == "reminder_add":
        return str(args.get("text", ""))
    if name == "ask_user":
        return str(args.get("question", ""))
    if name == "delegate_analysis":
        return str(args.get("task", ""))
    return ""


def args_preview(args: object, limit: int = 60) -> str:
    """One-line preview of tool arguments for a tool without a known key argument."""
    if args is None:
        return ""
    if isinstance(args, dict):
        if len(args) == 1:
            value = next(iter(args.values()))
            preview = f'"{value}"' if isinstance(value, str) else str(value)
        else:
            preview = "  ".join(f"{key}={value}" for key, value in args.items())
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
    tail = glyphs().ellipsis
    out, size = "", 0
    limit = max(1, width - display_width(tail))
    for char in text:
        step = display_width(char)
        if size + step > limit:
            break
        out += char
        size += step
    return out + tail


def first_sentence(text: str) -> str:
    """The start of some reasoning, for its collapsed one-line preview."""
    text = " ".join(clean(str(text)).split())
    match = re.search(r"(?<=[.!?])\s", text)
    return text[:match.start()] if match else text


def diff_counts(change: dict | None) -> tuple[int, int] | None:
    """Lines added and removed in a ``file_change`` preview."""
    if not isinstance(change, dict):
        return None
    added, removed = int(change.get("additions") or 0), int(change.get("deletions") or 0)
    diff = str(change.get("diff") or "")
    if not (added or removed) and diff:
        for line in diff.splitlines():
            if line.startswith("+") and not line.startswith("+++"):
                added += 1
            elif line.startswith("-") and not line.startswith("---"):
                removed += 1
    return added, removed


def result_summary(name: str, output: str, ok: bool, change: dict | None = None) -> str:
    """A tool's result in a few words: ``6 entries``, ``42 lines``, ``exit 1``."""
    counts = diff_counts(change)
    minus = glyphs().minus
    if counts and any(counts):
        return f"+{counts[0]} {minus}{counts[1]}"
    text = clean(str(output or "")).strip()
    try:
        data = json.loads(text) if text[:1] in "[{" else None
    except ValueError:
        data = None
    if isinstance(data, list):
        return f"{len(data)} result{'' if len(data) == 1 else 's'}"
    if isinstance(data, dict):
        if name == "run_command" or "exit_code" in data:
            code = data.get("exit_code", data.get("returncode"))
            if code is None and data.get("ok") is False:
                return "did not finish"
            return "exit 0" if code in (0, None) else f"exit {code}"
        if data.get("error"):
            return shorten(str(data["error"]), 60)
        for key, noun in (("entries", "entr"), ("matches", "match"), ("results", "result")):
            if isinstance(data.get(key), list):
                count = len(data[key])
                word = {"entr": "entry" if count == 1 else "entries",
                        "match": "match" if count == 1 else "matches",
                        "result": "result" if count == 1 else "results"}[noun]
                return f"{count} {word}" + ("+" if data.get("truncated") else "")
        if "total_lines" in data:
            count = data["total_lines"]
            return f"{count} line{'' if count == 1 else 's'}"
        if isinstance(data.get("content"), str):
            count = len(data["content"].splitlines())
            return f"{count} line{'' if count == 1 else 's'}"
        if data.get("created"):
            return "created"
        return "done" if data.get("ok", ok) else "failed"
    if name == "run_command":   # a command of the user's own (``!``): plain output
        code = re.search(r"exit code (-?\d+)\s*$", text)
        return f"exit {code[1]}" if code else ("exit 0" if ok else "failed")
    if not text:
        return "no output" if ok else "failed"
    if text.startswith("Error:"):
        return shorten(text[6:].strip(), 60)
    lines = text.splitlines()
    return f"{len(lines)} lines" if len(lines) > 1 else shorten(text, 48)


class Step:
    """One tool call in this turn, from its call to its result."""

    def __init__(self, call_id: str, name: str, args: dict, parent: str | None = None):
        self.id, self.name, self.args, self.parent = call_id, name, args, parent
        self.started = time.monotonic()
        self.ended: float | None = None
        self.ok: bool | None = None
        self.output = ""
        self.summary = ""
        self.change: dict | None = None
        self.status = "running" if parent is None else "queued"
        self.elapsed: float | None = None
        self.reported = False     # its tool_result (with the full output) has arrived

    @property
    def kind(self) -> str:
        return step_kind(self.name)


class Group:
    """Tools run at once (``task_start`` with no parent): parallel reads or sub-agents."""

    def __init__(self, group_id: str, label: str, kind: str, model: str, total: int):
        self.id, self.label, self.kind, self.model, self.total = group_id, label, kind, model, total
        self.children: list[str] = []
        self.started = time.monotonic()
        self.elapsed: float | None = None
        self.printed = False


class Renderer:
    """Writes a turn's trace to ``out`` as its events arrive; what is still
    running is offered to the live region by ``live_lines`` instead."""

    def __init__(self, out=None, *, color: bool | int | None = None, verbose: bool = False):
        self.out = out or sys.stdout
        self.tty = bool(getattr(self.out, "isatty", lambda: False)())
        if color is None:
            color = self.tty and not os.environ.get("NO_COLOR") and os.environ.get("TERM") != "dumb"
        self.p = Palette(color)
        self.g = glyphs()
        self.name = "Kova"
        self.model = ""
        self.verbose = verbose
        self.tools_expanded = False
        self.reasoning_text = ""
        self.tool_drafts: dict = {}
        self.tool_history: list = []
        self.detail_offset = 0
        self.stream: MarkdownStream | None = None
        self.thinking: MarkdownStream | None = None
        self.steps: dict[str, Step] = {}
        self.groups: dict[str, Group] = {}
        self.spinner = ""
        self.activity = ""        # what the live node says while something runs
        self.verb = "working"
        self.turn_started = 0.0
        self.think_started = 0.0
        self.text_started = 0.0
        self.waiting_since = 0.0  # when the model was last asked for more
        self.pending_tool: tuple[str, dict] | None = None
        self.stopping = False
        self.asking = False
        self.pass_chars = 0       # the pass streaming now, estimated from its text
        self.no_think = False     # hide reasoning entirely (--no-think)
        self.in_turn = False      # between begin_turn and finish_turn
        self.headed = False       # this turn's speaker line is out
        self.stopped = False      # the turn ended with a "stopped" event
        self.after_block = False  # a trace node was the last thing shown
        self.after_prose = False  # prose was the last thing shown
        self.shown_plan: list[str] = []   # the plan items last printed in full
        self.shown_status: list = []      # and their statuses the last time they were shown
        self.turn_usage = None
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

    @property
    def tokens(self) -> int:
        return self.pass_chars // 4

    # ------------------------------------------------------------ trace rows
    def node(self, kind: str, *, glyph: str | None = None) -> str:
        """A step's node: its kind's glyph in its kind's colour."""
        return self.p.paint("1;" + KIND_COLOURS.get(kind, MUTED), glyph or self.g[kind])

    def word(self, kind: str, text: str, nested: int = 0) -> str:
        """The step word, padded so arguments start in one column (``nested``
        is how far a tree's branches already pushed it right)."""
        return self.p.paint(KIND_COLOURS.get(kind, MUTED), text.ljust(WORD - 1 - nested)) + " "

    def spine(self) -> str:
        return self.p.faint(self.g.spine)

    def row(self, lead: str, middle: str = "", result: str = "", when: str = "", *,
            result_paint=None, middle_paint=None) -> str:
        """One trace row: ``lead`` (node and word), what it worked on, what came
        back, and the time pinned to the right edge. The middle gives way first,
        then the result; the node, word and time never wrap."""
        p, width = self.p, self.width
        used = display_width(strip_ansi(lead))
        right = p.faint(when.rjust(TIME)) if when else ""
        room = width - used - (TIME + 1 if when else 0)
        middle, result = clean(middle), clean(result)
        if result:
            # Results line up in one column: the argument gets a fixed share of
            # the room and the result the rest. When there is too little for
            # both, the argument goes and the result keeps what is left.
            column = min(40, max(0, room * 11 // 20))
            if not middle or column < 8:
                middle, column = "", 0
            middle = shorten(middle, column - 2) if middle else ""
            middle = middle.ljust(max(0, column - 2))
            result = shorten(result, max(1, room - column - (2 if column else 0)))
        else:
            middle = shorten(middle, max(1, room)) if middle else ""
        body = (middle_paint or (lambda text: text))(middle) if middle else ""
        gap_used = display_width(middle)
        if result:
            body += ("  " if middle else "") + (result_paint or p.dim)(result)
            gap_used += display_width(result) + (2 if middle else 0)
        if not when:
            return (lead + body).rstrip()
        pad = max(1, width - used - gap_used - TIME)
        return lead + body + " " * pad + right

    def continuation(self, text: str, paint=None, *, nested: str = "") -> str:
        """A line under a node, on the spine, aligned with the step's argument."""
        indent = " " * (WORD + 1 - display_width(nested))
        return self.spine() + nested + indent + (paint or self.p.dim)(text)

    def _trace(self, text: str) -> None:
        """Write a trace row, after the speaker line and apart from prose."""
        self.reply_header()
        if self.after_prose:
            self.line()
            self.after_prose = False
        self.line(text)
        self.after_block = True

    # ----------------------------------------------------------- live region
    def status(self, text: str) -> None:
        self.spinner = str(text or "").rstrip("…. ") or "working"

    def live_node(self, frame: int) -> str:
        if self.stopping:
            return self.node("stopped")
        if self.asking:
            return self.node("ask")
        return self.p.paint("1;" + ACCENT, self.g.spin(frame))

    def live_lines(self, frame: int, now: float | None = None) -> list[str]:
        """What runs now: one live node (or a tree of them) in the accent colour."""
        if not self.spinner:
            return []
        now = time.monotonic() if now is None else now
        p = self.p
        node = self.live_node(frame)
        rows: list[str] = []
        running_groups = [group for group in self.groups.values() if not group.printed]
        for group in running_groups:
            rows += self._group_rows(group, live=True, frame=frame, now=now)
        loose = [step for step in self.steps.values()
                 if step.ok is None and step.parent is None and step.name != "update_plan"]
        if self.stopping:
            rows.append(self.row(node + " " + self.word("stopped", "stopping"),
                                 "waiting for the current step to end",
                                 when=duration(now - (self.turn_started or now))))
            return rows
        if self.asking:
            return rows   # the question itself is on screen, below
        for step in loose:
            rows.append(self.row(node + " " + self.word(step.kind, step_word(step.name)),
                                 tool_detail(step.name, step.args) or args_preview(step.args, 60),
                                 ACTIVE_TITLES.get(step.name, "running"),
                                 duration(now - step.started), result_paint=p.faint))
        if self.pending_tool and not self.steps:
            # A command of the user's own (``!``), run outside the event stream.
            name, args = self.pending_tool
            rows.append(self.row(node + " " + self.word(step_kind(name), step_word(name)),
                                 tool_detail(name, args) or args_preview(args, 60), "running",
                                 duration(now - (self.turn_started or now)), result_paint=p.faint))
        if rows:
            return rows
        if self.think_started and not self.stream and not self.no_think:
            tail = " ".join(clean(self.reasoning_text).split())
            room = max(10, self.width - WORD - TIME - 4)
            tail = tail[-room:] if display_width(tail) > room else tail
            rows.append(self.row(node + " " + self.word("think", "thinking"), tail,
                                 when=duration(now - self.think_started),
                                 middle_paint=lambda text: p.paint(f"3;{FAINT}" if p.level >= 2
                                                                   else "2;3", text)))
        elif self.stream is not None:
            rows.append(self.row(node + " " + self.word("note", "writing"),
                                 f"{compact_number(self.tokens)} tokens" if self.tokens else "",
                                 when=duration(now - (self.text_started or now)),
                                 middle_paint=p.faint))
        else:
            since = self.waiting_since or self.turn_started or now
            label = self.activity or (f"for {self.model}" if self.model else "for the model")
            rows.append(self.row(node + " " + self.word("note", "waiting"), label,
                                 when=duration(now - since), middle_paint=p.faint))
        return rows

    def _on_tool_call_delta(self, event) -> None:
        # Snapshots are keyed by model call index; they never execute a tool.
        self.end_text()
        self.tool_drafts[event.get("index", 0)] = event

    def detail_lines(self) -> list[str]:
        """Replaceable live details, never appended token-by-token to scrollback:
        the full reasoning (``ctrl+t``), arguments being generated, and every
        step's arguments and output (``ctrl+o``)."""
        from pathlib import Path

        from .markdown import _highlighter
        p, g = self.p, self.g
        rows = []
        if self.reasoning_text and not self.no_think and self.verbose and self.think_started:
            rows.append(self.node("think") + " " + self.word("think", "thinking")
                        + p.faint("full reasoning, ctrl+t folds it"))
            rows.extend(self.continuation(row, self._think_paint) for row in
                        wrap_words(self.reasoning_text, self.width - WORD - 2))
        for draft in self.tool_drafts.values():
            name = str(draft.get("name") or "tool")
            raw = str(draft.get("arguments") or "")
            args = partial_arguments(raw)
            filename = args.get("path", args.get("filename", ""))
            rows.append(self.row(self.p.paint("1;" + ACCENT, g.spin(int(time.monotonic() * 8)))
                                 + " " + self.word(step_kind(name), step_word(name)),
                                 filename, "writing its arguments", result_paint=p.faint))
            code = next((args[k] for k in ("command", "content", "new_text", "new_string",
                                           "replacement") if k in args), raw)
            language = "bash" if name == "run_command" else Path(filename).suffix.lstrip(".") or "text"
            highlight = _highlighter(language, p.level) if p.color else None
            pieces = [piece for row in code.split("\n")
                      for piece in wrap_literal(row, self.width - WORD - 2)]
            for piece in pieces[-DRAFT_ROWS:]:
                rows.append(self.continuation(piece, highlight or (lambda text: text)))
        if self.tools_expanded:
            for step in self.steps.values():
                if step.ok is None:
                    rows.append(self.row(self.node(step.kind) + " " + self.word(step.kind, step_word(step.name)),
                                         "running", result_paint=p.faint))
                    rows.extend(self.continuation(row) for row in
                                wrap_literal(json.dumps(step.args, ensure_ascii=False, indent=2),
                                             self.width - WORD - 2))
            for name, args, ok, output in self.tool_history:
                kind = step_kind(name) if ok else "failed"
                rows.append(self.node(kind) + " " + self.word(kind, step_word(name))
                            + p.dim(shorten(tool_detail(name, args), self.width - WORD - 2)))
                for value in (json.dumps(args, ensure_ascii=False, indent=2), output):
                    rows.extend(self.continuation(row) for row in
                                wrap_literal(value, self.width - WORD - 2))
        if self.detail_offset:
            rows = rows[:-self.detail_offset] if self.detail_offset < len(rows) else rows[:1]
        return rows

    def _think_paint(self, text: str) -> str:
        return self.p.paint(f"3;{MUTED}" if self.p.level >= 2 else "2;3", text)

    # ------------------------------------------------------------ prompts
    def user(self, text: str, *, mark: str | None = None, colour: str = USER,
             tint: str = theme.USER) -> None:
        """Echo a submitted prompt: the speaker's bar and the text on a faint tint
        of ``tint``, so the user's turns stand apart from Kova's at a glance."""
        p = self.p
        mark = self.g.user if mark is None else mark
        rows: list[str] = []
        for paragraph in clean(str(text)).splitlines() or [""]:
            rows += wrap_words(paragraph, self.width - 4) if paragraph.strip() else [""]
        if len(rows) > 40:  # a pasted wall of text: show its start and end
            rows = [*rows[:18], f"{self.g.ellipsis} {len(rows) - 36} more lines", *rows[-18:]]
        size = max(display_width(row) for row in rows) + 3
        surface = p.terminal.roles["SURFACE"]
        shade = bg(mix(p.hex(tint), surface, 0.86)) if p.level >= 2 else ""
        for index, row in enumerate(rows):
            lead = mark if index == 0 or mark == self.g.user else " "
            if not p.color:
                self.line(f"{lead} {row}".rstrip())
                continue
            pad = " " * max(0, size - display_width(row) - 2)
            if shade:
                self.line(p.paint_mixed("1;" + colour, shade, lead + " ")
                          + p.paint_resolved(shade, row + pad))
            else:
                self.line(p.paint("1;" + colour, lead + " ") + row)
        self.line()

    def shell(self, command: str) -> None:
        self.user(command, mark="!", colour=BASH, tint=theme.BASH)

    def remember(self, note: str) -> None:
        self.user(note, mark="#", colour=MEMORY, tint=theme.MEMORY)

    def reminder(self, text: str) -> None:
        """A reminder that popped in while chatting."""
        self.line(self.row(self.node("ask", glyph=self.g.ask) + " " + self.word("ask", "reminder"), text))
        self.line()

    def notice(self, word: str, text: str, kind: str = "note") -> None:
        """A one-line notice in the trace: an answered approval, a saved note."""
        self._trace(self.row(self.node(kind) + " " + self.word(kind, word), text))

    def error(self, text: str) -> None:
        """A failure on the trace, wrapped rather than cut off, with room after it."""
        p = self.p
        rows = wrap_words(text, self.width - WORD - 2)
        self.line(self.node("failed") + " " + self.word("failed", "error") + p.red(rows[0]))
        for row in rows[1:]:
            self.line(self.continuation(row, p.red))
        self.line()
        self.after_block = False
        self.after_prose = False

    # ------------------------------------------------------------ events
    def begin_turn(self) -> None:
        self.reasoning_text = ""
        self.tool_drafts.clear()
        self.tool_history.clear()
        self.steps.clear()
        self.groups.clear()
        self.detail_offset = 0
        self.turn_started = time.monotonic()
        self.waiting_since = self.turn_started
        self.think_started = 0.0
        self.text_started = 0.0
        self.stopping = False
        self.activity = ""
        self.pass_chars = 0
        self.turn_usage = None
        self.in_turn, self.headed, self.stopped = True, False, False
        self.after_block = self.after_prose = False
        self.status("working")

    def speaker(self, name: str, *, own: bool = True) -> str:
        """A speaker's line: their tile (an initial on their colour) and name."""
        p = self.p
        initial = (str(name).strip()[:1] or "?").upper()
        if not p.color:
            return f"{initial} {name}"
        tile = p.pill(initial, identity(name, own=own), solid=True)
        return tile + " " + p.bold(name)

    def reply_header(self) -> None:
        """The turn's speaker line, once, before the first thing it shows."""
        if self.in_turn and not self.headed:
            self.headed = True
            self.line(self.speaker(self.name))

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
            self.reasoning_text = ""
            self.status("thinking")
        self.reasoning_text += text

    def _end_thinking(self) -> None:
        if self.think_started and not self.no_think and self.reasoning_text.strip():
            took = duration(time.monotonic() - self.think_started)
            lead = self.node("think") + " " + self.word("think", "thought")
            # Scrollback keeps the first sentence; ctrl+t shows the rest live.
            self._trace(self.row(lead, first_sentence(self.reasoning_text), when=took,
                                 middle_paint=self._think_paint))
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
            self.text_started = time.monotonic()
            self.stream = MarkdownStream(self.write, self.width, self.p, margin="  ")
        self.stream.feed(text)

    def end_text(self) -> None:
        if self.stream is not None:
            self.stream.finish()
            self.stream = None
            self.after_prose = True
            self.waiting_since = time.monotonic()
        if self.think_started:
            self._end_thinking()

    def _on_compacted(self, event) -> None:
        self.end_text()
        count = int(event.get("count", 0) or 0)
        self.notice("compacted", f"{count} earlier message{'' if count == 1 else 's'} into a summary")
        for row in wrap_words(str(event.get("summary", "")), self.width - WORD - 2):
            self.line(self.continuation(row))

    def _on_compact_skipped(self, event) -> None:
        self.notice("compact", str(event.get("message", "Nothing to compact.")))

    def _on_tool_call(self, event) -> None:
        self.end_text()
        name = str(event.get("name", ""))
        call_id = str(event.get("id") or f"call-{len(self.steps)}")
        self.tool_drafts = {i: draft for i, draft in self.tool_drafts.items()
                            if draft.get("id") != event.get("id") and draft.get("name") != name}
        args = event.get("args") if isinstance(event.get("args"), dict) else {}
        existing = self.steps.get(call_id)
        if existing is not None:   # announced by its group's task_start
            existing.args = args or existing.args
        else:
            self.steps[call_id] = Step(call_id, name, args)
        self.pending_tool = (name, args)
        self.activity = ACTIVE_TITLES.get(name, "")

    def _find_step(self, event) -> Step | None:
        call_id = str(event.get("id") or "")
        if call_id in self.steps:
            return self.steps[call_id]
        name = str(event.get("name", ""))
        return next((step for step in reversed(self.steps.values())
                     if step.name == name and step.ok is None), None)

    def _on_tool_result(self, event) -> None:
        name = str(event.get("name", ""))
        step = self._find_step(event)
        if step is None:
            step = Step(str(event.get("id") or ""), name, {})
        ok = bool(event.get("ok", True))
        output = str(event.get("output", event.get("summary") or ""))
        step.ok, step.output, step.reported = ok, output, True
        step.summary = str(event.get("summary") or "")
        step.change = event.get("file_change") if isinstance(event.get("file_change"), dict) else None
        if step.ended is None:
            step.ended = time.monotonic()
        if step.elapsed is None:
            step.elapsed = step.ended - step.started
        self.pending_tool = None
        self.activity = ""
        self.waiting_since = time.monotonic()
        self.tool_history.append((name, step.args, ok, output))
        group = self.groups.get(step.parent or "")
        if group is not None:
            # Drawn with its group, once every call in it has its full output.
            if group.elapsed is not None and all(
                    self.steps[child].reported for child in group.children if child in self.steps):
                self._print_group(group)
            return
        if name == "update_plan" and ok:
            return  # the plan event draws itself
        self.tool_row(name, step.args, ok, output, change=step.change, elapsed=step.elapsed)

    # ---------------------------------------------------- parallel and sub-agents
    def _on_task_start(self, event) -> None:
        self.end_text()
        task_id, parent = str(event.get("id", "")), event.get("parent")
        if parent is None:
            self.groups[task_id] = Group(task_id, str(event.get("label") or "steps at once"),
                                         str(event.get("kind") or "parallel"),
                                         str(event.get("model") or ""), int(event.get("total") or 0))
            return
        group = self.groups.get(str(parent))
        name = str(event.get("name") or event.get("label") or "tool")
        args = event.get("args") if isinstance(event.get("args"), dict) else {}
        self.steps[task_id] = Step(task_id, name, args, parent=str(parent))
        if group is not None:
            group.children.append(task_id)

    def _on_task_progress(self, event) -> None:
        step = self.steps.get(str(event.get("id", "")))
        if step is not None and event.get("status") == "running":
            step.status, step.started = "running", time.monotonic()

    def _task_end(self, event, ok: bool) -> None:
        task_id = str(event.get("id", ""))
        group = self.groups.get(task_id)
        if group is not None:
            group.elapsed = (event.get("elapsed_ms") or 0) / 1000 or (time.monotonic() - group.started)
            return
        step = self.steps.get(task_id)
        if step is None:
            return
        step.ok = ok if step.ok is None else step.ok
        step.status = "stopped" if event.get("stopped") else ("ok" if ok else "failed")
        step.summary = str(event.get("summary") or event.get("message") or "")
        step.output = step.output or step.summary
        if event.get("elapsed_ms") is not None:
            step.elapsed = float(event["elapsed_ms"]) / 1000
        step.ended = time.monotonic()

    def _on_task_done(self, event) -> None:
        self._task_end(event, bool(event.get("ok", True)))

    def _on_task_error(self, event) -> None:
        self._task_end(event, False)

    def _group_title(self, group: Group) -> str:
        count = len(group.children) or group.total
        if group.kind == "delegated":
            return f"{count} sub-agent{'' if count == 1 else 's'}" + (f" on {group.model}" if group.model else "")
        kinds = {self.steps[child].kind for child in group.children if child in self.steps}
        noun = "reads" if kinds == {"look"} else "steps"
        return f"{count} {noun} at once"

    def _group_rows(self, group: Group, *, live: bool, frame: int = 0,
                    now: float | None = None) -> list[str]:
        p, g = self.p, self.g
        now = time.monotonic() if now is None else now
        head_glyph = self.live_node(frame) if live else self.node("group", glyph=g.branch)
        elapsed = (now - group.started) if live else (group.elapsed or 0.0)
        rows = [self.row(head_glyph + p.faint(g.fork) + " " + p.bold(self._group_title(group)),
                         when=duration(elapsed))]
        for index, child in enumerate(group.children):
            step = self.steps.get(child)
            if step is None:
                continue
            last = index == len(group.children) - 1
            branch = p.faint(g.spine + (g.last if last else g.branch))
            if step.ok is None and live:
                status = "waiting" if step.status == "queued" else "running"
                node = p.paint("1;" + ACCENT, g.spin(frame)) if step.status == "running" else p.faint(g.todo)
                rows.append(self.row(branch + node + " " + self.word(step.kind, step_word(step.name), 2),
                                     tool_detail(step.name, step.args) or args_preview(step.args, 50),
                                     status, duration(now - step.started) if status == "running" else "",
                                     result_paint=p.faint))
                continue
            failed = step.ok is False
            kind = "stopped" if step.status == "stopped" else "failed" if failed else step.kind
            result = "stopped" if step.status == "stopped" else result_summary(
                step.name, step.output or step.summary, not failed, step.change)
            rows.append(self.row(branch + self.node(kind) + " " + self.word(kind, step_word(step.name), 2),
                                 tool_detail(step.name, step.args) or args_preview(step.args, 50),
                                 result, duration(step.elapsed) if step.elapsed is not None else "",
                                 result_paint=self._result_paint(not failed)))
        return rows

    def _print_group(self, group: Group) -> None:
        if group.printed:
            return
        group.printed = True
        rows = self._group_rows(group, live=False)
        self._trace(rows[0])
        for row in rows[1:]:
            self.line(row)
        self.waiting_since = time.monotonic()

    # ------------------------------------------------------------ the rest
    def _on_done(self, event) -> None:
        """The server's own token, cost and speed numbers for this answer (#75)."""
        usage = event.get("usage")
        self.turn_usage = usage if isinstance(usage, dict) else None

    def _on_privacy(self, event) -> None:
        """A safe count of what the outbound guard changed, never the values."""
        self.end_text()
        self.notice("privacy", privacy_status_text(event))

    def _on_error(self, event) -> None:
        self.end_text()
        self.reply_header()
        if self.after_prose:
            self.line()
        self.error(event.get("message", "The request failed"))

    def _on_stopped(self, _event) -> None:
        self.end_text()
        # A group still open was cut short: draw its tree first, so the stop
        # reads as the end of it rather than appearing above it.
        for group in self.groups.values():
            self._print_group(group)
        self.interrupted()

    def interrupted(self) -> None:
        if not self.stopped:
            self.stopped = True
            self._trace(self.row(self.node("stopped") + " " + self.word("stopped", "stopped"),
                                 "You stopped this turn. Send a message to carry on."))

    def _on_steer(self, event) -> None:
        """A message the user slipped into the reply while it was working."""
        self.end_text()
        self._trace(self.row(self.node("steer") + " " + self.word("steer", "you"),
                             str(event.get("text", "")), middle_paint=lambda text: text))

    def _on_plan(self, event) -> None:
        self.end_text()
        self.plan(event.get("items") or [])

    def _on_review(self, event) -> None:
        """A turn that changed files: one row, reviewable with /changes."""
        self.end_text()
        files = event.get("files") or []
        if not files:
            skipped = event.get("skipped") or []
            if skipped:
                self.notice("skipped", f"{len(skipped)} file{'' if len(skipped) == 1 else 's'} "
                                       "too large to checkpoint")
            return
        p = self.p
        additions = sum(int(entry.get("additions") or 0) for entry in files)
        deletions = sum(int(entry.get("deletions") or 0) for entry in files)
        counts = p.green(f"+{additions}") + " " + p.red(f"{self.g.minus}{deletions}")
        label = f"{len(files)} file{'' if len(files) == 1 else 's'}"
        self._trace(self.node("change") + " " + self.word("change", "changed") + label + "  "
                    + counts + p.faint("   /changes to review"))

    # ------------------------------------------------------------ tool rows
    def tool_header(self, name: str, args: dict) -> str:
        """A tool about to run: its word and what it is working on."""
        kind = step_kind(name)
        return self.row(self.node(kind) + " " + self.word(kind, step_word(name)),
                        tool_detail(name, args) or args_preview(args, 60))

    def tool_row(self, name, args, ok, summary, *, change: dict | None = None,
                 elapsed: float | None = None) -> None:
        """A finished tool: ``▸ run  ./run-tests -q  exit 1  0.4s``; commands keep
        the last lines of their output underneath, on the spine."""
        p = self.p
        args = args if isinstance(args, dict) else {}
        kind = step_kind(name) if ok else "failed"
        detail = tool_detail(name, args) or args_preview(args, 60)
        result = result_summary(name, summary, ok, change)
        when = duration(elapsed) if elapsed is not None else ""
        self._trace(self.row(self.node(kind) + " " + self.word(kind, step_word(name)), detail,
                             result, when, result_paint=self._result_paint(ok)))
        if name == "run_command":
            for row in self._run_command_body(summary, ok):
                self.line(row)

    def _result_paint(self, ok: bool):
        """Results in muted text; a diff's ``+3 −1`` in green and red; failures in red."""
        p, minus = self.p, self.g.minus

        def paint(text: str) -> str:
            match = re.fullmatch(rf"\+(\d+) {re.escape(minus)}(\d+)(\s*)", text)
            if match and ok:
                return p.green(f"+{match[1]}") + " " + p.red(f"{minus}{match[2]}") + match[3]
            return (p.dim if ok else p.red)(text)
        return paint

    def _run_command_body(self, summary: str, ok: bool) -> list[str]:
        """The last lines of a command's output, under its row."""
        p = self.p
        lines = [line.rstrip() for line in command_output(summary).splitlines()]
        # The exit status is already on the row; blank lines only cost height.
        lines = [line for line in lines if line.strip() and not re.fullmatch(
            r"exit code -?\d+|Command exited with (status|code) -?\d+\.?", line.strip())]
        if not lines:
            return []
        room = self.width - WORD - 2
        shown = lines[-OUTPUT_PREVIEW_LINES:]
        rows = []
        hidden = len(lines) - len(shown)
        if hidden:
            rows.append(self.continuation(f"{self.g.ellipsis} {hidden} earlier "
                                          f"line{'' if hidden == 1 else 's'}, ctrl+o shows all",
                                          p.faint))
        rows += [self.continuation(shorten(line, room), p.dim) for line in shown]
        return rows

    def plan(self, items) -> None:
        """The checklist: in full when it is new, then one row per update."""
        items = [item if isinstance(item, dict) else {"text": str(item)} for item in items or []]
        if not items:
            return
        p, g = self.p, self.g
        done = sum(1 for item in items if item.get("status") == "done")
        current = next((item for item in items if item.get("status") == "in_progress"), None)
        texts = [str(item.get("text") or "") for item in items]
        statuses = [item.get("status") for item in items]
        lead = self.node("plan") + " " + self.word("plan", "plan")
        summary = f"{done} of {len(items)} done"
        if texts == self.shown_plan:
            # An update to the same plan is one row: what just finished, or what is next.
            finished = [text for text, status, before in zip(texts, statuses, self.shown_status, strict=False)
                        if status == "done" and before != "done"]
            room = self.width - WORD - 2 - display_width(summary) - 5
            if finished:
                label = p.green(g.done) + " " + p.faint(shorten(finished[-1], room))
            elif current:
                label = p.bold(g.doing + " " + shorten(str(current.get("text")), room))
            else:
                label = ""
            self.shown_status = statuses
            self._trace(lead + p.dim(summary) + (("   " + label) if label else ""))
            return
        self.shown_status = statuses
        self.shown_plan = texts
        self._trace(lead + p.dim(summary))
        for item in items:
            status = item.get("status")
            title = shorten(str(item.get("text") or ""), self.width - WORD - 4)
            if status == "done":
                row = p.green(g.done) + " " + p.faint(title)
            elif status == "in_progress":
                row = p.bold(g.doing + " " + title)   # scrollback is history: no accent
            else:
                row = p.dim(g.todo) + " " + title
            self.line(self.spine() + " " * (WORD + 1) + row)

    # -------------------------------------------------------- permission
    def permission_card(self, name: str, args: dict, risk: str, summary: str,
                        where: str = "") -> tuple[str, list[str], str]:
        """Title, body lines and colour for the approval gate: what will run,
        where, and for edits the change itself."""
        p = self.p
        args = args if isinstance(args, dict) else {}
        action = PERMISSION_TITLES.get(name, f"use {step_word(name)}")
        title = f"{self.name} wants to {action}"
        room = self.width - 8
        body: list[str] = []
        if name == "run_command":
            for line in (clean(str(args.get("command", ""))).splitlines() or [""]):
                body += [p.code(row) for row in wrap_literal("$ " + line if not body else "  " + line, room)]
        elif name in ("edit_file", "write_file"):
            body.append(p.bold(clean(str(args.get("path", "")))))
            body += self._edit_preview(name, args, room)
        else:
            detail = tool_detail(name, args) or args_preview(args, room)
            body += [p.code(row) for row in wrap_words(detail, room)[:6]]
        cwd = clean(str(args.get("cwd") or where or ""))
        note = clean(str(summary or "")).strip()
        if name == "run_command":
            # The command is already above; keep only what the summary adds to it.
            command = clean(str(args.get("command", ""))).strip()
            first, _, rest = note.partition("\n")
            if first.strip() == command:
                note = rest.strip()
        if note and not (name in ("edit_file", "write_file") and note.startswith(name)):
            body += [p.dim(row) for row in wrap_words(note, room)[:3]]
        if cwd:
            body.append(p.dim("in " + cwd))
        if risk == "destructive":
            body.append(p.red("This can delete data or change the system for good."))
        colour = ERROR if risk == "destructive" else WARNING
        return title, body, colour

    def _edit_preview(self, name: str, args: dict, room: int, limit: int = 10) -> list[str]:
        """The lines an edit removes and adds, as a small diff."""
        p, minus = self.p, self.g.minus
        if name == "edit_file":
            old = clean(str(args.get("old_text", args.get("old_string", "")))).rstrip("\n").splitlines()
            new = clean(str(args.get("new_text", args.get("new_string", "")))).rstrip("\n").splitlines()
        else:
            old, new = [], clean(str(args.get("content", ""))).rstrip("\n").splitlines()
        def cut(line: str) -> str:   # keep indentation: it is part of the change
            line = line.expandtabs(4)
            return line if display_width(line) <= room - 2 else wrap_literal(line, room - 3)[0] + self.g.ellipsis
        rows = [p.red(minus + " " + cut(line)) for line in old] + \
               [p.green("+ " + cut(line)) for line in new]
        if len(rows) > limit:
            more = len(rows) - limit
            rows = rows[:limit] + [p.faint(f"{self.g.ellipsis} {more} more line{'' if more == 1 else 's'}")]
        return rows

    def approval_row(self, tool: str, args: dict, decision: str, message: str | None = None) -> None:
        """An answered approval, written into the trace where it happened."""
        p = self.p
        what = f"{step_word(tool)} {shorten(tool_detail(tool, args) or '', 60)}".strip()
        for step in self.steps.values():
            if step.ok is None and step.name == tool:
                step.started = time.monotonic()   # its time is the work, not the wait
        if decision == "deny":
            lead = self.node("failed", glyph=self.g.ask) + " " + self.word("failed", "declined")
            self._trace(self.row(lead, what, message or "", result_paint=p.dim,
                                 middle_paint=lambda text: text))
            return
        scope = {"allow": "once", "allow_session": "for this session",
                 "allow_always": "always"}.get(decision, "")
        lead = p.paint("1;" + SUCCESS, self.g.ask) + " " + p.paint(SUCCESS, "allowed".ljust(WORD - 1)) + " "
        self._trace(self.row(lead, what, scope, middle_paint=lambda text: text))

    # ------------------------------------------------------------ totals
    def finish_turn(self, *, stopped: bool = False) -> None:
        self.end_text()
        for group in list(self.groups.values()):
            self._print_group(group)
        if stopped:
            self.interrupted()
        elapsed = time.monotonic() - self.turn_started if self.turn_started else 0
        parts = [duration(elapsed)] if elapsed else []
        usage = getattr(self, "turn_usage", None)
        if usage:
            parts += usage_footer(usage).split(" · ")
        elif self.pass_chars:
            parts.append(f"{compact_number(self.tokens)} tokens")
        self.last_tokens = self.tokens if self.pass_chars else 0
        self.last_elapsed = elapsed
        if elapsed:
            self.last_stats = "  ".join(
                ([f"{compact_number(self.tokens)} tokens"] if self.pass_chars else [])
                + [duration(elapsed)])
        if parts and self.headed:
            if self.after_prose or self.after_block:
                self.line()
            self.line("  " + self.p.faint("   ".join(parts)))
        if self.headed:
            self.line()
        self.spinner = ""
        self.activity = ""
        self.pending_tool = None
        self.tool_drafts.clear()
        self.stopping = False
        self.in_turn = False
        self.after_block = self.after_prose = False


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
    'IDENTITY_RING',
    'KIND_COLOURS',
    'LOOK_TOOLS',
    'PERMISSION_TITLES',
    'TOOL_NAMES',
    'Group',
    'Renderer',
    'Step',
    'args_preview',
    'command_output',
    'compact_number',
    'diff_counts',
    'duration',
    'first_sentence',
    'identity',
    'result_summary',
    'shorten',
    'step_kind',
    'step_word',
    'tool_detail',
    'wrap_words',
]
