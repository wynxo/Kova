import json

import httpx
import pytest

from kova.config import ModelEntry
from kova.models import ModelClient, ModelError, ThinkSplitter
from tests.fake_model import FakeModel, text, tool


@pytest.fixture
def entry():
    return ModelEntry("tiel", "http://model.test/v1", "test-model")


async def collect(entry, script):
    fake = FakeModel([script])
    events = [event async for event in ModelClient(entry, fake.transport).stream([{"role": "user", "content": "Hi"}])]
    return events, fake


async def test_streams_text(entry):
    events, fake = await collect(entry, text("Hello there"))
    assert "".join(e.text for e in events if e.type == "text") == "Hello there"
    assert events[-1].type == "done"
    assert fake.requests[0]["stream"] is True
    assert fake.requests[0]["model"] == "test-model"


async def test_reasoning_field(entry):
    events, _ = await collect(entry, [{"reasoning_content": "one"}, {"reasoning": "two"}])
    assert "".join(e.text for e in events if e.type == "reasoning") == "onetwo"


async def test_think_tags_become_reasoning(entry):
    events, _ = await collect(entry, [{"content": "<think>hmm</think>Hi"}])
    assert "".join(e.text for e in events if e.type == "reasoning") == "hmm"
    assert "".join(e.text for e in events if e.type == "text") == "Hi"


async def test_think_tag_split_across_chunks(entry):
    events, _ = await collect(entry, [{"content": s} for s in ("<thi", "nk>a</th", "ink>b")])
    assert "".join(e.text for e in events if e.type == "reasoning") == "a"
    assert "".join(e.text for e in events if e.type == "text") == "b"
    assert all("<" not in e.text for e in events if e.type == "text")


async def test_tool_call_accumulated(entry):
    events, _ = await collect(entry, tool("memory_save", {"title": "Dog", "body": "Rex"}))
    calls = [e.tool_call for e in events if e.type == "tool_call"]
    assert len(calls) == 1
    assert calls[0]["id"] == "call_1" and calls[0]["name"] == "memory_save"
    assert json.loads(calls[0]["arguments"]) == {"title": "Dog", "body": "Rex"}
    assert events[-1].type == "done"


async def test_unreachable_raises_model_error(entry):
    def fail(request):
        raise httpx.ConnectError("down", request=request)
    with pytest.raises(ModelError, match="Can't reach model 'tiel'"):
        _ = [event async for event in ModelClient(entry, httpx.MockTransport(fail)).stream([])]


async def test_ping(entry):
    assert await ModelClient(entry, FakeModel([]).transport).ping() is True
    assert await ModelClient(entry, httpx.MockTransport(lambda request: httpx.Response(503))).ping() is False


def test_splitter_flush_preserves_incomplete_tag():
    splitter = ThinkSplitter()
    assert splitter.feed("hello <thi") == [("text", "hello ")]
    assert splitter.flush() == [("text", "<thi")]


async def test_http_error_and_missing_tool_id(entry):
    transport = httpx.MockTransport(lambda request: httpx.Response(429))
    with pytest.raises(ModelError, match="HTTP 429"):
        _ = [event async for event in ModelClient(entry, transport).stream([])]
    events, _ = await collect(entry, [{"tool_calls": [{"index": 0, "function": {"name": "x", "arguments": "{}"}}]}])
    assert next(e.tool_call for e in events if e.type == "tool_call")["id"] == "call_0"


@pytest.mark.parametrize("delta", [
    {"content": "An unfinished answer"},
    {"reasoning_content": "Still thinking"},
    {"tool_calls": [{"index": 0, "id": "call_cut", "function": {
        "name": "write_file", "arguments": '{"path":"oops"}'}}]},
])
async def test_premature_eof_never_releases_tools_or_success(entry, delta):
    wire = 'data: ' + json.dumps({"choices": [{"delta": delta}]}) + '\n\n'
    transport = httpx.MockTransport(lambda request: httpx.Response(200, text=wire))
    events = []
    with pytest.raises(ModelError, match="ended before completion"):
        async for event in ModelClient(entry, transport).stream([]):
            events.append(event)
    assert not any(event.type in {"tool_call", "done"} for event in events)


@pytest.mark.parametrize("terminal", [
    'data: [DONE]\n\n',
    'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}\n\n',
])
async def test_explicit_completion_accepts_done_or_finish_reason(entry, terminal):
    wire = 'data: {"choices":[{"delta":{"content":"Hello"}}]}\n\n' + terminal
    transport = httpx.MockTransport(lambda request: httpx.Response(200, text=wire))
    events = [event async for event in ModelClient(entry, transport).stream([])]
    assert events[-1].type == "done"


async def test_fixed_destination_refuses_to_send_after_managed_endpoint_changes(entry, monkeypatch, tmp_path):
    from contextlib import asynccontextmanager
    from types import SimpleNamespace

    from kova import managed_engine
    from kova.config import load_config

    @asynccontextmanager
    async def use(model, *, transport=None):
        model.base_url = "http://new-model.test/v1"
        yield

    engine = SimpleNamespace(use=use)
    monkeypatch.setattr(managed_engine, "manager", lambda cfg: engine)
    entry.provider = "managed-test"
    fake = FakeModel([text("Unexpected")])
    client = ModelClient(entry, fake.transport, config=load_config(tmp_path), fixed_destination=True)

    with pytest.raises(ModelError, match="destination changed"):
        _ = [event async for event in client.stream([{"role": "user", "content": "Approved task"}])]
    assert not fake.requests
