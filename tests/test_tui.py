"""The full-screen terminal chat: keys, the input editor, suggestions, file
mentions, the screen's live region, the widgets it draws, streamed Markdown,
the reply renderer, the chat loop's handling of approvals, plans, queueing
and stopping, and the real ``kova`` in a pseudo-terminal against a fake server.

The unit tests up to the renderer are ported from Kaine's
``tests/test_cli_tui.py`` and ``tests/test_cli.py``.
"""
from __future__ import annotations

import io
import os
import random
import re
import sys
import time
from types import SimpleNamespace

import pytest
from rich.console import Console

from kova.tui.app import ChatApp, Job, TextPrompt, run_shell
from kova.tui.editor import Editor, Suggestion, expand_mentions, load_history, save_history, suggest
from kova.tui.markdown import MarkdownStream, Palette, clean, display_width
from kova.tui.render import Renderer, command_output
from kova.tui.terminal import KeyReader, Screen
from kova.tui.widgets import (
    Choice,
    CommandPalette,
    Picker,
    input_box,
    menu,
    shimmer,
    spinner_line,
    visible,
    welcome,
)
from tests.fake_kova import FakeKova, Terminal, write_config
from tests.vt import VT

ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
linux_only = pytest.mark.skipif(not sys.platform.startswith("linux"),
                                reason="pty behaviour is Linux-specific")


def plain(text: str) -> str:
    return ANSI.sub("", text)


# ------------------------------------------------------------------ keys
@pytest.fixture
def keys():
    read, write = os.pipe()
    reader = KeyReader(read)

    def feed(data: bytes):
        os.write(write, data)
        return [(key.name, key.text) for key in reader.read(0.05)]
    yield feed
    os.close(read)
    os.close(write)


def test_keys_decode_text_sequences_and_controls(keys):
    assert keys(b"hi\x1b[Aok\r") == [("text", "hi"), ("up", ""), ("text", "ok"), ("enter", "")]
    assert keys(b"\x1b[1;5D\x1b[Z\x7f\x03\x04\x0c") == [
        ("word-left", ""), ("shift-tab", ""), ("backspace", ""), ("ctrl-c", ""), ("ctrl-d", ""),
        ("clear-screen", "")]
    assert keys(b"\x1b\r\x1b[13;2u\n") == [("newline", "")] * 3  # alt/shift+enter, ctrl+j


def test_a_lone_esc_is_a_key_and_unknown_sequences_are_dropped(keys):
    assert keys(b"\x1b") == [("esc", "")]
    assert keys(b"\x1b\x1b") == [("esc", ""), ("esc", "")]
    assert keys(b"\x1b[99;99X\x1bx") == []


def test_pastes_arrive_whole_even_across_reads(keys):
    assert keys(b"\x1b[200~one\r\ntwo") == []
    assert keys(b"\rthree\x1b[201~!") == [("paste", "one\ntwo\nthree"), ("text", "!")]


def test_characters_split_across_reads_are_joined(keys):
    assert keys(b"caf\xc3") == [("text", "caf")]
    assert keys(b"\xa9") == [("text", "é")]


# ------------------------------------------------------------------ editor
def test_editor_moves_and_kills_like_a_shell():
    editor = Editor()
    editor.insert("hello brave world")
    editor.word_left()
    editor.kill_word()
    assert editor.text == "hello world" and editor.cursor == 6
    editor.yank()
    assert editor.text == "hello brave world"
    editor.home()
    editor.kill_end()
    assert editor.text == "" and editor.killed == "hello brave world"
    editor.insert("one\ntwo three")
    editor.up()
    assert editor.row_col == (0, 3)
    editor.down()
    editor.kill_start()
    assert editor.text == "one\n three"


def test_history_walks_back_and_restores_the_draft():
    editor = Editor(["first", "second"])
    editor.insert("draft")
    editor.up()
    assert editor.text == "second"
    editor.up()
    assert editor.text == "first"
    editor.down()
    editor.down()
    assert editor.text == "draft"
    assert editor.submit() == "draft" and editor.history[-1] == "draft" and editor.text == ""


def test_long_pastes_collapse_and_expand_on_submit():
    editor = Editor()
    editor.insert("look: ")
    pasted = "\n".join(f"line {n}" for n in range(20))
    editor.paste(pasted)
    assert editor.text == "look: [Pasted text #1 +20 lines]"
    editor.backspace()  # a paste token goes in one keystroke
    assert editor.text == "look: "
    editor.paste(pasted)
    assert editor.submit() == "look: " + pasted


def test_history_file_round_trips(tmp_path):
    path = tmp_path / "state" / "history.jsonl"
    save_history(path, ["one", "two\nlines"])
    assert load_history(path) == ["one", "two\nlines"]
    assert oct(path.stat().st_mode & 0o777) == "0o600"


# ------------------------------------------------------------------ suggestions
COMMANDS = {"model": "Choose the model", "mode": "Chat or work", "memory": "Memory",
            "permissions": "When Kova asks", "help": "Help"}


def options(name, previous):
    if previous:
        return []
    return [("manual", ""), ("auto", ""), ("full", "")] if name == "permissions" else []


def labels(found):
    return [item.label for item in found.items] if found else None


def test_commands_rank_prefix_matches_first():
    found = suggest("/mo", 3, COMMANDS, options, list)
    assert labels(found) == ["/mode", "/model", "/memory", "/permissions"]  # fuzzy last
    assert (found.start, found.end) == (0, 3) and found.items[0].submit


