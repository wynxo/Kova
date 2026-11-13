"""Kova's full-screen terminal chat.

One loop on the main thread reads keys, drains the reply's events and
animates, then redraws the live region at the bottom of the terminal: the
spinner and running tool, queued messages, the input box, and under it the
suggestion menu, the shortcuts or the status line. Dialogs (approval prompts,
pickers) take the input box's place. The conversation scrolls above as
ordinary terminal output, so it stays in the terminal's scrollback.

Replies stream from the Kova server on a worker thread; ``Esc``/``Ctrl+C``
cancel them through ``POST /chats/{id}/stop``. Approval and plan events are
optional: a server that never sends them simply never shows the dialogs.

Ported and adapted from Kaine's ``kaine/cli_tui.py`` (the loop, keys, queue
and dialogs) and ``kaine/cli_commands.py`` (pickers and slash commands).
"""
from __future__ import annotations

import os
import json
import queue
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path

from kova import __version__, theme
from kova.client import KovaClient, KovaError
from kova.modes import MODES

from .editor import Editor, FileIndex, expand_mentions, load_history, rank, save_history, suggest
from .markdown import ACCENT, BASH, MEMORY
from .render import Renderer, compact_number, duration, shorten
from .style import ACCENT_SOFT, FAINT, colour_level
from .terminal import KeyReader, RawMode, Screen
from .widgets import (
    MODE_COLOURS,
    Choice,
    CommandPalette,
    Picker,
    box,
    fit,
    input_box,
    menu,
    pick_placeholder,
    reduced_motion,
    shortcuts,
    status_bar,
    welcome,
)

FRAME = 0.08            # animation step, seconds
WELCOME_SECONDS = 0.9   # how long the welcome card animates in
STOP_GRACE = 5.0        # how long a stopped reply may take to end before the UI lets it go
SHELL_TIMEOUT = 300     # seconds a ``!command`` may run
SHELL_CONTEXT_CHARS = 4000
REPLAY_MESSAGES = 20    # messages shown when a chat is resumed

PERMISSIONS = {"manual": "asks before every action",
               "auto": "asks only before destructive actions",
               "full": "never asks"}
PERMISSION_CYCLE = ("manual", "auto", "full")
MODE_DETAILS = {"chat": "conversation only, web search at most",
                "work": "everything: web, files, memory, reminders"}

# name -> (usage, description); every chat command, in /help order.
COMMANDS = {
    "help": ("/help", "Show commands and keyboard shortcuts"),
    "new": ("/new", "Start a fresh conversation"),
    "resume": ("/resume [n]", "Open an earlier conversation"),
    "mode": ("/mode [chat|work]", "Show the chat mode; /mode chat|work switches"),
    "permissions": ("/permissions [mode|rules]", "When Kova asks before acting: manual, auto, full"),
    "web": ("/web [on|off]", "Show web search state; /web on|off toggles it"),
    "model": ("/model [name]", "List models; /model <name> switches"),
    "cd": ("/cd <folder>", "Change the folder Kova works in"),
    "think": ("/think", "Toggle full reasoning (collapsed by default)"),
    "compact": ("/compact", "Summarize older conversation context"),
    "clear": ("/clear", "Clear the screen, keeping this conversation"),
    "exit": ("/exit", "Leave Kova (also /quit)"),
    "quit": ("/quit", "Leave Kova"),
}


class Job:
    """Work on a worker thread (a reply or a shell command) that reports back
    through a queue; ``None`` on the queue means it has ended."""

    def __init__(self, kind: str, work: Callable, label: str = ""):
        self.kind, self.label = kind, label
        self.events: queue.Queue = queue.Queue()
        self.cancel = threading.Event()
        self.cancelled_at = 0.0
        self.outcome: dict = {}
        self.started = time.monotonic()
        self.thread = threading.Thread(target=self._run, args=(work,), daemon=True,
                                       name=f"kova-chat-{kind}")
        self.thread.start()

    def _run(self, work) -> None:
        try:
            self.outcome["result"] = work(self.events.put, self.cancel)
        except Exception as exc:
            self.outcome["error"] = exc
        finally:
            self.events.put(None)


class TextPrompt:
    """A one-line question in place of the input box."""

    def __init__(self, prompt: str, on_value: Callable[[str | None], None], *,
                 placeholder: str = "", note: str = ""):
        self.prompt, self.on_value, self.value = prompt, on_value, ""
        self.placeholder, self.note = placeholder, note

    def render(self, width: int, p) -> list[str]:
        room = width - 10
        line = p.paint("1;" + ACCENT, "❯ ") + self.value[-room:] + p.paint(ACCENT, "▏")
        if not self.value and self.placeholder:
            line += p.paint(f"3;{FAINT}" if p.level >= 2 else "2;3", fit(self.placeholder, room - 1))
        footer = " · ".join(part for part in (self.note, "enter to send · esc to skip")
                                if part)
        return box(["", line, ""], width, p, ACCENT, title=p.bold(fit(self.prompt, width - 8)),
                   footer=p.faint(fit(footer, width - 10)))


def run_shell(command: str, cwd: str, cancel: threading.Event,
              timeout: float = SHELL_TIMEOUT) -> dict:
    """Run ``command`` in a shell; output and exit code. Cancel or timeout kills it."""
    try:
        process = subprocess.Popen(command, shell=True, cwd=cwd, stdin=subprocess.DEVNULL,
                                   stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                                   errors="replace", start_new_session=os.name == "posix")
    except OSError as exc:
        return {"ok": False, "output": str(exc), "code": None}
    deadline = time.monotonic() + timeout
    while True:
        try:
            output, _ = process.communicate(timeout=0.1)
            break
        except subprocess.TimeoutExpired:
            if cancel.is_set() or time.monotonic() > deadline:
                _kill(process)
                output, _ = process.communicate()
                note = "stopped" if cancel.is_set() else f"timed out after {timeout:.0f}s"
                return {"ok": False, "output": (output or "") + f"\n({note})", "code": None}
    return {"ok": process.returncode == 0, "output": output or "", "code": process.returncode}


