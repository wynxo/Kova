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