def test_arguments_are_offered_and_a_finished_one_is_not():
    found = suggest("/permissions a", 14, COMMANDS, options, list)
    assert labels(found) == ["auto", "manual"] and found.start == 13
    assert suggest("/permissions auto", 17, COMMANDS, options, list) is None


def test_file_mentions_and_plain_text():
    files = lambda: ["README.md", "src/", "src/kova.py", "src/kova_tui.py"]  # noqa: E731
    found = suggest("read @src/kova", 14, COMMANDS, options, files)
    assert labels(found) == ["@src/kova.py", "@src/kova_tui.py"] and found.start == 5
    assert not found.items[0].submit
    assert suggest("just a message", 14, COMMANDS, options, files) is None


def test_mentions_attach_files_in_the_folder_only(tmp_path):
    (tmp_path / "notes.md").write_text("alpha\nbeta\n")
    (tmp_path / "blob.bin").write_bytes(b"\0\1\2")
    outside = tmp_path.parent / "secret.txt"
    outside.write_text("nope")
    text, attached = expand_mentions("see @notes.md, @blob.bin and @../secret.txt", str(tmp_path))
    assert attached == [("notes.md", 2)]
    assert '<file path="notes.md">\nalpha\nbeta\n</file>' in text and "nope" not in text


# ------------------------------------------------------------------ screen
class FakeTerminal:
    def __init__(self, columns=40, rows=12):
        self.vt = VT(columns, rows)
        self.size = (columns, rows)

    def write(self, text):
        self.vt.feed(text)

    def flush(self):
        pass


def test_the_live_region_never_leaves_marks_in_the_transcript():
    term = FakeTerminal()
    screen = Screen(term, size_fn=lambda: term.size)
    box = ["╭────╮", "│ >  │", "╰────╯", "  hint"]
    screen.set_live(["spinner", "", *box], caret=(3, 4))
    screen.render()
    screen.write("streaming ")
    screen.render()
    screen.write("text\nsecond line that is long enough to wrap past forty columns\n")
    screen.set_live(box, caret=(1, 4))
    screen.render()
    for index in range(15):
        screen.write(f"line {index}\n")
        screen.render()
    history = term.vt.history()
    assert "streaming text" in history
    assert history.count("spinner") == 0 and history.count("╭────╮") == 1
    assert history[-5:] == ["line 14", *box]
    assert (term.vt.y, term.vt.x) == (term.vt.rows - 3, 4)  # the caret sits in the box
    screen.close()
    assert "╭────╮" not in term.vt.history()


def test_a_redraw_hands_the_transcript_back_its_style():
    out = FakeTerminal()
    chunks = []
    out.write = chunks.append
    screen = Screen(out, size_fn=lambda: out.size)
    screen.write("normal \x1b[0;1mbold ")
    screen.set_live(["live"])
    screen.render()
    screen.write("still bold")
    screen.render()
    last = chunks[-1]
    assert last.index("\x1b[J\x1b[0;1m") < last.index("still bold")


def test_untrusted_text_cannot_steer_the_terminal():
    assert clean("ok\x1b[2J\x1b]0;pwned\x07\x9b31m done\n") == "ok[2J]0;pwned31m done\n"


# ------------------------------------------------------------------ widgets
def test_input_box_places_the_caret_on_wrapped_and_prefixed_text():
    p = Palette(False)
    lines, caret = input_box("x" * 50, 50, 30, p)
    term = VT(40, 10)
    term.feed("\r\n".join(lines))
    assert caret == (3, 6) and term.line(3).startswith("│   xx")
    lines, caret = input_box("!ls -la", 7, 30, p)
    assert lines[1].startswith("│ ! ls -la") and caret == (1, 10)
    lines, caret = input_box("", 0, 30, p, placeholder='Try "x"')
    assert 'Try "x"' in lines[1] and lines[1].startswith("│ ❯ ") and caret == (1, 4)


def test_menu_dialog_and_picker_render():
    p = Palette(False)
    shown = menu([Suggestion("/model ", "/model", "Choose"), Suggestion("/mode ", "/mode", "")],
                 1, 40, p)
    assert shown == ["    /model  Choose", "  ❯ /mode"]
    choice = Choice("Run a shell command?", ["  rm -rf x"],
                    [("Yes", "allow", "y"), ("No", "deny", "n")])
    assert choice.pick("2") == "deny" and choice.pick("y") == "allow" and choice.pick("z") is None
    assert "❯ 1. Yes" in "\n".join(choice.render(40, p))
    picker = Picker("Select model", [("alpha", "", "a"), ("beta", "", "b")], None, current="b")
    assert picker.value == "b"
    picker.type("alp")
    assert picker.value == "a" and "Filter: alp" in "\n".join(picker.render(40, p))