def _kill(process: subprocess.Popen) -> None:
    try:
        if os.name == "posix":
            os.killpg(process.pid, signal.SIGTERM)
        else:
            process.terminate()
    except OSError:
        pass


def _home_short(path: str) -> str:
    home = str(Path.home())
    return "~" + path[len(home):] if path == home or path.startswith(home + os.sep) else path


class ChatApp:
    """The interactive chat in a terminal, bound to one Kova server and one chat."""

    def __init__(self, client: KovaClient, chat_id: str, *, no_think: bool = False,
                 out=None, color: bool | None = None, clear: bool = True,
                 history_path: Path | None = None, memory_path: str = ""):
        self.client = client
        self.chat_id = chat_id
        self.out = out or sys.stdout
        self.screen = Screen(self.out)
        if color is None:
            color = colour_level()
        self.renderer = Renderer(self.screen, color=color)
        self.renderer.no_think = no_think
        self.p = self.renderer.p
        self.history_path = history_path
        self.editor = Editor(load_history(history_path) if history_path else [])
        self.memory_path = memory_path
        self.clear_on_start = clear
        self.placeholder = pick_placeholder()
        # Chat settings, as the server reports them; ``permission`` stays None
        # when the server has no permission support.
        self.mode, self.web, self.permission = "work", True, None
        self.title = ""
        self.cwd = os.getcwd()
        self.model, self.model_ok = "", True
        self.notes: int | None = None
        self.plan: list[dict] = []
        self.files = FileIndex(self.cwd)
        # Rough context accounting for the status line: characters in the
        # conversation so far, and the active model's window in tokens.
        self.context_chars = 0
        self.context_window = 0
        # The live region's state.
        self.menu = None
        self.menu_index = 0
        self.menu_dismissed: str | None = None
        self.overlay = None            # Choice, Picker or TextPrompt
        self.approval: dict | None = None
        self.job: Job | None = None
        self.queued: list[str] = []
        self.context_notes: list[str] = []
        self.notices: queue.Queue = queue.Queue()   # reminders from the event stream
        self.show_shortcuts = False
        self.flash_text, self.flash_until = "", 0.0
        self.frame, self.last_frame = 0, 0.0
        self.welcome_started: float | None = None
        self.last_esc = self.last_ctrl_c = 0.0
        self.quit = False
        self.resized = False
        self.raw: RawMode | None = None
        self._models: list[dict] = []
        self._chats: list[dict] = []
        self._loading = False

    # ================================================================ lifecycle
    def run(self) -> int:
        fd = sys.stdin.fileno()
        reader = KeyReader(fd)
        previous = signal.signal(signal.SIGWINCH, lambda *_: setattr(self, "resized", True))
        self.raw = RawMode(fd, self.out)
        try:
            self.refresh_chat()
            self._refresh_health()
            try:
                self.notes = len(self.client.memory_list())
            except KovaError:
                pass
            self._load_lists_later()
            threading.Thread(target=self._watch_reminders, daemon=True,
                             name="kova-chat-reminders").start()
            with self.raw:
                if self.clear_on_start:
                    self.screen.clear()
                if reduced_motion():
                    self._commit_welcome()
                else:
                    self.welcome_started = time.monotonic()
                while not self.quit:
                    animating = self.job is not None or self.welcome_started is not None
                    for key in reader.read(FRAME / 2 if animating else 0.25):
                        self.on_key(key)
                        if self.quit:
                            break
                    self._pump()
                    self._animate()
                    self._draw()
        finally:
            signal.signal(signal.SIGWINCH, previous)
            self._shutdown()
        return 0

    def _shutdown(self) -> None:
        if self.job is not None:
            self._stop()
            self.job.thread.join(2)
        if self.history_path is not None:
            save_history(self.history_path, self.editor.history)
        if self.welcome_started is not None:
            self._commit_welcome()
        self.screen.close()
        self.out.write(self.p.paint("1;" + ACCENT, "◆ ") + self.p.dim("Until next time.") + "\n")
        self.out.flush()

    def _suspend(self) -> None:
        self.screen.close()
        if self.raw is not None:
            self.raw.__exit__(None, None, None)
        os.kill(os.getpid(), signal.SIGTSTP)
        if self.raw is not None:
            self.raw.__enter__()
        self.screen.dirty = True

    # ================================================================ server state
    def refresh_chat(self) -> None:
        chat = self.client.get_chat(self.chat_id) or {}
        self._read_chat(chat)
        cwd = chat.get("cwd")
        if cwd and os.path.isdir(cwd) and cwd != self.cwd:
            self.cwd = cwd
            self.files = FileIndex(cwd)

    def _read_chat(self, chat: dict) -> None:
        """Take the server's view of the chat and its rough token size."""
        self.title = str(chat.get("title") or "")
        self.mode = chat.get("mode") or "work"
        self.web = chat.get("web", True)
        self.permission = chat.get("permission")
        self.context_chars = sum(len(str(message.get("content") or ""))
                                 for message in chat.get("messages") or []
                                 if isinstance(message, dict))

    def _sync_context_window(self) -> None:
        """The active model's context window, from the server's model list."""
        if not self._models:
            return  # nothing loaded yet; keep what we have
        entry = next((model for model in self._models if model.get("name") == self.model), None)
        try:
            self.context_window = max(0, int((entry or {}).get("context") or 0))
        except (TypeError, ValueError):
            self.context_window = 0

    def _refresh_health(self) -> None:
        try:
            health = self.client.health()
        except KovaError:
            self.model_ok = False
            return
        self.model, self.model_ok = str(health.get("model", "")), bool(health.get("model_ok", True))
        self._sync_context_window()

    def _load_lists_later(self) -> None:
        """Models and chats for suggestions, fetched off the UI thread."""
        if self._loading:
            return
        self._loading = True

        def load():
            try:
                self._models = self.client.models_list()
                self._chats = self.client.list_chats()[:30]
                self._sync_context_window()
            except Exception:  # suggestions are a nicety; a hiccup must not break typing
                pass
            finally:
                self._loading = False
        threading.Thread(target=load, daemon=True, name="kova-chat-lists").start()

    def _refresh_title_later(self) -> None:
        """Pick up the chat's title and token size once the turn is stored."""
        client, chat_id = self.client, self.chat_id

        def load():
            try:
                chat = client.get_chat(chat_id) or {}
            except Exception:  # the title is a nicety; never let it break the chat
                return
            if chat_id == self.chat_id:
                if chat.get("title"):
                    self.title = str(chat["title"])
                self.context_chars = sum(
                    len(str(message.get("content") or ""))
                    for message in chat.get("messages") or [] if isinstance(message, dict))
                self.screen.dirty = True
        threading.Thread(target=load, daemon=True, name="kova-chat-title").start()

    def _watch_reminders(self) -> None:
        try:
            for event in self.client.events():
                if event.get("type") == "reminder":
                    self.notices.put(str(event.get("text", "")))
        except Exception:  # a dead event connection must never crash chat
            pass

    # ================================================================ keys
    def on_key(self, key) -> None:
        if isinstance(self.overlay, TextPrompt):
            self._prompt_key(key)
        elif self.overlay is not None:
            self._overlay_key(key)
        else:
            self._editor_key(key)

    def _overlay_key(self, key) -> None:
        overlay, name = self.overlay, key.name
        if isinstance(overlay, Choice):
            if name in ("up", "down"):
                overlay.move(-1 if name == "up" else 1)
            elif name == "enter":
                self._answer(overlay.value)
            elif name == "esc":
                self._answer("deny", ask_why=False)
            elif name == "ctrl-c":
                self._answer("deny", ask_why=False)
                self._stop()
            elif name == "text":
                value = overlay.pick(key.text[:1])
                if value is not None:
                    self._answer(value)
            return
        if isinstance(overlay, Picker):
            if name == "ctrl-p":
                self.overlay = None
                return
            if name in ("up", "down", "ctrl-n"):
                overlay.move(-1 if name == "up" else 1)
            elif name in ("esc", "ctrl-c"):
                self.overlay = None
                if not isinstance(overlay, CommandPalette):
                    self._done("Cancelled", dim=True)
            elif name == "enter":
                value = overlay.value
                self.overlay = None
                if value is not None:
                    overlay.on_pick(value)
            elif name == "backspace":
                overlay.erase()
            elif name == "text":
                overlay.type(key.text)

    def _prompt_key(self, key) -> None:
        prompt = self.overlay
        if key.name in ("esc", "ctrl-c"):
            self.overlay = None
            prompt.on_value(None)
        elif key.name == "enter":
            self.overlay = None
            prompt.on_value(prompt.value.strip() or None)
        elif key.name == "backspace":
            prompt.value = prompt.value[:-1]
        elif key.name == "kill-start":
            prompt.value = ""
        elif key.name in ("text", "paste"):
            prompt.value += key.text.replace("\n", " ")

    def _editor_key(self, key) -> None:
        editor, name, now = self.editor, key.name, time.monotonic()
        before = editor.text
        if name == "ctrl-p":
            self._open_palette()
        elif name == "ctrl-t":
            self.renderer.verbose = not self.renderer.verbose
            self.renderer.no_think = False
        elif name in ("pageup", "pagedown"):
            r = self.renderer
            r.detail_offset = max(0, r.detail_offset + (10 if name == "pageup" else -10))
        elif name == "ctrl-o":
            self.renderer.tools_expanded = not self.renderer.tools_expanded
        elif name == "ctrl-c":
            if self.job is not None:
                self._stop()
            elif editor.text:
                editor.clear()
            elif now - self.last_ctrl_c < 1.5:
                self.quit = True
            else:
                self.last_ctrl_c = now
                self._flash("Press Ctrl+C again to exit")
        elif name == "ctrl-d":
            if not editor.text and self.job is None:
                self.quit = True
            else:
                editor.delete()
        elif name == "esc":
            if self.menu is not None:
                self.menu_dismissed = editor.text
                self.menu = None
            elif self.show_shortcuts:
                self.show_shortcuts = False
            elif self.job is not None:
                self._stop()
            elif editor.text and now - self.last_esc < 0.8:
                editor.clear()
            elif editor.text:
                self.last_esc = now
                self._flash("Esc again to clear")
        elif name == "shift-tab":
            self._cycle_permission()
        elif name == "clear-screen":
            self.screen.clear()
        elif name == "suspend":
            self._suspend()
        elif name in ("up", "down", "ctrl-n"):
            if self.menu is not None and self.menu.items:
                self.menu_index = (self.menu_index + (-1 if name == "up" else 1)) \
                    % len(self.menu.items)
                return
            editor.up() if name == "up" else editor.down()
        elif name == "tab":
            if self.menu is not None:
                self._accept(submit=False)
        elif name == "enter":
            if self.menu is not None and self.menu.items:
                self._accept(submit=True)
            elif editor.text[:editor.cursor].endswith("\\"):
                editor.backspace()
                editor.insert("\n")
            else:
                self.submit(editor.submit())
        elif name == "newline":
            editor.insert("\n")
        elif name == "text":
            if key.text == "?" and not editor.text:
                self.show_shortcuts = not self.show_shortcuts
                return
            editor.insert(key.text)
        elif name == "paste":
            editor.paste(key.text)
        else:
            action = {"backspace": editor.backspace, "delete": editor.delete,
                      "left": editor.left, "right": editor.right, "home": editor.home,
                      "end": editor.end, "word-left": editor.word_left,
                      "word-right": editor.word_right, "kill-end": editor.kill_end,
                      "kill-start": editor.kill_start, "kill-word": editor.kill_word,
                      "kill-word-right": editor.kill_word_right, "yank": editor.yank}.get(name)
            if action:
                action()
        if name != "esc":
            self.show_shortcuts = False
        if editor.text != before or name in ("left", "right", "home", "end"):
            self._refresh_menu()

    # ================================================================ suggestions
    def _refresh_menu(self) -> None:
        text = self.editor.text
        if not text or text == self.menu_dismissed:
            self.menu = None
            return
        self.menu_dismissed = None
        descriptions = {name: description for name, (_usage, description) in COMMANDS.items()}
        found = suggest(text, self.editor.cursor, descriptions, self.argument_options, self.files)
        labels = [item.label for item in found.items] if found else []
        previous = [item.label for item in self.menu.items] if self.menu else []
        if labels[:1] != previous[:1] or len(labels) != len(previous):
            self.menu_index = 0
        self.menu = found
        if self.menu is not None:
            self.menu_index = min(self.menu_index, len(self.menu.items) - 1)

    def argument_options(self, name: str, previous: list[str]) -> list[tuple[str, str]]:
        """``(value, detail)`` for the argument being typed after ``/name``."""
        if previous:
            return []
        if name == "model":
            if not self._models:
                self._load_lists_later()
            return [(entry["name"], "current" if entry["name"] == self.model else entry["model"])
                    for entry in self._models]
        if name == "mode":
            return [(mode, MODE_DETAILS.get(mode, "")) for mode in MODES]
        if name == "web":
            return [("on", "search the web when it helps"), ("off", "never search the web")]
        if name == "permissions":
            return list(PERMISSIONS.items())
        if name == "resume":
            return [(str(index), chat.get("title") or "Untitled")
                    for index, chat in enumerate(self._chats[:15], 1)]
        if name == "cd":
            try:
                folders = sorted(entry.name for entry in os.scandir(self.cwd)
                                 if entry.is_dir() and not entry.name.startswith("."))
            except OSError:
                folders = []
            return [("..", "the folder above")] + [(folder, "") for folder in folders[:50]]
        return []

    # ================================================================ palette
    def _palette_items(self) -> tuple[list[tuple[str, str, object]], dict]:
        """Everything the Ctrl+P palette can find, grouped by kind."""
        items: list[tuple[str, str, object]] = []
        groups: dict = {}
        for name, (usage, description) in COMMANDS.items():
            if name == "quit":  # /quit is offered as /exit
                continue
            value = ("command", name)
            items.append((usage, description, value))
            groups[value] = "Commands"
        for entry in sorted(self._models, key=lambda model: (model.get("provider_name") or "",
                                                             model.get("name") or "")):
            name = str(entry.get("name") or "")
            if not name:
                continue
            value = ("model", name)
            detail = str(entry.get("model") or "")
            provider = str(entry.get("provider_name") or "")
            if provider:
                detail = f"{detail} \u00b7 {provider}" if detail else provider
            if name == self.model:
                detail = f"{detail} \u00b7 current" if detail else "current"
            items.append((name, detail, value))
            groups[value] = "Models"
        for chat in self._chats[:20]:
            chat_id = str(chat.get("id") or "")
            if not chat_id:
                continue
            value = ("chat", chat_id)
            detail = f"{chat.get('mode') or 'work'} \u00b7 {chat.get('message_count', 0)} messages"
            if chat_id == self.chat_id:
                detail += " \u00b7 current"
            items.append((str(chat.get("title") or "Untitled"), detail, value))
            groups[value] = "Chats"
        return items, groups

    def _open_palette(self) -> None:
        self._load_lists_later()
        items, groups = self._palette_items()
        self.overlay = CommandPalette(items, self._palette_pick, groups=groups)

    def _palette_pick(self, value) -> None:
        kind, choice = value
        if kind != "command" and self.job is not None:
            self._flash("Stop the reply first", 2)
            return
        try:
            if kind == "command":
                name = str(choice).lstrip("/").split(" ")[0]
                if "<" in COMMANDS.get(name, ("", ""))[0]:
                    self.editor.set("/" + name + " ")  # needs an argument: let them finish
                    self._refresh_menu()
                else:
                    self.submit("/" + name)            # optional arguments open their picker
            elif kind == "model":
                self._cmd_model(str(choice))
            elif kind == "chat":
                self._cmd_resume(str(choice))
        except KovaError as exc:
            self.renderer.error(exc.message)
        except Exception as exc:  # a pick must never take the chat down
            self.renderer.error(f"Command palette: {exc}")

    def _accept(self, submit: bool) -> None:
        found, item = self.menu, self.menu.items[self.menu_index]
        editor = self.editor
        editor.text = editor.text[:found.start] + item.value + editor.text[found.end:]
        editor.cursor = found.start + len(item.value)
        self.menu = None
        if submit and item.submit and editor.text.startswith("/"):
            # Enter on a command runs it, unless it only makes sense with an argument.
            name = item.label.lstrip("/").split(" ")[0]
            if found.start == 0 and "<" in COMMANDS.get(name, ("", ""))[0]:
                self._refresh_menu()
                return
            self.submit(editor.submit().rstrip())
            return
        self._refresh_menu()

    # ================================================================ submitting
    def submit(self, text: str) -> None:
        if not text.strip():
            return
        if self.job is not None:
            self.queued.append(text)
            return
        self._dispatch(text)

    def _dispatch(self, text: str) -> None:
        stripped = text.strip()
        if self.welcome_started is not None:
            self._commit_welcome()
        if stripped.startswith("/") and "\n" not in stripped:
            self.renderer.user(stripped)
            try:
                self.command(stripped)
            except KovaError as exc:
                self.renderer.error(exc.message)
            except Exception as exc:  # a failing command must not take the chat down
                self.renderer.error(f"{stripped.split()[0]} failed: {exc}")
            return
        if stripped.startswith("!"):
            self._shell(stripped[1:].strip())
            return
        if stripped.startswith("#"):
            self._remember(stripped[1:].strip())
            return
        self.renderer.user(text)
        message, attached = expand_mentions(text, self.cwd)
        for path, lines in attached:
            self.renderer.line(self.p.faint("  ⎿  ") + self.p.dim("Read ") + self.p.bold(path)
                               + self.p.dim(f" ({lines} lines)"))
        if attached:
            self.renderer.line()
        if self.context_notes:
            message = "\n\n".join([*self.context_notes, message])
            self.context_notes.clear()
        self.context_chars += len(message)
        self.renderer.begin_turn()
        client, chat_id = self.client, self.chat_id

        def reply(emit, cancel):
            events = client.stream_message(chat_id, message)
            try:
                for event in events:
                    emit(event)
                    if event.get("type") == "done":
                        break
            finally:
                events.close()
        self.job = Job("reply", reply, label=text)

    def _shell(self, command: str) -> None:
        if not command:
            return
        self.renderer.shell(command)
        self.renderer.begin_turn()
        self.renderer.headed = True  # a command is the user's, not a Kova reply
        self.renderer.pending_tool = ("run_command", {"command": command})
        self.renderer.activity = "Running"
        cwd = self.cwd
        self.job = Job("shell", lambda _emit, cancel: run_shell(command, cwd, cancel),
                       label=command)

    def _remember(self, note: str) -> None:
        if not note:
            return
        self.renderer.remember(note)
        title = shorten(note, 60)
        try:
            self.client.memory_add(title, note, tags=["chat"])
        except KovaError as exc:
            self.renderer.error(f"Could not remember that: {exc.message}")
            return
        self.renderer.line("  " + self.p.green("✓") + " " + self.p.pill("Remember", theme.USER)
                           + " " + self.p.dim(fit(note, 60)))
        self._done("Saved to memory")

    # ================================================================ jobs
    def _pump(self) -> None:
        while True:
            try:
                text = self.notices.get_nowait()
            except queue.Empty:
                break
            self.renderer.reminder(text)
        job = self.job
        if job is None:
            return
        if job.cancel.is_set() and time.monotonic() - job.cancelled_at > STOP_GRACE:
            self._finish(job)  # the server never ended the stream; let it go
            return
        while self.approval is None:
            try:
                event = job.events.get_nowait()
            except queue.Empty:
                return
            if event is None:
                self._finish(job)
                return
            kind = event.get("type")
            if kind == "approval":
                if job.cancel.is_set():
                    self._send_approval(event, "deny")
                else:
                    self._ask(event)
                continue
            if kind == "plan":
                self.plan = [item for item in event.get("items") or [] if isinstance(item, dict)]
            self.renderer.handle(event)

    def _finish(self, job: Job) -> None:
        self.job = None
        r = self.renderer
        stopped = job.cancel.is_set()
        if self.approval is not None:
            self.approval, self.overlay = None, None
        if job.kind == "shell":
            result = job.outcome.get("result") or {}
            if "error" in job.outcome:
                result = {"ok": False, "output": str(job.outcome["error"])}
            r.pending_tool = None
            output = str(result.get("output") or "")
            if result.get("code") not in (None, 0):
                output = output.rstrip("\n") + f"\nexit code {result['code']}"
            r.tool_row("run_command", {"command": job.label}, bool(result.get("ok")), output)
            r.turn_started, r.pass_chars = 0.0, 0  # a command of the user's has no totals
            r.finish_turn()
            self.context_notes.append(
                f"I ran this shell command myself in {self.cwd}:\n$ {job.label}\n"
                f"{output[-SHELL_CONTEXT_CHARS:].rstrip() or '(no output)'}")
        else:
            error = job.outcome.get("error")
            if error is not None and not stopped:
                r.end_text()
                r.reply_header()
                r.error(error.message if isinstance(error, KovaError) else str(error))
            r.finish_turn(stopped=stopped)
            self._refresh_title_later()
        if self.queued:
            if stopped:
                # Stopping a reply also holds what was queued behind it.
                waiting = "\n".join(self.queued)
                self.editor.set(waiting + ("\n" + self.editor.text if self.editor.text else ""))
                self.queued.clear()
            else:
                self._dispatch(self.queued.pop(0))

    def _stop(self) -> None:
        job = self.job
        if job is None or job.cancel.is_set():
            return
        job.cancel.set()
        job.cancelled_at = time.monotonic()
        self.renderer.stopping = True
        if job.kind == "reply":
            client, chat_id = self.client, self.chat_id

            def stop():
                try:
                    client.stop_chat(chat_id)
                except Exception:  # the grace period ends the reply on our side anyway
                    pass
            threading.Thread(target=stop, daemon=True, name="kova-chat-stop").start()

    # ================================================================ approvals
    def _ask(self, event: dict) -> None:
        r = self.renderer
        r.end_text()
        args = event.get("args") if isinstance(event.get("args"), dict) else {}
        risk = event.get("risk") or "normal"
        title, body, colour = r.permission_card(str(event.get("tool", "")), args, risk,
                                                str(event.get("summary") or ""))
        options = [("Allow once", "allow", "y")]
        options.append(("Allow this exact action for this session", "allow_session", "a"))
        options.append(("Always allow this exact action", "allow_always", "w"))
        options.append(("No, and tell Kova what to do differently", "deny", "n"))
        self.overlay = Choice(title, body, options, colour=colour,
                              footer="enter to choose · esc to decline")
        self.approval = event
        r.asking = True
        r.activity = "Waiting for your answer"

    def _answer(self, decision: str, ask_why: bool = True) -> None:
        event = self.approval
        self.overlay = None
        if event is None:
            return
        if decision == "deny" and ask_why:
            def send(message):
                self._send_approval(event, "deny", message)
            self.overlay = TextPrompt("What should Kova do instead?", send,
                                      placeholder="optional — leave empty to just decline")
            return
        self._send_approval(event, decision)

    def _send_approval(self, event: dict, decision: str, message: str | None = None) -> None:
        if self.approval is event:
            self.approval = None
        self.renderer.asking = False
        self.renderer.activity = ""
        try:
            self.client.answer_approval(self.chat_id, str(event.get("id", "")), decision, message)
        except KovaError as exc:
            self.renderer.error(f"Could not answer the approval: {exc.message}")
            return
        r, tool = self.renderer, str(event.get("tool", "") or "the action")
        r.reply_header()
        if decision == "allow":
            r.line(self.p.dim("  ⎿  ") + f"Allowed {tool}")
        elif decision in {"allow_session", "allow_always"}:
            scope = "this session" if decision == "allow_session" else "always"
            r.line(self.p.dim("  ⎿  ") + f"Allowed {tool} "
                   + self.p.dim(f"· this exact action, {scope}"))
        else:
            r.line(self.p.dim("  ⎿  ") + self.p.red(f"Declined {tool}")
                   + (self.p.dim(f" · {fit(message, 60)}") if message else ""))

    # ================================================================ commands
    def command(self, line: str) -> None:
        name, _, rest = line[1:].partition(" ")
        name, rest = name.lower(), rest.strip()
        handler = getattr(self, f"_cmd_{name}", None)
        if handler is None or name not in COMMANDS:
            close = rank(sorted(COMMANDS), name)
            hint = f" Did you mean /{close[0]}?" if close and name else " Type /help to see them."
            self.renderer.error(f"Unknown command /{name}.{hint}")
            return
        handler(rest)

    def _done(self, text: str, *, dim: bool = False) -> None:
        """A command's one-line result under its echo."""
        self.renderer.line(self.p.faint("  ⎿  ") + (self.p.dim(text) if dim else text))
        self.renderer.line()

    def _choose(self, title: str, items: list[tuple[str, str, object]], on_pick, *,
                current=None, subtitle: str = "", groups: dict | None = None) -> None:
        self.overlay = Picker(title, items, on_pick, current=current, subtitle=subtitle, groups=groups)

    def _cmd_compact(self, _rest: str) -> None:
        self.renderer.begin_turn()
        client, chat_id = self.client, self.chat_id

        def run(emit, cancel):
            event = client.compact_chat(chat_id)
            emit(event)
            emit({"type": "done"})
        self.job = Job("reply", run, label="/compact")

    def _cmd_help(self, _rest: str) -> None:
        p, r = self.p, self.renderer
        width = max(len(usage) for usage, _ in COMMANDS.values()) + 3
        room = max(10, r.width - width - 4)
        r.line("  " + p.paint("1;" + ACCENT, "◆") + " " + p.bold("Commands"))
        for name, (usage, text) in COMMANDS.items():
            if name == "quit":
                continue
            r.line("    " + p.paint(ACCENT_SOFT, usage.ljust(width)) + p.dim(fit(text, room)))
        r.line()
        r.line("  " + p.paint("1;" + ACCENT, "◆") + " " + p.bold("Keys"))
        for key, text in (("esc", "stop the reply · close a menu · twice to clear the input"),
                          ("ctrl+p", "command palette: commands, models and chats"),
                          ("ctrl+c", "stop · clear the input · twice to exit"),
                          ("ctrl+t", "toggle reasoning"), ("ctrl+o", "toggle tool details"),
                          ("ctrl+d", "exit"), ("shift+tab", "cycle permission modes"),
                          ("\\⏎  shift+⏎  ctrl+j", "new line"), ("tab", "complete"),
                          ("↑ ↓", "earlier messages"), ("ctrl+l", "clear the screen"),
                          ("! # @ /", "shell command · remember · attach a file · command")):
            r.line("    " + p.paint(ACCENT_SOFT, key.ljust(width)) + p.dim(fit(text, room)))
        r.line()

    def _cmd_exit(self, _rest: str) -> None:
        self.quit = True

    _cmd_quit = _cmd_exit

    def _cmd_new(self, _rest: str) -> None:
        chat = self.client.create_chat(mode=self.mode, web=self.web, permission=self.permission,
                                       cwd=self.cwd)
        self.chat_id = chat["id"]
        self.plan = []
        self.refresh_chat()
        self._load_lists_later()
        self._done("Started a new chat. Earlier ones stay in /resume.")

    def _cmd_clear(self, _rest: str) -> None:
        self.screen.clear()
        self._commit_welcome(tips=False)

    def _cmd_think(self, _rest: str) -> None:
        r = self.renderer
        r.verbose = not r.verbose
        r.no_think = False
        self._done("Reasoning expanded." if r.verbose else "Reasoning collapsed.")

    def _cmd_mode(self, rest: str) -> None:
        if not rest:
            items = [(mode, MODE_DETAILS.get(mode, ""), mode) for mode in MODES]
            self._choose("Select mode", items, self._cmd_mode, current=self.mode,
                         subtitle="Takes effect on your next message")
            return
        if rest not in MODES:
            self.renderer.error(f'Unknown mode "{rest}". Use /mode {" or ".join(MODES)}.')
            return
        self.client.update_chat(self.chat_id, mode=rest)
        self.mode = rest
        self._done(f"Mode: {self.p.bold(rest)}. Takes effect on your next message.")

    def _cmd_web(self, rest: str) -> None:
        if rest not in ("on", "off"):
            if rest:
                self.renderer.error("Use /web on or /web off.")
            else:
                self._done(f"Web search is {'on' if self.web else 'off'}. "
                           "Toggle with /web on or /web off.")
            return
        self.client.update_chat(self.chat_id, web=rest == "on")
        self.web = rest == "on"
        self._done(f"Web search {rest}. Takes effect on your next message.")

    def _cmd_model(self, rest: str) -> None:
        if not rest:
            models = self.client.models_list()
            self._models = models
            models = sorted(models, key=lambda m: (m.get("provider_name", ""), m["name"]))
            items = [(entry["name"], entry["model"], entry["name"]) for entry in models]
            if not items:
                self._done("No models yet. Run kova setup to add one.")
                return
            self._choose("Select model", items, self._cmd_model, current=self.model,
                         subtitle="Used for every chat",
                         groups={entry["name"]: entry.get("provider_name", "Custom") for entry in models})
            return
        try:
            self.client.model_use(rest)
        except KovaError as exc:
            if exc.status != 404:
                raise
            self.renderer.error(f'No model named "{rest}". Type /model to see them.')
            return
        self.model = rest
        self._refresh_health()
        self._sync_context_window()
        self._done(f"Now using {self.p.bold(rest)}.")

    def _cmd_permissions(self, rest: str) -> None:
        if rest.lower() == "rules":
            rules = self.client.permission_rules()
            def save(value):
                if value is None:
                    return
                try:
                    self.client.replace_permission_rules(json.loads(value))
                    self._done("Permission rules saved")
                except (ValueError, KovaError) as exc:
                    self.renderer.error(str(exc))
            self.overlay = TextPrompt("Permission rules (JSON: action/tool/pattern/scope)", save,
                                      placeholder=json.dumps(rules))
            self.overlay.value = json.dumps(rules)
            return
        level = rest.lower()
        if level not in PERMISSIONS:
            if rest:
                self.renderer.error(f'Unknown permission mode "{rest}". '
                                    f"Use /permissions {', '.join(PERMISSIONS)}.")
                return
            items = [(name, text, name) for name, text in PERMISSIONS.items()]
            self._choose("Permissions", items, self._cmd_permissions, current=self.permission,
                         subtitle="When Kova asks before acting in this chat")
            return
        if self._set_permission(level):
            self._done(f"Permissions: {self.p.bold(level)} · {PERMISSIONS[level]}")

    def _set_permission(self, level: str) -> bool:
        chat = self.client.update_chat(self.chat_id, permission=level)
        if "permission" not in chat:
            self.renderer.error("This Kova server doesn't support permission modes yet. "
                                "Update it with kova update.")
            return False
        self.permission = chat["permission"]
        return True

    def _cycle_permission(self) -> None:
        current = self.permission if self.permission in PERMISSION_CYCLE else PERMISSION_CYCLE[-1]
        level = PERMISSION_CYCLE[(PERMISSION_CYCLE.index(current) + 1) % len(PERMISSION_CYCLE)]
        try:
            changed = self._set_permission(level)
        except KovaError as exc:
            self._flash(f"Could not change permissions: {exc.message}", 3)
            return
        if not changed:
            self._flash("This Kova server has no permission modes yet", 3)

    def _cmd_cd(self, rest: str) -> None:
        target = Path(rest or "~").expanduser()
        if not target.is_absolute():
            target = Path(self.cwd) / target
        if not target.is_dir():
            self.renderer.error(f"Not a folder: {target}")
            return
        folder = str(target.resolve())
        chat = self.client.update_chat(self.chat_id, cwd=folder)
        self.cwd = folder
        self.files = FileIndex(folder)
        note = "" if "cwd" in chat else self.p.dim(" · for @ files and ! commands")
        self._done(f"Folder: {self.p.bold(_home_short(folder))}" + note)

    def _cmd_resume(self, rest: str) -> None:
        chats = self.client.list_chats()[:30]
        self._chats = chats
        if not rest:
            if not chats:
                self._done("No saved chats yet.")
                return
            items = [(chat.get("title") or "Untitled",
                      f"{chat.get('mode') or 'work'} · {chat.get('message_count', 0)} messages",
                      chat["id"]) for chat in chats]
            self._choose("Resume a chat", items, self._cmd_resume, current=self.chat_id)
            return
        chat = None
        if rest.isdigit() and 1 <= int(rest) <= len(chats):
            chat = chats[int(rest) - 1]
        else:
            chat = next((c for c in chats if c["id"] == rest), None) \
                or next((c for c in chats if c["id"].startswith(rest)), None)
        if chat is None:
            self.renderer.error(f"No chat matches {rest!r}. Type /resume to pick one.")
            return
        self.chat_id = chat["id"]
        self.plan = []
        self.refresh_chat()
        self._done(f"Resumed {self.p.bold(chat.get('title') or 'Untitled')}")
        self.replay()

    def replay(self) -> None:
        """The chat's recent messages, so a resumed conversation reads as it was."""
        chat = self.client.get_chat(self.chat_id) or {}
        messages = [m for m in chat.get("messages", [])
                    if m.get("role") in ("user", "assistant") and m.get("content")]
        hidden = len(messages) - REPLAY_MESSAGES
        if hidden > 0:
            self.renderer.line(self.p.faint(f"  … {hidden} earlier messages"))
            self.renderer.line()
        for message in messages[-REPLAY_MESSAGES:]:
            if message["role"] == "user":
                self.renderer.user(message["content"])
            else:
                self.renderer.begin_turn()
                self.renderer.handle({"type": "text", "text": message["content"]})
                self.renderer.end_text()
                self.renderer.spinner = ""
                self.renderer.in_turn = False

    # ================================================================ drawing
    def _flash(self, text: str, seconds: float = 1.5) -> None:
        self.flash_text, self.flash_until = text, time.monotonic() + seconds

    def _animate(self) -> None:
        now = time.monotonic()
        if now - self.last_frame >= FRAME:
            self.frame += 1
            self.last_frame = now
        if self.welcome_started is not None and now - self.welcome_started >= WELCOME_SECONDS:
            self._commit_welcome()

    def _welcome_lines(self, animate: bool, tips: bool = True) -> list[str]:
        width = self.screen.size()[0] - 1
        phase = None
        if animate and self.welcome_started is not None:
            phase = (time.monotonic() - self.welcome_started) / WELCOME_SECONDS * 12 - 4
        return welcome(self.p, width, version=__version__, model=self.model or "?",
                       mode=self.mode, cwd=_home_short(self.cwd),
                       memory=_home_short(self.memory_path) if self.memory_path else "",
                       notes=self.notes, web=self.web, offline=not self.model_ok, phase=phase,
                       tips=tips, title=self.title, permission=self.permission)

    def _commit_welcome(self, tips: bool = True) -> None:
        self.welcome_started = None
        for line in self._welcome_lines(animate=False, tips=tips):
            self.renderer.line(line)
        self.renderer.line()

    def _draw(self) -> None:
        if self.resized:
            self.resized = False
            self.screen.dirty = True
        width = self.screen.size()[0] - 1
        p = self.p
        lines: list[str] = []
        if self.welcome_started is not None:
            lines += [*self._welcome_lines(animate=True), ""]
        lines += self.renderer.detail_lines()
        if self.job is not None:
            lines += self.renderer.live_lines(self.frame)
        for text in self.queued[-3:]:
            lines.append(p.faint("  ❯ " + fit(text.replace("\n", " ⏎ "), width - 14))
                         + p.faint("  (queued)"))
        if lines and lines[-1] != "":
            lines.append("")
        if not self.screen.ends_blank:
            lines.insert(0, "")  # one blank line between the transcript and the live region
        caret = None
        if self.overlay is not None:
            lines += self.overlay.render(width, p)
            lines.append(self._status(width))
        else:
            busy = self.job is not None
            box_lines, (row, col) = input_box(self.editor.text, self.editor.cursor, width, p,
                                              placeholder="" if busy else self.placeholder,
                                              footer=self._hint(), busy=busy and not self.editor.text)
            caret = (len(lines) + row, col)
            lines += box_lines
            if self.menu is not None:
                lines += menu(self.menu.items, self.menu_index, width, p)
            elif self.show_shortcuts:
                lines += shortcuts(width, p)
            lines.append(self._status(width))
        self.screen.set_live(lines, caret)
        self.screen.render()

    def _hint(self) -> str:
        """The input box's footer: what the keys do right now."""
        p, text = self.p, self.editor.text
        if self.flash_text and time.monotonic() < self.flash_until:
            return p.yellow(self.flash_text)
        if text.startswith("!"):
            return p.paint(BASH, "! shell command") + p.faint(" · runs in "
                                                              + fit(_home_short(self.cwd), 30))
        if text.startswith("#"):
            return p.paint(MEMORY, "# save a memory note")
        if self.job is not None:
            return p.faint("esc stop · ctrl+t thinking · ctrl+o tools")
        if self.permission in PERMISSIONS and self.permission != "manual":
            return (p.paint(MODE_COLOURS.get(self.permission, ACCENT),
                            f"⏵⏵ {self.permission} permissions")
                    + p.faint(" · shift+tab to cycle"))
        if text:
            return p.faint("⏎ send · \\⏎ new line · ctrl+p palette")
        return p.faint("ctrl+p palette · ctrl+t thinking · ? help")

    def _turn_stats(self) -> str:
        """Tokens, context fill and turn time for the right of the status line.

        While a reply runs these are live; afterwards they describe the last
        turn. Tokens are estimated from characters, like the spinner's count,
        and context fill compares that estimate with the model's window.
        """
        r, live = self.renderer, self.job is not None
        tokens = r.tokens if live else r.last_tokens
        elapsed = (time.monotonic() - r.turn_started if r.turn_started else 0.0) \
            if live else r.last_elapsed
        parts = []
        if tokens:
            parts.append(f"{compact_number(tokens)} tok")
        if self.context_window:
            used = self.context_chars // 4 + (r.pass_chars // 4 if live else 0)
            fill = max(0.0, used / self.context_window * 100)
            percent = "<1%" if 0 < fill < 1 else f"{min(100, round(fill))}%"
            parts.append(f"ctx {percent}")
        if elapsed:
            parts.append(duration(elapsed))
        return " \u00b7 ".join(parts)

    def _status(self, width: int) -> str:
        plan = ""
        if self.plan:
            done = sum(item.get("status") == "done" for item in self.plan)
            plan = f"{done}/{len(self.plan)}"
        return status_bar(self.p, width, mode=self.mode, model=self.model or "?",
                          title=self.title, permission=self.permission, web=self.web, plan=plan,
                          stats=self._turn_stats(), offline=not self.model_ok,
                          cwd=_home_short(self.cwd))


__all__ = ["COMMANDS", "ChatApp", "Job", "PERMISSIONS", "TextPrompt", "run_shell"]
