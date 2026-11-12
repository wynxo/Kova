"""Consumer-visible chat rendering, including terminal overflow and cancellation."""

import io
from types import SimpleNamespace

import pytest
from rich.console import Console

from kova.client import KovaError
from kova.ui import _render_turn


def render(events, *, no_think=False, show_thinking=False, terminal=False, clock=None, monkeypatch=None):
    def stream(*args):
        yield from events

    if clock is not None:
        monkeypatch.setattr("kova.ui.perf_counter", lambda: next(clock))
    out = io.StringIO()
    console = Console(file=out, width=80, height=8, record=True, force_terminal=terminal,
                      color_system="truecolor" if terminal else None, no_color=False)
    _render_turn(SimpleNamespace(stream_message=stream), "chat", "question", no_think, console,
                 show_thinking=show_thinking)
    return console.export_text(), out.getvalue()


def test_markdown_reply_formats_bold_lists_and_code():
    rendered, raw = render([
        {"type": "text", "text": "**Important**\n\n- First item\n- Second item\n\n```python\n"},
        {"type": "text", "text": "print('hello')\n```"},
        {"type": "done"},
    ])
    assert "Important" in rendered and "**Important**" not in rendered
    assert "• First item" in rendered and "• Second item" in rendered
    assert "print('hello')" in rendered and "```" not in rendered
    assert "\x1b[" not in raw


@pytest.mark.parametrize("no_think,show_thinking,details,summary", [
    (False, False, False, True),
    (False, True, True, True),
    (True, True, False, False),
])
def test_reasoning_visibility_and_elapsed_time(no_think, show_thinking, details, summary, monkeypatch):
    rendered, _ = render([
        {"type": "reasoning", "text": "Private working"},
        {"type": "text", "text": "Final answer"},
        {"type": "done"},
    ], no_think=no_think, show_thinking=show_thinking,
        clock=iter([10.0, 13.2, 13.2]), monkeypatch=monkeypatch)
    assert ("Private working" in rendered) is details
    assert ("thought for 3.2s" in rendered) is summary
    assert "Final answer" in rendered
    assert "Thinking…" not in rendered


def test_tools_replace_pending_status_and_escape_server_markup():
    rendered, raw = render([
        {"type": "tool_call", "name": "memory_search"},
        {"type": "tool_result", "name": "memory_search", "ok": True, "summary": "Found\n2 notes"},
        {"type": "tool_call", "name": "read_file"},
        {"type": "tool_result", "name": "read_file", "ok": False, "summary": "[red]not allowed[/red]"},
        {"type": "error", "message": "[bold]server error[/bold]"},
        {"type": "done"},
    ], terminal=True)
    final = rendered[rendered.rfind("● Kova"):]
    assert final.count("memory_search") == 1
    assert "✓ memory_search · Found 2 notes" in final
    assert "✗ read_file · [red]not allowed[/red]" in final
    assert "Error: [bold]server error[/bold]" in final
    assert "\x1b[31m" in raw


def test_tall_reply_commits_all_lines_to_scrollback():
    rendered, _ = render([
        {"type": "text", "text": "\n\n".join(f"Paragraph {i}" for i in range(30))},
        {"type": "done"},
    ], terminal=True)
    final = rendered[rendered.rfind("● Kova"):]
    assert {f"Paragraph {i}" for i in range(30)} <= {line.rstrip() for line in final.splitlines()}


def test_connection_error_preserves_partial_reply_and_propagates():
    closed = []

    def stream(*args):
        try:
            yield {"type": "text", "text": "Partial answer"}
            raise KovaError(None, "offline")
        finally:
            closed.append(True)

    out = io.StringIO()
    with pytest.raises(KovaError, match="offline"):
        _render_turn(SimpleNamespace(stream_message=stream), "chat", "hi", False, Console(file=out))
    assert "Partial answer" in out.getvalue()
    assert closed == [True]


def test_slash_completion_covers_mode_and_web():
    from prompt_toolkit.completion import CompleteEvent
    from prompt_toolkit.document import Document

    from kova.ui import _COMMANDS, _CommandCompleter

    completer = _CommandCompleter()

    def completes(text):
        return {c.text for c in completer.get_completions(Document(text), CompleteEvent())}

    assert set(_COMMANDS) <= completes("")  # every command is completable
    assert "/mode" in completes("/m") and "/model" in completes("/m")
    assert "/web" in completes("/w")
    assert completes("/mode ") == {"chat", "work"}  # arguments complete too
    assert completes("/mode wo") == {"work"}
    assert completes("/web ") == {"on", "off"}
    assert completes("/web of") == {"off"}
    assert completes("/mode chat ") == set()  # nothing further to complete


