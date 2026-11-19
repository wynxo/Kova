"""``/compare`` in the terminal chats (#96): argument parsing, event folding, and
both the full-screen chat and the plain line chat asking, printing and keeping."""
from __future__ import annotations

import io
import time

import pytest
from rich.console import Console

from kova import compare_cli, ui
from kova.client import KovaError
from kova.compare_cli import Collector, CompareError, choose_models, parse
from tests.test_tui import StubClient, chat_app, drain, transcript

MODELS = [{"name": "lab", "model": "m1"}, {"name": "cloud", "model": "m2"}, {"name": "mini", "model": "m3"}]

USAGE = {"prompt_tokens": 12, "completion_tokens": 30, "tokens_per_second": 41.5, "local": True}


def events(*, fail: str = "", kept: bool = True):
    """What the server sends for a compare of lab and cloud."""
    yield {"type": "start", "compare_id": "cmp1", "models": ["lab", "cloud"]}
    yield {"type": "reasoning", "model": "lab", "text": "hm"}
    yield {"type": "text", "model": "lab", "text": "Lab says "}
    yield {"type": "text", "model": "lab", "text": "hello"}
    yield {"type": "model_done", "model": "lab", "text": "Lab says hello", "usage": USAGE}
    if fail:
        yield {"type": "model_error", "model": "cloud", "message": fail}
    else:
        yield {"type": "model_done", "model": "cloud", "text": "Cloud says hi", "usage": USAGE}
    yield {"type": "done", "compare_id": "cmp1", "kept": ["lab", "cloud"] if kept else []}


class CompareClient(StubClient):
    def __init__(self, **options):
        super().__init__()
        self.options = options
        self.compared: list[tuple] = []
        self.kept: list[tuple] = []
        self.keep_error: KovaError | None = None

    def models_list(self):
        return [dict(model) for model in MODELS]

    def stream_compare(self, chat_id, text, models):
        self.compared.append((chat_id, text, list(models)))
        yield from events(**self.options)

    def keep_compare(self, chat_id, compare_id, model):
        if self.keep_error:
            raise self.keep_error
        self.kept.append((chat_id, compare_id, model))
        return {"model": model}


# ------------------------------------------------------------ arguments
def test_parse_reads_models_and_message():
    assert parse("hello there") == (None, "hello there")
    assert parse("-m lab,cloud  which is better?") == (["lab", "cloud"], "which is better?")
    assert parse("--models lab,cloud hi") == (["lab", "cloud"], "hi")
    assert parse("--models=lab,cloud hi") == (["lab", "cloud"], "hi")


@pytest.mark.parametrize("rest", ["", "   ", "-m lab,cloud", "-m  hello", "-m"])
def test_parse_refuses_an_empty_request(rest):
    with pytest.raises(CompareError):
        parse(rest)


def test_a_message_that_starts_with_a_dash_word_is_not_a_flag():
    assert parse("-mood is low today") == (None, "-mood is low today")


def test_choose_models_puts_the_active_one_first_and_stops_at_four():
    many = [{"name": f"m{i}"} for i in range(6)]
    assert choose_models(many, "m3", None) == ["m3", "m0", "m1", "m2"]
    assert choose_models(MODELS, "gone", None) == ["lab", "cloud", "mini"]


def test_choose_models_explains_what_is_wrong():
    with pytest.raises(CompareError, match='No model named "nope"'):
        choose_models(MODELS, "lab", ["lab", "nope"])
    with pytest.raises(CompareError, match="once"):
        choose_models(MODELS, "lab", ["lab", "lab"])
    with pytest.raises(CompareError, match="at least two"):
        choose_models(MODELS[:1], "lab", None)
    with pytest.raises(CompareError, match="at least 2"):
        choose_models(MODELS, "lab", ["lab"])
    with pytest.raises(CompareError, match="up to 4"):
        choose_models([{"name": f"m{i}"} for i in range(5)], "m0", [f"m{i}" for i in range(5)])


# ------------------------------------------------------------ events
def test_the_collector_folds_a_stream_into_answers():
    state = Collector()
    for event in events():
        state.feed(event)
    assert state.compare_id == "cmp1" and state.finished == "done"
    assert [a.model for a in state.keepable()] == ["lab", "cloud"]
    assert state.answers["lab"].text == "Lab says hello"
    assert "41.5 tok/s" in state.answers["lab"].stats()


def test_the_collector_keeps_a_failure_apart_and_marks_a_stop():
    state = Collector()
    for event in events(fail="no key"):
        state.feed(event)
    assert state.answers["cloud"].phase == "error" and state.answers["cloud"].error == "no key"
    assert [a.model for a in state.keepable()] == ["lab"]

    stopped = Collector()
    for event in [{"type": "start", "compare_id": "c", "models": ["a", "b"]},
                  {"type": "text", "model": "a", "text": "part"}, {"type": "stopped"}]:
        stopped.feed(event)
    assert stopped.finished == "stopped"
    assert all(answer.phase == "stopped" and not answer.keepable for answer in stopped.answers.values())


def test_progress_counts_who_has_answered():
    state = Collector()
    assert state.progress() == "Comparing"
    for event in list(events())[:5]:
        state.feed(event)
    assert state.progress() == "Comparing 2 models · 1 of 2 answered"