def test_command_palette_filters_commands_models_and_chats():
    items = [("/model", "List models; /model <name> switches", ("command", "/model")),
             ("local", "fake-model \u00b7 current", ("model", "local")),
             ("Trip plans", "work \u00b7 3 messages", ("chat", "c2"))]
    groups = {("command", "/model"): "Commands", ("model", "local"): "Models",
              ("chat", "c2"): "Chats"}
    palette = CommandPalette(items, lambda _value: None, groups=groups)
    text = "\n".join(palette.render(80, Palette(False)))
    assert "COMMANDS" in text and "MODELS" in text and "CHATS" in text
    assert "\u276f /model" in text and "List models" in text

    fuzzy = CommandPalette(items, lambda _value: None, groups=groups)
    fuzzy.type("mdl")                                   # a subsequence of /model
    assert fuzzy.value == ("command", "/model")
    model = CommandPalette(items, lambda _value: None, groups=groups)
    model.type("fake")                                  # matches the model's id detail
    assert model.value == ("model", "local")
    chat = CommandPalette(items, lambda _value: None, groups=groups)
    chat.type("tp")                                     # fuzzy chat title
    assert chat.value == ("chat", "c2")

    empty = CommandPalette(items, lambda _value: None, groups=groups)
    empty.type("zzz")
    assert empty.value is None and "Nothing matches" in "\n".join(empty.render(40, Palette(False)))


@pytest.mark.parametrize("width", [24, 32, 44, 80, 120])
def test_command_palette_fits_narrow_terminals_without_colour(width):
    items = [("/model", "List models; /model <name> switches", ("command", "/model")),
             ("a-very-long-model-name", "some-provider/some-model \u00b7 current",
              ("model", "a-very-long-model-name")),
             ("A very long chat title that needs shortening", "work \u00b7 12 messages",
              ("chat", "c2"))]
    groups = {value: group for value, group in zip(
        (item[2] for item in items), ("Commands", "Models", "Chats"), strict=True)}
    palette = CommandPalette(items, lambda _value: None, groups=groups)
    palette.type("model")
    lines = palette.render(width, Palette(False))
    assert all(visible(line) <= width for line in lines), (width, lines)
    assert "\x1b" not in "\n".join(lines)
    assert len(lines) <= 14
    palette.type("x" * 60)                              # a query longer than the row
    assert all(visible(line) <= width for line in palette.render(width, Palette(False)))


def test_spinner_and_welcome_read_plainly_without_colour():
    p = Palette(False)
    assert shimmer("Thinking", 3, p) == "Thinking"
    line = spinner_line(p, verb="Pondering", frame=4, elapsed=12.0, tokens=1500, stopping=False)
    assert line.endswith("Pondering…  12s · ↓ 1.5K tokens · esc to interrupt")
    coloured = shimmer("Thinking", 3, Palette(True))
    assert coloured.count("\x1b[38;") == len("Thinking")
    card = "\n".join(welcome(p, 80, version="1.0.0", model="local", mode="work", cwd="/p",
                             memory="~/notes", notes=2, offline=True))
    assert "model    local" in card and "● offline" in card and "mode     work" in card
    assert "Ask anything." in card and "2 notes" in card and "█▄▀ █▀█" in card
    assert "unreachable" in card and "\x1b" not in card
    narrow = welcome(p, 34, version="1.0.0", model="local", mode="work", cwd="/p")
    assert "◆ Kova" in "\n".join(narrow) and all(len(line) <= 34 for line in narrow)


# ------------------------------------------------------------------ Markdown
SAMPLE = """# Heading

Some **bold**, *italic*, `code` and a [link](https://example.com/a) in a paragraph long enough to wrap.

- one
- two that is long enough to need wrapping onto a second line under the bullet text
  - nested
- [x] done
- [ ] open

1. first
2. second

> quoted

---

| Name | Uses |
|------|-----:|
| `web_search` | 12 |
| memory_save | 3 |

```python
print("hi")
```

Array[0] stays literal and 3 * 4 = 12.
"""


def render_markdown(chunks, *, color=False, width=60):
    out = []
    stream = MarkdownStream(out.append, width, Palette(color), first="● ")
    for chunk in chunks:
        stream.feed(chunk)
    stream.finish()
    return "".join(out)


@pytest.mark.parametrize("color", [False, True])
def test_markdown_output_does_not_depend_on_how_the_text_was_chunked(color):
    whole = render_markdown([SAMPLE], color=color)
    assert render_markdown(list(SAMPLE), color=color) == whole
    for seed in range(20):
        rng, parts, index = random.Random(seed), [], 0
        while index < len(SAMPLE):
            size = rng.randint(1, 9)
            parts.append(SAMPLE[index:index + size])
            index += size
        assert render_markdown(parts, color=color) == whole, f"seed {seed}"


def test_markdown_blocks_render_as_terminal_text():
    text = render_markdown([SAMPLE])
    assert "● Heading" in text and "#" not in text.splitlines()[0]
    assert "  • one" in text and "    ◦ nested" in text
    assert "  ☑ done" in text and "  ☐ open" in text
    assert "  1. first" in text
    assert "│ quoted" in text
    assert "│ web_search  │ 12   │" in text
    assert '    print("hi")' in text
    assert "link (https://example.com/a)" in text
    assert "Array[0] stays literal and 3 * 4 = 12." in text
    assert "\x1b" not in text


def test_markdown_wraps_on_words_inside_the_width():
    text = render_markdown(["- " + "word " * 40 + "\n\nplain " * 3], width=40)
    assert all(display_width(line) <= 40 for line in text.splitlines())


# ------------------------------------------------------------------ renderer
def renderer(**kwargs):
    return Renderer(io.StringIO(), color=False, **kwargs)