# ---------------------------------------------------------------------------
# Pretty chat: tool argument previews, header wordmark, reasoning collapse
# ---------------------------------------------------------------------------

from pathlib import Path as _Path  # noqa: E402

from kova import ui as _ui  # noqa: E402


def _capture(renderable, width=80):
    out = io.StringIO()
    console = Console(file=out, width=width, record=True)
    console.print(renderable)
    return console.export_text()


class TestFormatArgs:
    def test_single_string_value_is_quoted(self):
        assert _ui._format_args({"query": "weather berlin"}) == '"weather berlin"'

    def test_single_non_string_value_is_bare(self):
        assert _ui._format_args({"count": 3}) == "3"

    def test_many_pairs_become_key_equals_value(self):
        assert _ui._format_args({"a": 1, "b": 2}) == "a=1, b=2"

    def test_none_is_empty_and_long_values_are_cut(self):
        assert _ui._format_args(None) == ""
        assert _ui._format_args({"q": "x" * 200}, limit=60).endswith("…")


class TestToolArgPreview:
    def test_running_row_shows_args(self):
        turn = _ui._Turn(False, False)
        turn.accept({"type": "tool_call", "name": "web_search",
                     "args": {"query": "weather berlin"}})
        rendered = _capture(turn.render())
        assert "web_search" in rendered
        assert "weather berlin" in rendered

    def test_finished_row_hides_args_and_keeps_summary(self):
        turn = _ui._Turn(False, False)
        turn.accept({"type": "tool_call", "name": "web_search",
                     "args": {"query": "weather berlin"}})
        turn.accept({"type": "tool_result", "name": "web_search", "ok": True,
                     "summary": "5 results"})
        turn.finished = True
        rendered = _capture(turn.render())
        assert "✓ web_search · 5 results" in rendered

    def test_interrupted_row_says_so(self):
        turn = _ui._Turn(False, False)
        turn.accept({"type": "tool_call", "name": "memory_save", "args": {"title": "x"}})
        turn.finished = True
        assert "interrupted" in _capture(turn.render())


class TestReasoningCollapse:
    def test_collapses_to_elapsed_time(self):
        turn = _ui._Turn(False, False)
        turn.accept({"type": "reasoning", "text": "thinking hard"})
        turn.end_reasoning()
        turn.finished = True
        rendered = _capture(turn.render())
        assert "thought for" in rendered
        assert "Thinking" not in rendered

    def test_no_think_hides_everything(self):
        turn = _ui._Turn(True, False)
        turn.accept({"type": "reasoning", "text": "thinking hard"})
        turn.end_reasoning()
        turn.finished = True
        rendered = _capture(turn.render())
        assert "thinking hard" not in rendered
        assert "thought for" not in rendered


class _StubClient:
    def __init__(self, model_ok=True):
        self._model_ok = model_ok

    def health(self):
        return {"model": "local", "model_ok": self._model_ok}

    def memory_list(self):
        return [{"slug": "a"}, {"slug": "b"}]

    def get_chat(self, chat_id):
        return {"mode": "work", "web": True}


def _header_text(monkeypatch, model_ok=True):
    monkeypatch.setattr(_ui, "load_config",
                        lambda: SimpleNamespace(memory_path=_Path("/tmp/mem")))
    out = io.StringIO()
    _ui._header(_StubClient(model_ok), Console(file=out, width=80, record=True), "chat-1")
    return out.getvalue()


class TestHeader:
    def test_wordmark_model_mode_and_memory(self, monkeypatch):
        rendered = _header_text(monkeypatch)
        assert "Kova" in rendered
        assert "model local" in rendered
        assert "mode work" in rendered
        assert "2 notes" in rendered
        assert "/help" in rendered

    def test_no_heavy_box(self, monkeypatch):
        rendered = _header_text(monkeypatch)
        assert "┌" not in rendered and "┐" not in rendered

    def test_offline_model_is_calm_but_clear(self, monkeypatch):
        rendered = _header_text(monkeypatch, model_ok=False)
        assert "offline" in rendered
        assert "local" in rendered