# ------------------------------------------------------------ full-screen chat
def keys(app, *names):
    from types import SimpleNamespace
    for name in names:
        app.on_key(SimpleNamespace(name=name, text=""))


def test_compare_streams_prints_every_answer_and_keeps_the_pick():
    client = CompareClient()
    app = chat_app(client)
    app.model = "lab"
    app.command("/compare which is better?")
    drain(app)
    assert client.compared == [("c1", "which is better?", ["lab", "cloud", "mini"])]
    assert app.overlay is not None and app.overlay.title == "Keep an answer"
    keys(app, "down", "enter")
    assert client.kept == [("c1", "cmp1", "cloud")]
    out = transcript(app)
    assert "Lab says hello" in out and "Cloud says hi" in out and "41.5 tok/s" in out
    assert "Kept the answer from cloud" in out


def test_compare_names_the_models_and_skips_the_picker_when_nothing_can_be_kept():
    client = CompareClient(fail="boom", kept=False)
    client.stream_compare = lambda chat_id, text, models: iter(
        [{"type": "start", "compare_id": "c", "models": models},
         {"type": "model_error", "model": "lab", "message": "boom"},
         {"type": "model_error", "model": "cloud", "message": "boom"},
         {"type": "done", "compare_id": "c", "kept": []}])
    app = chat_app(client)
    app.command("/compare -m lab,cloud hi")
    drain(app)
    assert app.overlay is None
    assert transcript(app).count("boom") == 2


def test_compare_with_a_bad_request_explains_and_sends_nothing():
    client = CompareClient()
    app = chat_app(client)
    app.command("/compare")
    app.command("/compare -m lab,nope hi")
    assert client.compared == [] and app.job is None
    text = transcript(app)
    assert "Usage: /compare" in text and 'No model named "nope"' in text


def test_a_refused_compare_shows_the_servers_reason():
    client = CompareClient()

    def refuse(chat_id, text, models):
        raise KovaError(409, "Local only is on: cloud is not on this computer.")
        yield  # pragma: no cover

    client.stream_compare = refuse
    app = chat_app(client)
    app.command("/compare hi")
    drain(app)
    assert "Local only is on" in transcript(app) and app.overlay is None


def test_esc_keeps_nothing_and_a_failed_keep_is_reported():
    client = CompareClient()
    app = chat_app(client)
    app.command("/compare hi")
    drain(app)
    keys(app, "esc")
    assert client.kept == [] and app.overlay is None

    client.keep_error = KovaError(404, "That comparison has expired.")
    app.command("/compare again")
    drain(app)
    keys(app, "enter")
    assert "Could not keep that answer: That comparison has expired." in transcript(app)


def test_stopping_a_compare_calls_the_stop_endpoint_and_offers_nothing():
    def slow(chat_id, text, models):
        yield {"type": "start", "compare_id": "c", "models": models}
        yield {"type": "text", "model": models[0], "text": "partial"}
        while not client.stops:
            time.sleep(0.01)
        yield {"type": "stopped", "compare_id": "c", "kept": []}

    client = CompareClient()
    client.stream_compare = slow
    app = chat_app(client)
    app.command("/compare hi")
    drain(app, until=lambda app: any(answer.text for answer in app.compare.answers.values()))
    app._stop()
    drain(app)
    assert client.stops == ["c1"] and app.overlay is None


def test_compare_is_listed_with_the_other_commands():
    from kova.tui.app import COMMANDS
    assert "compare" in COMMANDS and "/compare" in ui._all_commands()


# ------------------------------------------------------------ plain chat
def plain_console():
    return Console(file=io.StringIO(), force_terminal=False, width=100)


def test_the_plain_chat_prints_answers_and_keeps_the_number_you_type(monkeypatch):
    client = CompareClient()
    console = plain_console()
    monkeypatch.setattr(console, "input", lambda prompt="": "2")
    ui._compare(client, console, lambda: "c1", "lab", "-m lab,cloud which?", interactive=True)
    out = console.file.getvalue()
    assert "Lab says hello" in out and "Cloud says hi" in out
    assert client.compared == [("c1", "which?", ["lab", "cloud"])]
    assert client.kept == [("c1", "cmp1", "cloud")]
    assert "Kept the answer from cloud" in out


def test_the_plain_chat_keeps_none_on_enter_or_a_bad_number(monkeypatch):
    for typed, expected in (("", "Nothing kept."), ("9", "Nothing kept.")):
        client = CompareClient()
        console = plain_console()
        monkeypatch.setattr(console, "input", lambda prompt="", typed=typed: typed)
        ui._compare(client, console, lambda: "c1", "lab", "hi", interactive=True)
        assert client.kept == [] and expected in console.file.getvalue()


def test_the_plain_chat_does_not_prompt_when_input_is_piped():
    client = CompareClient()
    console = plain_console()
    ui._compare(client, console, lambda: "c1", "lab", "hi", interactive=False)
    assert client.kept == [] and "Not kept" in console.file.getvalue()


def test_the_plain_chat_explains_a_bad_request_before_creating_a_chat():
    created = []
    console = plain_console()
    ui._compare(CompareClient(), console, lambda: created.append(1) or "c1", "lab", "", interactive=False)
    assert created == [] and "Usage: /compare" in console.file.getvalue()