def test_a_reply_has_one_kova_line_and_tool_rows_in_kova_style():
    r = renderer()
    r.begin_turn()
    for event in ({"type": "reasoning", "text": "hmm"},
                  {"type": "text", "text": "Let me check."},
                  {"type": "tool_call", "name": "web_search", "args": {"query": "berlin"}},
                  {"type": "tool_result", "name": "web_search", "ok": True, "summary": "5 results"},
                  {"type": "tool_call", "name": "read_file", "args": {"path": "x"}},
                  {"type": "tool_result", "name": "read_file", "ok": False, "summary": "missing"},
                  {"type": "text", "text": "**Done**."},
                  {"type": "done"}):
        r.handle(event)
    r.finish_turn()
    text = r.out.getvalue()
    assert text.count("◆ Kova") == 1 and text.index("◆ Kova") < text.index("Thought for")
    assert "  ✓ Search(berlin) · 5 results" in text and "  ✗ Read(x) · missing" in text
    assert "Done." in text and "**" not in text
    assert "Worked for" in text


def test_command_rows_collapse_and_expand_full_output():
    r = renderer()
    r.begin_turn()
    output = "\n".join(f"line {n}" for n in range(1, 7))
    r.handle({"type": "tool_call", "name": "run_command", "args": {"command": "ls -la"}})
    r.handle({"type": "tool_result", "name": "run_command", "ok": True, "summary": output})
    r.tool_row("run_command", {"command": "false"}, False, "")
    text = r.out.getvalue()
    assert "  ✓ Bash(ls -la)" in text and "line 1" not in text
    assert "  ✗ Bash(false)" in text
    r.tools_expanded = True
    assert "line 6" in "\n".join(r.detail_lines())
    r.tools_expanded = False
    assert "line 6" not in "\n".join(r.detail_lines())
    assert command_output('{"output": "hi\\n", "exit_code": 2}') == "hi\n\nexit code 2"
    assert command_output("plain") == "plain"


def test_running_tools_show_name_and_arguments_in_the_live_region():
    r = renderer()
    r.begin_turn()
    r.handle({"type": "tool_call", "name": "web_search", "args": {"query": "weather berlin"}})
    live = plain("\n".join(r.live_lines(0)))
    assert "⠋ Search(weather berlin)" in live and "esc to interrupt" in live
    r.handle({"type": "tool_result", "name": "web_search", "ok": True, "summary": "ok"})
    r.handle({"type": "tool_call", "name": "run_command", "args": {"command": "git status"}})
    assert "⠋ Bash(git status)" in plain("\n".join(r.live_lines(0)))


def test_plan_is_a_checklist():
    r = renderer()
    r.begin_turn()
    r.handle({"type": "plan", "items": [{"text": "Inspect", "status": "done"},
                                        {"text": "Change", "status": "in_progress"},
                                        {"text": "Verify", "status": "pending"}]})
    text = r.out.getvalue()
    assert "◆ Plan 1/3\n    ✓ Inspect\n    ◐ Change\n    ○ Verify" in text


def test_permission_card_warns_before_destruction():
    title, body, _ = renderer().permission_card("run_command", {"command": "rm -rf build"},
                                                "destructive", "Delete the build folder")
    text = "\n".join(body)
    assert title == "Run a shell command?" and "rm -rf build" in text
    assert "Destructive" in text and "Delete the build folder" in text
    title, body, _ = renderer().permission_card("write_file", {"path": "a.txt"}, "normal", "")
    assert title == "Write a file?" and "Destructive" not in "\n".join(body)


# ------------------------------------------------------------------ the chat loop
class StubClient:
    """Just enough of KovaClient for ChatApp, with scripted replies."""

    def __init__(self, script=(), permissions=True):
        self.script = list(script)
        self.permissions = permissions
        self.chat = {"id": "c1", "mode": "work", "web": True, "messages": []}
        if permissions:
            self.chat.update(permission="manual", cwd=os.getcwd())
        self.approvals, self.stops, self.notes, self.sent = [], [], [], []

    def get_chat(self, _chat_id):
        return self.chat

    def update_chat(self, _chat_id, **fields):
        allowed = ("title", "mode", "web") + (("permission", "cwd") if self.permissions else ())
        self.chat.update({k: v for k, v in fields.items() if v is not None and k in allowed})
        return dict(self.chat)

    def stream_message(self, _chat_id, text):
        self.sent.append(text)
        yield from self.script

    def answer_approval(self, chat_id, approval_id, decision, message=None):
        self.approvals.append((approval_id, decision, message))
        return {"ok": True}

    def stop_chat(self, chat_id):
        self.stops.append(chat_id)
        return True

    def memory_add(self, title, body, tags=None):
        self.notes.append((title, body))
        return {}


def chat_app(client):
    return ChatApp(client, "c1", out=io.StringIO(), color=False, clear=False)


