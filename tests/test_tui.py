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
from kova.tui.style import ERROR, WARNING
from kova.tui.render import Renderer, command_output
from kova.tui.terminal import KeyReader, Screen
from kova.tui.widgets import (
    Choice,
    CommandPalette,
    Picker,
    input_box,
    menu,
    shimmer,
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
    if os.name == "nt":
        pytest.skip("POSIX pipe key reader; Windows uses its console input path")
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
    if os.name != "nt":
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
    (tmp_path / "notes.md").write_bytes(b"alpha\nbeta\n")
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
    erase = re.search(r"\x1b\[J\x1b\[A\r(\x1b\[\d+C)?\x1b\[0;1m", last)
    assert erase and erase.start() < last.index("still bold")


def test_redraws_never_erase_from_the_top_left_corner():
    # tmux treats an erase-to-end from row 0, column 0 as clearing the screen and
    # copies the whole screen into its scrollback (scroll-on-clear): every frame
    # of the live region would pile up there. The erase starts one row down.
    out = FakeTerminal()
    chunks = []
    out.write = chunks.append
    screen = Screen(out, size_fn=lambda: out.size)
    screen.set_live(["a", "b", "c"])
    screen.render()
    screen.set_live(["a", "b", "d"])
    screen.render()
    assert chunks[-1].count("\x1b[J") == 1 and "\r\x1b[K\x1b[B\r\x1b[J\x1b[A\r" in chunks[-1]
    term = VT(20, 6)
    term.feed("".join(chunks))
    assert term.lines()[:3] == ["a", "b", "d"]


def test_untrusted_text_cannot_steer_the_terminal():
    assert clean("ok\x1b[2J\x1b]0;pwned\x07\x9b31m done\n") == "ok[2J]0;pwned31m done\n"


# ------------------------------------------------------------------ widgets
def test_input_box_places_the_caret_on_wrapped_and_prefixed_text():
    p = Palette(False)
    lines, caret = input_box("x" * 50, 50, 30, p)
    term = VT(40, 10)
    term.feed("\r\n".join(lines))
    assert caret == (2, 25) and term.line(2).startswith("  xxx") and term.line(0) == "─" * 30
    lines, caret = input_box("!ls -la", 7, 30, p)
    assert lines[1].startswith("! ls -la") and caret == (1, 8)
    lines, caret = input_box("", 0, 30, p, placeholder='Try "x"', footer="esc stop")
    assert 'Try "x"' in lines[1] and lines[1].startswith("▌ ") and caret == (1, 2)
    assert lines[-1].endswith(" esc stop ─") and len(lines[-1]) == 30


def test_menu_dialog_and_picker_render():
    p = Palette(False)
    shown = menu([Suggestion("/model ", "/model", "Choose"), Suggestion("/mode ", "/mode", "")],
                 1, 40, p)
    assert shown == ["    /model  Choose", "  ▸ /mode"]
    choice = Choice("Kova wants to run a command", ["$ rm -rf x"],
                    [("allow once", "allow", "y"), ("no", "deny", "n")], footer="esc decline")
    assert choice.pick("2") == "deny" and choice.pick("y") == "allow" and choice.pick("z") is None
    lines = choice.render(40, p)
    assert lines[0] == "◆ Kova wants to run a command"
    assert "┃ $ rm -rf x" in lines and "┃ ▸ y  allow once" in lines and "┃   n  no" in lines
    assert lines[-1].endswith("esc decline ─") and all(visible(line) <= 40 for line in lines)
    picker = Picker("Select model", [("alpha", "", "a"), ("beta", "", "b")], None, current="b")
    assert picker.value == "b"
    rendered = "\n".join(picker.render(40, p))
    assert "── Select model" in rendered and "current" in rendered and "╭" not in rendered
    picker.type("alp")
    assert picker.value == "a" and "filter  alp" in "\n".join(picker.render(40, p))


def test_command_palette_filters_commands_models_and_chats():
    items = [("/model", "List models; /model <name> switches", ("command", "/model")),
             ("local", "fake-model \u00b7 current", ("model", "local")),
             ("Trip plans", "work \u00b7 3 messages", ("chat", "c2"))]
    groups = {("command", "/model"): "Commands", ("model", "local"): "Models",
              ("chat", "c2"): "Chats"}
    palette = CommandPalette(items, lambda _value: None, groups=groups)
    text = "\n".join(palette.render(80, Palette(False)))
    assert "Commands" in text and "Models" in text and "Chats" in text
    assert "COMMANDS" not in text                        # group names stay in sentence case
    assert "▸ /model" in text and "List models" in text

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


def test_live_node_and_welcome_read_plainly_without_colour():
    p = Palette(False)
    assert shimmer("Thinking", 3, p) == "Thinking"
    coloured = shimmer("Thinking", 3, Palette(True))
    assert coloured.count("\x1b[38;") == len("Thinking")
    r = renderer()
    r.model = "local-model"
    r.begin_turn()
    live = r.live_lines(0, now=r.turn_started + 12)
    assert len(live) == 1 and "\x1b" not in live[0]
    assert live[0].startswith("◐ waiting") and "for local-model" in live[0] and live[0].endswith("12s")
    card = "\n".join(welcome(p, 80, version="1.0.0", model="local", mode="work", cwd="/p",
                             memory="~/notes", notes=2, offline=True, permission="manual"))
    assert "model    local  offline" in card and "mode     work" in card
    assert "asks     before acting" in card
    assert "Every step shows up here" in card and "2 notes" in card and "K  Kova" in card
    assert "not answering" in card and "\x1b" not in card
    assert "╭" not in card and "█▄▀" not in card            # no frame, no wordmark
    narrow = welcome(p, 34, version="1.0.0", model="local", mode="work", cwd="/p")
    assert "K  Kova" in "\n".join(narrow) and all(len(line) <= 34 for line in narrow)


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


def test_a_reply_has_one_kova_line_and_tool_rows_in_kova_style(monkeypatch):
    ticks = iter(range(1, 100))
    monkeypatch.setattr(time, "monotonic", lambda: next(ticks))
    r = renderer()
    r.begin_turn()
    for event in ({"type": "reasoning", "text": "hmm. Then more."},
                  {"type": "text", "text": "Let me check."},
                  {"type": "tool_call", "name": "web_search", "args": {"query": "berlin"}},
                  {"type": "tool_result", "name": "web_search", "ok": True,
                   "output": '[{"title": "a"}, {"title": "b"}]'},
                  {"type": "tool_call", "name": "read_file", "args": {"path": "x"}},
                  {"type": "tool_result", "name": "read_file", "ok": False, "summary": "Error: missing"},
                  {"type": "text", "text": "**Done**."},
                  {"type": "done"}):
        r.handle(event)
    r.finish_turn()
    text = r.out.getvalue()
    lines = text.splitlines()
    assert text.count("K Kova") == 1 and text.index("K Kova") < text.index("∴ thought")
    thought = next(line for line in lines if line.startswith("∴ thought"))
    assert "hmm." in thought and "Then more" not in thought and thought.endswith("1.0s")
    search = next(line for line in lines if line.startswith("▹ search"))
    assert "berlin" in search and "2 results" in search and search.endswith("s")
    failed = next(line for line in lines if line.startswith("✗ read"))
    assert " x " in failed and "missing" in failed
    assert "Done." in text and "**" not in text
    assert lines[-2].strip().endswith("s") and "Worked" not in text and "\u00b7" not in text


def test_command_rows_collapse_and_expand_full_output():
    r = renderer()
    r.begin_turn()
    output = "\n".join(f"line {n}" for n in range(1, 7))
    r.handle({"type": "tool_call", "name": "run_command", "args": {"command": "ls -la"}})
    r.handle({"type": "tool_result", "name": "run_command", "ok": True, "summary": output})
    r.tool_row("run_command", {"command": "false"}, False, "")
    text = r.out.getvalue()
    assert "▸ run       ls -la" in text and "✗ run       false" in text
    # The last lines of a command's output stay under its row, on the spine.
    assert "│           line 6" in text and "line 1" not in text and "3 earlier lines" in text
    r.tools_expanded = True
    assert "line 1" in "\n".join(r.detail_lines())
    r.tools_expanded = False
    assert "line 1" not in "\n".join(r.detail_lines())
    assert command_output('{"output": "hi\\n", "exit_code": 2}') == "hi\n\nexit code 2"
    assert command_output("plain") == "plain"


def test_running_tools_show_name_and_arguments_in_the_live_region():
    r = renderer()
    r.begin_turn()
    r.handle({"type": "tool_call", "name": "web_search", "args": {"query": "weather berlin"}})
    live = plain("\n".join(r.live_lines(0)))
    assert live.startswith("◐ search    weather berlin") and "searching" in live
    r.handle({"type": "tool_result", "name": "web_search", "ok": True, "summary": "ok"})
    r.handle({"type": "tool_call", "name": "run_command", "args": {"command": "git status"}})
    assert plain("\n".join(r.live_lines(1))).startswith("◓ run       git status")


def test_plan_is_a_checklist():
    r = renderer()
    r.begin_turn()
    items = [{"text": "Inspect", "status": "done"}, {"text": "Change", "status": "in_progress"},
             {"text": "Verify", "status": "pending"}]
    r.handle({"type": "plan", "items": items})
    text = r.out.getvalue()
    assert "≡ plan      1 of 3 done\n│           ✓ Inspect\n│           ▸ Change\n│           ◦ Verify" in text
    # The same plan again is one row: progress and the current step.
    items[1]["status"], items[2]["status"] = "done", "in_progress"
    r.handle({"type": "plan", "items": items})
    assert r.out.getvalue().splitlines()[-1] == "≡ plan      2 of 3 done   ✓ Change"


def test_permission_card_warns_before_destruction():
    title, body, colour = renderer().permission_card("run_command", {"command": "rm -rf build"},
                                                     "destructive", "Delete the build folder",
                                                     where="~/projects/demo")
    text = "\n".join(body)
    assert title == "Kova wants to run a command" and "$ rm -rf build" in text
    assert "delete data" in text and "Delete the build folder" in text
    assert "in ~/projects/demo" in text and colour == ERROR
    title, body, colour = renderer().permission_card("write_file", {"path": "a.txt"}, "normal", "")
    assert title == "Kova wants to write a file" and "delete data" not in "\n".join(body)
    assert colour == WARNING


def test_edit_approval_shows_the_change_with_its_indentation():
    _, body, _ = renderer().permission_card(
        "edit_file", {"path": "src/p.py", "old_text": "    return x\n",
                      "new_text": "    if x:\n        return x\n"}, "normal", "edit_file: src/p.py")
    assert body[0] == "src/p.py"
    assert "−     return x" in body and "+     if x:" in body and "+         return x" in body
    assert not any(line.startswith("edit_file") for line in body)


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
        self.queues: list[dict] = []
        self.steers: list[str] = []
        self.created: list[dict] = []
        self.checkpoints: list[dict] = []
        self.diffs: dict[tuple[int, str], dict] = {}
        self.restores: list[int] = []

    def get_chat(self, _chat_id):
        return self.chat

    def create_chat(self, title=None, mode=None, web=None, permission=None, cwd=None):
        chat = {"id": f"c{len(self.created) + 2}", "title": title or "",
                "mode": mode or "work", "web": True if web is None else web,
                "permission": permission or "manual", "cwd": cwd or os.getcwd(),
                "messages": []}
        self.created.append(chat)
        self.chat = chat
        return dict(chat)

    def list_chats(self):
        return [dict(self.chat)] + [dict(chat) for chat in self.created[:-1]]

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

    def queue_message(self, _chat_id, text, model=None):
        row = {"id": len(self.queues) + 1, "text": text, "model": model}
        self.queues.append(row)
        return dict(row)

    def queued_messages(self, _chat_id):
        return [dict(row) for row in self.queues]

    def next_queued_message(self, _chat_id):
        return self.queues.pop(0) if self.queues else None

    def delete_queued_message(self, _chat_id, message_id):
        return bool(self.queues) and self.queues.pop(0).get("id") == message_id

    def steer_message(self, _chat_id, text):
        self.steers.append(text)
        return {"ok": True}

    def list_checkpoints(self, _chat_id):
        return [dict(entry) for entry in self.checkpoints]

    def checkpoint_diff(self, _chat_id, checkpoint_id, path):
        return self.diffs.get((checkpoint_id, path),
                              {"path": path, "diff": "", "truncated": False})

    def restore_checkpoint(self, _chat_id, checkpoint_id):
        self.restores.append(checkpoint_id)
        return {"ok": True, "checkpoint_id": checkpoint_id, "root": "/p",
                "restored": ["notes.txt"], "deleted": ["new.txt"], "failed": []}


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
    assert labels == ["allow once", "allow this exact action for this session",
                      "always allow this exact action", "no, and tell Kova what to do instead"]
    assert [key for _label, _value, key in app.overlay.options] == ["y", "a", "w", "n"]
    screen = plain("\n".join(app.overlay.render(80, app.p)))
    assert "Kova wants to run a command" in screen and "in " in screen
    app.on_key(SimpleNamespace(name="enter", text=""))
    drain(app)
    assert client.approvals == [("a1", "allow", None)]
    assert re.search(r"◆ allowed\s+run git status\s+once", transcript(app))


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
    assert re.search(r"◆ declined\s+run git status\s+use git log instead", transcript(app))


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


def test_a_queued_message_is_persisted_on_the_server():
    client = StubClient([{"type": "text", "text": "hi"}, {"type": "done"}])
    app = chat_app(client)
    app.submit("first")
    app.submit("second")
    assert [row["text"] for row in client.queues] == ["second"]
    drain(app, until=lambda app: app.job is None and not app.queued)
    drain(app)
    assert client.queues == [] and client.sent == ["first", "second"]


def test_queued_messages_survive_a_reload():
    client = StubClient()
    client.queues = [{"id": 4, "text": "waiting here", "model": None}]
    app = chat_app(client)
    app.refresh_chat()
    assert app.queued == ["waiting here"]


def test_steering_reaches_the_running_reply():
    def slow():
        yield {"type": "text", "text": "working"}
        while not client.steers:
            time.sleep(0.01)
        yield {"type": "steer", "text": client.steers[0]}
        yield {"type": "done"}

    client = StubClient()
    client.stream_message = lambda _chat_id, _text: slow()
    app = chat_app(client)
    app.submit("start")
    app.command("/steer use the other file")
    drain(app)
    assert client.steers == ["use the other file"]
    text = transcript(app)
    assert "↳ you       use the other file" in text
    assert "No reply is running" not in text


def test_steering_outside_a_reply_explains_instead_of_sending():
    client = StubClient()
    app = chat_app(client)
    app.command("/steer too late")
    assert client.steers == []
    assert "No reply is running" in transcript(app)


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
    assert "partial" in text and text.count("■ stopped") == 1


def test_a_stopped_group_is_drawn_before_the_stopped_row():
    r = renderer()
    r.begin_turn()
    r.handle({"type": "task_start", "id": "g", "label": "2 reads at once",
              "kind": "parallel", "total": 2})
    r.handle({"type": "task_start", "id": "t1", "parent": "g", "name": "read_file",
              "args": {"path": "a.py"}})
    r.handle({"type": "task_start", "id": "t2", "parent": "g", "name": "read_file",
              "args": {"path": "b.py"}})
    r.handle({"type": "task_done", "id": "t1", "ok": True, "summary": '{"content": "x"}'})
    r.handle({"type": "task_error", "id": "t2", "message": "stopped", "stopped": True})
    r.handle({"type": "stopped"})
    r.finish_turn(stopped=True)
    lines = r.out.getvalue().splitlines()
    group = next(index for index, line in enumerate(lines) if "reads at once" in line)
    stopped = next(index for index, line in enumerate(lines) if "■ stopped" in line)
    assert group < stopped and "■ read    b.py" in "\n".join(lines)


def test_plan_events_feed_the_status_line():
    client = StubClient([{"type": "plan", "items": [{"text": "a", "status": "done"},
                                                    {"text": "b", "status": "pending"}]},
                         {"type": "done"}])
    app = chat_app(client)
    app.refresh_chat()
    app.submit("plan it")
    drain(app)
    assert "plan 1/2" in plain(app._status(120)) and "asks before acting" in plain(app._status(120))


def test_the_plan_is_pinned_above_the_input_while_a_turn_runs():
    # docs/TERMINAL-DESIGN.md: one row, the current step and a progress bar.
    app = chat_app(StubClient([]))
    app.plan = [{"text": "Look", "status": "done"},
                {"text": "Fix the parser and re-run the whole suite", "status": "in_progress"},
                {"text": "Report", "status": "pending"}]
    line = plain(app._pinned_plan(80))
    assert line.startswith("≡ 1 of 3  Fix the parser") and line.endswith("▰▱▱")
    assert len(line) <= 80
    narrow = plain(app._pinned_plan(40))
    assert narrow.startswith("≡ 1 of 3  ") and narrow.endswith("  ▰▱▱") and len(narrow) <= 40
    app.plan = []
    assert plain(app._pinned_plan(80)) == "≡ 0 of 0"


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
    assert app.model == "other" and "Now using m2 on Custom" in transcript(app)


def test_provider_is_an_alias_for_the_model_command():
    client = StubClient()
    state = {"model": "local/qwen3:8b"}
    client.models_list = lambda: [
        {"name": "openai/gpt-4o", "model": "gpt-4o", "provider": "openai", "provider_name": "OpenAI"},
        {"name": "local/qwen3:8b", "model": "qwen3:8b", "provider": "local", "provider_name": "Local server"},
    ]
    client.model_use = lambda name: state.update(model=name) or {"ok": True}
    client.health = lambda: {"model": state["model"], "model_ok": True}
    app = chat_app(client)
    app.submit("/provider")
    assert isinstance(app.overlay, Picker) and app.overlay.title == "Select model"
    app.submit("/provider openai/gpt-4o")
    assert state["model"] == "openai/gpt-4o" and app.model == "openai/gpt-4o"
    assert "Now using gpt-4o on OpenAI" in transcript(app)


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
    assert re.search(r"▸ run\s+echo shell-ok\s+exit 0", text)
    assert "│           shell-ok" in text and "▸ saved     I prefer short answers" in text


def test_run_shell_reports_exit_codes_and_can_be_cancelled(tmp_path):
    import threading

    assert run_shell("echo hi& exit 3" if os.name == "nt" else "echo hi; exit 3", str(tmp_path), threading.Event()) == \
        {"ok": False, "output": "hi\n", "code": 3}
    cancel = threading.Event()
    cancel.set()
    started = time.monotonic()
    result = run_shell("ping -n 11 127.0.0.1 > nul" if os.name == "nt" else "sleep 10", str(tmp_path), cancel)
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
    assert "≡ plan      0 of 1 done" in text and "│           ▸ Look" in text
    assert "approvals need an interactive" in text
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
        assert "∴ thought" in term.vt.text()
        term.wait(lambda: IDLE in term.vt.text(), "the idle prompt")
        term.type(b"\x1b[Z")                                           # shift+tab
        term.screen("asks before risky steps")
        term.type(b"run it\r")
        term.screen("Kova wants to run a command")                     # the approval gate
        term.screen("git status")                                      # drawn after the title
        term.screen("exact action")
        term.type(b"1")
        term.screen("The tree is clean.")
        term.screen("✓ Report what changed")
        term.wait(lambda: IDLE in term.vt.text(), "the idle prompt")
        term.type(b"rm it\r")
        term.screen("delete data")
        assert "don't ask again" not in term.vt.text()
        term.type(b"n")
        term.screen("What should Kova do instead?")
        term.type(b"use the trash\r")
        term.screen("Okay, I won't run it.")
        term.wait(lambda: IDLE in term.vt.text(), "the idle prompt")
        term.type(b"!echo shell-ok\r")
        term.screen("run       echo shell-ok")
        term.type(b"# I like tea\r")
        term.screen("saved     I like tea")
        term.type(b"summarize @no")
        term.screen("@notes.md")                                       # file suggestions
        term.type(b"\t\r")
        term.screen("attached notes.md  2 lines")
        term.wait(lambda: IDLE in term.vt.text(), "the idle prompt")
        term.type(b"slow\r")
        term.screen("tick1")
        term.type(b"queued one\r")
        term.screen("queued one  queued")
        term.type(b"\x1b")                                             # esc interrupts
        term.screen("■ stopped")
        term.screen("▌ queued one")                                    # the queue comes back
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
    assert re.search(r"◆ allowed\s+run git status", history) and "shell-ok" in history
    assert "Until next time." in history
    for transient in ("Select model", "Kova wants to run a command", "esc stop", IDLE):
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
        term.screen("Press ctrl+c again to exit")
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
        term.screen("/help lists every command", 30)
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
        term.gone("writing its arguments")
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


def test_keymap_shows_bindings_the_way_keys_are_written():
    from kova.tui.keymap import Keymap
    keys = Keymap()
    assert keys.pretty('clear') == 'ctrl+l' and keys.pretty('shortcuts') == '?'
    assert keys.pretty('palette') == 'ctrl+p' and keys.pretty('permissions') == 'shift+tab'
    # Help text written the default way follows a remap; names stay readable.
    assert keys.format_help("ctrl+l") == "ctrl+l"
    assert Keymap({'clear': 'ctrl-g'}).format_help("ctrl+l") == "ctrl+g"
    assert Keymap({'cancel': 'ctrl-r'}).format_help("esc again") == "ctrl+r again"


def test_remapped_key_toggles_shortcuts_and_question_still_types_in_a_picker():
    from kova.tui.keymap import Keymap
    from kova.tui.terminal import Key
    client = StubClient()
    client.list_chats = lambda: [{"id": "c1", "title": "First"}, {"id": "c2", "title": "Second"}]
    app = chat_app(client)
    app.keys = Keymap({'shortcuts': 'ctrl-g'})
    app.on_key(Key("text", "?"))
    assert app.editor.text == "?"          # unbound from shortcuts, so it is text
    app.on_key(Key("ctrl-g"))
    assert app.show_shortcuts
    app.on_key(Key("esc"))
    app.on_key(Key("ctrl-p"))
    app.keys = Keymap()                     # default shortcuts, over the palette
    app.on_key(Key("ctrl-g"))
    assert isinstance(app.overlay, Picker)
    app.on_key(Key("text", "?"))            # '?' filters; it never toggles a panel here
    assert app.overlay.query == "?"
    app.on_key(Key("esc"))


def test_session_picker_forks_the_highlighted_chat():
    client = StubClient()
    client.list_chats = lambda: [{"id": "c1", "title": "First", "mode": "work", "message_count": 2},
                                 {"id": "c2", "title": "Second", "mode": "chat", "message_count": 1}]
    forks = []

    def fork(chat_id):
        forks.append(chat_id)
        return {"id": "f" + chat_id, "title": "Second (fork)", "mode": "chat", "messages": []}
    client.fork_chat = fork
    app = chat_app(client)
    app.submit("/resume")
    assert isinstance(app.overlay, Picker) and app.overlay.actions
    app.on_key(SimpleNamespace(name="down", text=""))          # highlight the second chat
    app.on_key(SimpleNamespace(name="ctrl-s", text=""))        # fork it
    assert forks == ["c2"] and app.chat_id == "fc2"
    assert "Forked" in transcript(app)


def test_fork_command_copies_the_current_chat_and_a_numbered_one():
    client = StubClient()
    chats = [{"id": "c1", "title": "First", "mode": "work", "message_count": 2},
             {"id": "c2", "title": "Second", "mode": "work", "message_count": 0}]
    client.list_chats = lambda: chats
    forks = []

    def fork(chat_id):
        forks.append(chat_id)
        return {"id": "f" + chat_id, "title": "Forked", "mode": "work", "messages": []}
    client.fork_chat = fork
    app = chat_app(client)
    app.submit("/fork")
    assert forks == ["c1"] and app.chat_id == "fc1"
    app.submit("/fork 2")
    assert forks == ["c1", "c2"] and app.chat_id == "fc2"
    app.submit("/fork nope")
    assert "No chat matches" in transcript(app)
    assert forks == ["c1", "c2"]

# ------------------------------------------------- per-turn change review
CHANGE_SET = {"id": 7, "label": "fix the notes", "root": "/p", "created": "2026-11-13T09:00:00Z",
              "files": {"notes.txt": {"status": "modified", "additions": 3, "deletions": 1}}}


def test_a_turn_that_changed_files_prints_one_summary_line():
    client = StubClient([{"type": "review", "checkpoint_id": 7, "label": "fix the notes",
                          "files": [{"path": "notes.txt", "status": "modified",
                                     "additions": 3, "deletions": 1}]},
                         {"type": "text", "text": "Done"}, {"type": "done"}])
    app = chat_app(client)
    app.submit("edit the notes")
    drain(app)
    text = transcript(app)
    assert re.search(r"▸ changed\s+1 file  \+3 −1   /changes to review", text)


def test_changes_lists_every_change_set():
    client = StubClient()
    client.checkpoints = [CHANGE_SET,
                          {"id": 3, "label": "add a file", "root": "/p", "created": "2026-11-13T08:00:00Z",
                           "files": {"new.txt": {"status": "added", "additions": 2, "deletions": 0}}}]
    app = chat_app(client)
    app.command("/changes")
    text = transcript(app)
    assert "Changes  2 change sets" in text
    assert "fix the notes" in text and "+3 −1" in text
    assert "+2 −0" in text and "/diff <n> shows one" in text


def test_diff_prints_the_unified_diff_of_one_change_set():
    client = StubClient()
    client.checkpoints = [CHANGE_SET]
    client.diffs[(7, "notes.txt")] = {
        "path": "notes.txt", "status": "modified", "additions": 3, "deletions": 1,
        "truncated": False, "diff": "--- a/notes.txt\n+++ b/notes.txt\n@@ -1 +1 @@\n-one\n+ONE"}
    app = chat_app(client)
    app.command("/diff")
    text = transcript(app)
    assert "Change set 7" in text and "fix the notes" in text
    assert "notes.txt" in text and "-one" in text and "+ONE" in text


def test_restore_asks_before_putting_the_files_back():
    client = StubClient()
    client.checkpoints = [CHANGE_SET]
    app = chat_app(client)
    app.command("/restore 7")
    assert client.restores == [] and isinstance(app.overlay, Picker)
    assert app.overlay.title == "Restore change set 7?"
    assert "notes.txt" in "\n".join(transcript(app).splitlines()[:6] or [""])
    app.on_key(SimpleNamespace(name="enter", text=""))
    assert client.restores == [7]
    text = transcript(app)
    assert "Restored change set 7" in text
    assert "put back notes.txt" in text and "removed new.txt" in text


def test_restore_can_be_cancelled():
    client = StubClient()
    client.checkpoints = [CHANGE_SET]
    app = chat_app(client)
    app.command("/restore 7")
    app.on_key(SimpleNamespace(name="down", text=""))
    app.on_key(SimpleNamespace(name="enter", text=""))
    assert client.restores == []
    assert "Cancelled" in transcript(app)


def test_an_unknown_change_set_is_reported():
    client = StubClient()
    client.checkpoints = [CHANGE_SET]
    app = chat_app(client)
    app.command("/diff 99")
    assert "No change set '99'" in transcript(app)


def test_new_chat_is_created_only_by_the_first_message():
    """Opening Kova and /new must not leave empty conversations (#100)."""
    client = StubClient([{"type": "text", "text": "Hi"}, {"type": "done"}])
    app = ChatApp(client, None, out=io.StringIO(), color=False, clear=False)
    app.refresh_chat()
    app.command("/mode chat")
    app.command("/web off")
    for _ in range(3):
        app.command("/new")
    assert client.created == []
    assert app.chat_id is None
    app.submit("hello")
    drain(app)
    assert len(client.created) == 1
    created = client.created[0]
    assert app.chat_id == created["id"]
    assert (created["mode"], created["web"]) == ("chat", False)
    assert client.sent == ["hello"]