def drain(app, until=lambda app: app.job is None, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        app._pump()
        if until(app):
            return
        time.sleep(0.01)
    raise AssertionError("the chat loop never got there")


def transcript(app) -> str:
    app.screen.close()
    return plain(app.out.getvalue())


APPROVAL = {"type": "approval", "id": "a1", "tool": "run_command",
            "args": {"command": "git status"}, "risk": "normal", "summary": "Run git status"}


def test_an_approval_asks_and_yes_allows_it():
    client = StubClient([APPROVAL, {"type": "text", "text": "done"}, {"type": "done"}])
    app = chat_app(client)
    app.refresh_chat()
    app.submit("check the repo")
    drain(app, until=lambda app: app.approval is not None)
    labels = [label for label, _value, _key in app.overlay.options]
    assert labels == ["Allow once", "Allow this exact action for this session", "Always allow this exact action",
                      "No, and tell Kova what to do differently"]
    app.on_key(SimpleNamespace(name="enter", text=""))
    drain(app)
    assert client.approvals == [("a1", "allow", None)]
    assert "Allowed run_command" in transcript(app)


def test_destructive_actions_cannot_be_allowed_for_the_whole_chat():
    client = StubClient([{**APPROVAL, "risk": "destructive"}, {"type": "done"}])
    app = chat_app(client)
    app.submit("clean up")
    drain(app, until=lambda app: app.approval is not None)
    assert [value for _label, value, _key in app.overlay.options] == ["allow", "allow_session", "allow_always", "deny"]
    assert app.overlay.pick("a") == "allow_session"


def test_no_asks_what_to_do_instead_and_sends_it():
    client = StubClient([APPROVAL, {"type": "done"}])
    app = chat_app(client)
    app.submit("check")
    drain(app, until=lambda app: app.approval is not None)
    app.on_key(SimpleNamespace(name="text", text="n"))
    assert isinstance(app.overlay, TextPrompt)
    app.on_key(SimpleNamespace(name="text", text="use git log instead"))
    app.on_key(SimpleNamespace(name="enter", text=""))
    drain(app)
    assert client.approvals == [("a1", "deny", "use git log instead")]
    assert "Declined run_command · use git log instead" in transcript(app)


def test_esc_declines_without_asking():
    client = StubClient([APPROVAL, {"type": "done"}])
    app = chat_app(client)
    app.submit("check")
    drain(app, until=lambda app: app.approval is not None)
    app.on_key(SimpleNamespace(name="esc", text=""))
    drain(app)
    assert client.approvals == [("a1", "deny", None)] and app.overlay is None


def test_messages_typed_during_a_reply_queue_and_run_next():
    client = StubClient([{"type": "text", "text": "hi"}, {"type": "done"}])
    app = chat_app(client)
    app.submit("first")
    app.submit("second")
    assert app.queued == ["second"]
    drain(app, until=lambda app: app.job is None and not app.queued)
    drain(app)
    assert client.sent == ["first", "second"]


def test_stopping_calls_the_stop_endpoint_and_returns_the_queue_to_the_input():
    def slow():
        yield {"type": "text", "text": "partial"}
        while not client.stops:
            time.sleep(0.01)
        yield {"type": "stopped"}

    client = StubClient()
    client.stream_message = lambda _chat_id, _text: slow()
    app = chat_app(client)
    app.submit("long one")
    app.submit("waiting")
    app.on_key(SimpleNamespace(name="esc", text=""))
    drain(app)
    assert client.stops == ["c1"] and app.editor.text == "waiting" and not app.queued
    text = transcript(app)
    assert "partial" in text and text.count("Interrupted") == 1


def test_plan_events_feed_the_status_line():
    client = StubClient([{"type": "plan", "items": [{"text": "a", "status": "done"},
                                                    {"text": "b", "status": "pending"}]},
                         {"type": "done"}])
    app = chat_app(client)
    app.refresh_chat()
    app.submit("plan it")
    drain(app)
    assert "plan 1/2" in plain(app._status(120)) and "manual" in plain(app._status(120))


def test_shift_tab_cycles_permission_and_tolerates_an_older_server():
    client = StubClient()
    app = chat_app(client)
    app.refresh_chat()
    app.on_key(SimpleNamespace(name="shift-tab", text=""))
    assert app.permission == "auto" and client.chat["permission"] == "auto"
    app.on_key(SimpleNamespace(name="shift-tab", text=""))
    app.on_key(SimpleNamespace(name="shift-tab", text=""))
    assert app.permission == "manual"
    old = StubClient(permissions=False)
    app = chat_app(old)
    app.refresh_chat()
    app.on_key(SimpleNamespace(name="shift-tab", text=""))
    assert app.permission is None and "no permission modes" in app.flash_text


def test_slash_command_arguments_are_suggested():
    app = chat_app(StubClient())
    app._models = [{"name": "local", "model": "m1"}, {"name": "other", "model": "m2"}]
    app.model = "local"
    for typed, expected in (("/permissions ", {"manual", "auto", "full"}),
                            ("/mode ", {"chat", "work"}), ("/web ", {"on", "off"}),
                            ("/model ", {"local", "other"})):
        app.editor.set(typed)
        app._refresh_menu()
        assert {item.label for item in app.menu.items} == expected, typed


def test_pickers_open_without_an_argument():
    client = StubClient()
    client.models_list = lambda: [{"name": "local", "model": "m1"}, {"name": "other", "model": "m2"}]
    client.model_use = lambda name: {"ok": True}
    client.health = lambda: {"model": "other", "model_ok": True}
    client.list_chats = lambda: [{"id": "c1", "title": "First"}, {"id": "c2", "title": "Second"}]
    app = chat_app(client)
    for command, title in (("/model", "Select model"), ("/mode", "Select mode"),
                           ("/permissions", "Permissions"), ("/resume", "Resume a chat")):
        app.overlay = None
        app.submit(command)
        assert isinstance(app.overlay, Picker) and app.overlay.title == title, command
    app.overlay = None
    app.submit("/model")
    app.on_key(SimpleNamespace(name="text", text="oth"))
    app.on_key(SimpleNamespace(name="enter", text=""))
    assert app.model == "other" and "Now using other" in transcript(app)


def test_ctrl_p_palette_runs_commands_and_switches_models_and_chats():
    client = StubClient()
    models = [{"name": "local", "model": "fake-model", "context": 32768},
              {"name": "other", "model": "other-model", "context": 32768}]
    chats = [{"id": "c2", "title": "Trip plans", "mode": "work", "message_count": 3}]
    client.models_list = lambda: models
    client.list_chats = lambda: chats
    client.model_use = lambda _name: {"ok": True}
    client.health = lambda: {"model": "other", "model_ok": True}
    app = chat_app(client)
    app.refresh_chat()
    app._models, app._chats, app.model = models, chats, "local"
    app._sync_context_window()

    app.on_key(SimpleNamespace(name="ctrl-p", text=""))
    assert isinstance(app.overlay, CommandPalette)
    app.on_key(SimpleNamespace(name="text", text="hel"))
    app.on_key(SimpleNamespace(name="enter", text=""))
    assert app.overlay is None
    text = transcript(app)
    assert "Commands" in text and "ctrl+p" in text

    app.on_key(SimpleNamespace(name="ctrl-p", text=""))
    app.on_key(SimpleNamespace(name="text", text="mode"))
    app.on_key(SimpleNamespace(name="enter", text=""))
    assert isinstance(app.overlay, Picker) and app.overlay.title == "Select mode"
    app.on_key(SimpleNamespace(name="esc", text=""))

    app.on_key(SimpleNamespace(name="ctrl-p", text=""))
    app.on_key(SimpleNamespace(name="text", text="oth"))
    app.on_key(SimpleNamespace(name="enter", text=""))
    assert app.overlay is None and app.model == "other" and app.context_window == 32768

    app.on_key(SimpleNamespace(name="ctrl-p", text=""))
    app.on_key(SimpleNamespace(name="text", text="trip"))
    app.on_key(SimpleNamespace(name="enter", text=""))
    assert app.overlay is None and app.chat_id == "c2"

    app.on_key(SimpleNamespace(name="ctrl-p", text=""))
    app.on_key(SimpleNamespace(name="esc", text=""))
    assert app.overlay is None  # esc closes it quietly


def test_status_line_shows_tokens_context_fill_and_turn_time():
    app = chat_app(StubClient())
    app.model = "local"
    app.renderer.last_tokens, app.renderer.last_elapsed = 1500, 4.2
    app.context_window, app.context_chars = 100_000, 80_000       # 20K of 100K tokens
    line = plain(app._status(120))
    assert "1.5K tok" in line and "ctx 20%" in line and "4.2s" in line
    assert "local" in line

    app.job = SimpleNamespace()                                    # a live turn
    app.renderer.turn_started = time.monotonic() - 2
    app.renderer.pass_chars = 400                                  # about 100 more tokens
    live = plain(app._status(120))
    assert "100 tok" in live and "ctx 20%" in live and "2" in live
    app.job, app.renderer.turn_started, app.renderer.pass_chars = None, 0, 0

    app.context_chars = 100                                        # a tiny sliver
    assert "ctx <1%" in plain(app._status(120))
    for width in (120, 80, 70, 60, 40, 24):
        assert visible(app._status(width)) <= width - 1, width
    app.context_window = 0
    assert "ctx" not in plain(app._status(120))                    # no window, no claim


def test_shell_output_and_memory_notes_go_where_they_belong(tmp_path):
    client = StubClient([{"type": "done"}])
    app = chat_app(client)
    app.cwd = str(tmp_path)
    app.submit("!echo shell-ok")
    drain(app)
    app.submit("# I prefer short answers")
    app.submit("what did it print?")
    drain(app)
    assert client.notes == [("I prefer short answers", "I prefer short answers")]
    assert client.sent[0].startswith(f"I ran this shell command myself in {tmp_path}:\n"
                                     "$ echo shell-ok\nshell-ok")
    assert client.sent[0].endswith("what did it print?")
    text = transcript(app)
    assert "✓ Bash(echo shell-ok)" in text and "Saved to memory" in text


def test_run_shell_reports_exit_codes_and_can_be_cancelled(tmp_path):
    import threading

    assert run_shell("echo hi; exit 3", str(tmp_path), threading.Event()) == \
        {"ok": False, "output": "hi\n", "code": 3}
    cancel = threading.Event()
    cancel.set()
    started = time.monotonic()
    result = run_shell("sleep 10", str(tmp_path), cancel)
    assert not result["ok"] and "stopped" in result["output"]
    assert time.monotonic() - started < 5


def test_a_job_reports_errors_and_always_ends():
    def fail(_emit, _cancel):
        raise RuntimeError("boom")

    job = Job("reply", fail)
    assert job.events.get(timeout=5) is None and str(job.outcome["error"]) == "boom"


# ------------------------------------------------------------------ the plain chat
def test_piped_chat_declines_approvals_and_shows_plans():
    from kova.ui import _render_turn

    answered = []
    events = [{"type": "plan", "items": [{"text": "Look", "status": "in_progress"}]},
              APPROVAL,
              {"type": "tool_result", "name": "run_command", "ok": False, "summary": "Declined"},
              {"type": "done"}]
    client = SimpleNamespace(stream_message=lambda *_: (event for event in events),
                             answer_approval=lambda *args: answered.append(args))
    out = io.StringIO()
    _render_turn(client, "c1", "check", False, Console(file=out, width=80))
    text = out.getvalue()
    assert answered == [("c1", "a1", "deny", None)]
    assert "Update Todos" in text and "◐ Look" in text and "approvals need an interactive" in text
    assert "\x1b[" not in text


# ------------------------------------------------------------------ in a real terminal
@pytest.fixture
def kova_home(tmp_path):
    fake = FakeKova()
    home = tmp_path / ".kova"
    write_config(home, fake.port)
    project = tmp_path / "project"
    (project / "sub").mkdir(parents=True)
    (project / "notes.md").write_text("alpha\nbeta\n")
    yield fake, home, project
    fake.close()


IDLE = 'Try "'


@linux_only
def test_full_screen_chat_in_a_real_terminal(kova_home):
    fake, home, project = kova_home
    term = Terminal(home, project, "--new", columns=90, rows=30)
    try:
        term.screen(IDLE, 30)
        assert "model    local" in term.vt.text() and "mode     work" in term.vt.text()
        term.type(b"/mo")
        term.screen("List models; /model <name> switches")            # the suggestion menu
        term.type(b"\x15/model\r")
        term.screen("Select model")                                    # the picker
        term.type(b"\x1b[B\r")
        term.screen("Now using other")
        term.type(b"hello\r")
        term.screen("Hello from the fake server.")
        term.screen("Worked for")
        term.wait(lambda: IDLE in term.vt.text(), "the idle prompt")
        term.type(b"\x1b[Z")                                           # shift+tab
        term.screen("auto permissions")
        term.type(b"run it\r")
        term.screen("Do you want to proceed?")                         # the approval dialog
        assert "git status" in term.vt.text() and "exact action" in term.vt.text()
        term.type(b"1")
        term.screen("The tree is clean.")
        assert "✓ Report what changed" in term.vt.text()
        term.wait(lambda: IDLE in term.vt.text(), "the idle prompt")
        term.type(b"rm it\r")
        term.screen("Destructive")
        assert "don't ask again" not in term.vt.text()
        term.type(b"n")
        term.screen("What should Kova do instead?")
        term.type(b"use the trash\r")
        term.screen("Okay, I won't run it.")
        term.wait(lambda: IDLE in term.vt.text(), "the idle prompt")
        term.type(b"!echo shell-ok\r")
        term.screen("Bash  echo shell-ok")
        term.type(b"# I like tea\r")
        term.screen("Saved to memory")
        term.type(b"summarize @no")
        term.screen("@notes.md")                                       # file suggestions
        term.type(b"\t\r")
        term.screen("Read notes.md (2 lines)")
        term.wait(lambda: IDLE in term.vt.text(), "the idle prompt")
        term.type(b"slow\r")
        term.screen("tick1")
        term.type(b"queued one\r")
        term.screen("(queued)")
        term.type(b"\x1b")                                             # esc interrupts
        term.screen("Interrupted")
        term.screen("│ ❯ queued one")                                  # the queue comes back
        term.type(b"\x15/cd sub\r")
        term.screen("Folder:")
        term.type(b"?")
        term.screen("clear the input")
        term.type(b"?")
        term.gone("clear the input")
        term.type(b"\x04")
        status = term.exit_status()
        assert status == 0, "kova did not exit cleanly on ctrl+d"
    finally:
        term.close()
    history = term.history()
    assert "Bash  git status" in history and "shell-ok" in history and "Until next time." in history
    for transient in ("Select model", "Do you want to proceed?", "esc to interrupt", IDLE):
        assert transient not in history, transient
    assert [d["decision"] for d in fake.decisions] == ["allow", "deny"]
    assert fake.decisions[1]["message"] == "use the trash"
    assert fake.notes and fake.notes[0]["body"] == "I like tea"
    assert fake.stops == ["chat1"] and fake.active == "other"
    sent = dict(enumerate(text for _chat, text in fake.sent))
    assert any("$ echo shell-ok\nshell-ok" in text and '<file path="notes.md">' in text
               for text in sent.values())
    chat = fake.chats["chat1"]
    assert chat["permission"] == "auto" and chat["cwd"] == str(project / "sub")


@linux_only
@pytest.mark.parametrize("no_color", [False, True])
def test_command_palette_and_status_line_in_a_real_terminal(kova_home, no_color):
    fake, home, project = kova_home
    term = Terminal(home, project, "--new", columns=70, rows=30,
                    env={"NO_COLOR": "1"} if no_color else {})
    try:
        term.screen(IDLE, 30)
        term.type(b"\x10")                                             # ctrl+p
        term.screen("Command palette")
        term.type(b"model")
        term.screen("/model")
        term.type(b"\x1b")                                             # esc closes
        term.gone("Command palette")
        term.screen(IDLE)
        term.type(b"hello\r")
        term.screen("Hello from the fake server.")
        term.wait(lambda: IDLE in term.vt.text(), "the idle prompt")
        term.screen("tok")                                             # tokens used
        term.screen("ctx")                                             # context fill
        term.type(b"\x10")
        term.type(b"nothing-here")
        term.screen("Nothing matches")
        term.type(b"\x1b")
        term.gone("Nothing matches")
        term.type(b"\x04")
        assert term.exit_status() == 0
    finally:
        term.close()
    if no_color:
        assert not re.search(r"\x1b\[[0-9;]*m", term.raw.replace("\x1b[0m", ""))


@linux_only
def test_older_server_without_permissions_and_no_color(tmp_path):
    fake = FakeKova(permissions=False)
    home = tmp_path / ".kova"
    write_config(home, fake.port)
    term = Terminal(home, tmp_path, "--new", columns=80, rows=24, env={"NO_COLOR": "1"})
    try:
        term.screen(IDLE, 30)
        term.type(b"\x1b[Z")
        term.screen("no permission modes yet")
        term.type(b"hello\r")
        term.screen("Hello from the fake server.")
        term.wait(lambda: IDLE in term.vt.text(), "the idle prompt")
        term.type(b"\x03")
        term.screen("Press Ctrl+C again to exit")
        term.type(b"\x03")
        status = term.exit_status()
        assert status == 0
    finally:
        term.close()
        fake.close()
    assert not re.search(r"\x1b\[[0-9;]*m", term.raw.replace("\x1b[0m", "")), "colour with NO_COLOR"


@linux_only
def test_kova_simple_keeps_the_plain_prompt(kova_home):
    fake, home, project = kova_home
    term = Terminal(home, project, "--new", env={"KOVA_SIMPLE": "1"})
    try:
        term.screen("/help commands", 30)
        term.type(b"hello\r")
        term.screen("Hello from the fake server.")
        # The line prompt asks the terminal where its cursor is; answer like one.
        term.wait(lambda: term.raw.count("\x1b[6n") >= 2, "the next prompt")
        term.type(b"\x1b[1;1R")
        time.sleep(0.3)
        term.type(b"\x04")
        status = term.exit_status()
        assert status == 0
    finally:
        term.close()
    assert "╭" not in term.history()


def test_live_reasoning_and_partial_tool_arguments_toggle_without_transcript_spam():
    import json
    from kova.tui.render import partial_arguments
    r = renderer()
    r.begin_turn()
    r.handle({"type": "reasoning", "text": "private thought"})
    assert "private thought" not in "\n".join(r.detail_lines())
    r.verbose = True
    assert "private thought" in "\n".join(r.detail_lines())
    r.verbose = False
    assert "private thought" not in "\n".join(r.detail_lines())
    raw = json.dumps({"path": "demo.py", "content": 'print("hi")\n'})
    for i in range(1, len(raw) + 1):
        r.handle({"type": "tool_call_delta", "index": 0, "name": "write_file", "arguments": raw[:i]})
        assert "\x1b" not in "\n".join(r.detail_lines())
    assert partial_arguments(raw)["content"] == 'print("hi")\n'
    assert "demo.py" in "\n".join(r.detail_lines())
    assert 'print("hi")' in "\n".join(r.detail_lines())
    assert 'print("hi")' not in r.out.getvalue()
    r.handle({"type": "tool_call", "name": "write_file", "args": {}})
    assert not r.tool_drafts


def test_toggle_control_keys(keys):
    assert keys(b"\x14\x0f") == [("ctrl-t", ""), ("ctrl-o", "")]
    assert keys(b"\x10\x0e") == [("ctrl-p", ""), ("ctrl-n", "")]


@linux_only
@pytest.mark.parametrize("no_color", [False, True])
def test_live_details_in_narrow_pty(kova_home, no_color):
    fake, home, project = kova_home
    term = Terminal(home, project, "--new", columns=55, rows=30,
                    env={"NO_COLOR": "1"} if no_color else {})
    try:
        term.screen(IDLE, 30)
        term.type(b"\x14details\r")
        term.screen("reasoning-visible-marker")
        term.type(b"\x14")
        term.gone("reasoning-visible-marker")
        term.screen("demo.py")
        term.screen("live-marker")
        term.screen("details done")
        term.type(b"\x0f")
        term.screen("full-output-marker")
        term.type(b"\x0f")
        term.gone("full-output-marker")
        term.gone("generating")
        term.type(b"\x04")
        assert term.exit_status() == 0
    finally:
        term.close()


def test_permissions_rules_editor_can_save_and_cancel():
    client=StubClient()
    saved=[]
    client.permission_rules=lambda: []
    client.replace_permission_rules=lambda rules: saved.append(rules)
    app=chat_app(client)
    app.submit('/permissions rules')
    assert isinstance(app.overlay, TextPrompt)
    app.overlay.value='[{"action":"ask","tool":"run_command","pattern":"git *"}]'
    app.on_key(SimpleNamespace(name='enter',text=''))
    assert saved[0][0]['action']=='ask'
    app.submit('/permissions rules')
    app.on_key(SimpleNamespace(name='esc',text=''))
    assert len(saved)==1


def test_key_remapping_swaps_actions_without_leaving_old_binding():
    from kova.tui.terminal import Key
    from kova.tui.keymap import Keymap
    keys = Keymap({'palette': 'ctrl-t', 'thinking': 'ctrl-p'})
    assert keys.translate(Key('ctrl-t')).name == 'ctrl-p'
    assert keys.translate(Key('ctrl-p')).name == 'ctrl-t'
    assert keys.label('palette') == 'ctrl-t'
    with pytest.raises(ValueError):
        Keymap({'palette': 'ctrl-t'})
