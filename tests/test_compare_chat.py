"""Compare answers inside a chat and keep one (issue #96).

A fake model transport answers per model id, so nothing here needs a model
server or the network.
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest
from fastapi.testclient import TestClient

from kova.config import ModelEntry, ensure_token, load_config
from kova.server import create_app


def _frames(text: str) -> bytes:
    pieces = [text[i:i + 4] for i in range(0, len(text), 4)]
    body = "".join(f"data: {json.dumps({'choices': [{'delta': {'content': p}}]})}\n\n" for p in pieces)
    usage = {"prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 10}
    body += f"data: {json.dumps({'choices': [], 'usage': usage})}\n\n"
    return (body + "data: [DONE]\n\n").encode()


class _Hanging(httpx.AsyncByteStream):
    """Send one piece of an answer, then wait until the request is cancelled."""

    def __init__(self, cancelled: threading.Event):
        self.cancelled = cancelled

    async def __aiter__(self):
        yield f"data: {json.dumps({'choices': [{'delta': {'content': 'thinking'}}]})}\n\n".encode()
        try:
            await asyncio.sleep(10)  # bounded even if cancellation regresses
        finally:
            self.cancelled.set()


class FakeModels:
    """Answers by model id; records requests and how many ran at the same time."""

    def __init__(self, answers: dict, *, together: int = 0):
        self.answers = answers
        self.together = together
        self.requests: list[dict] = []
        self.in_flight = 0
        self.most_at_once = 0
        self.cancelled = threading.Event()
        self.transport = httpx.MockTransport(self.handle)

    async def handle(self, request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, json={"data": []})
        body = json.loads(request.content)
        self.requests.append(body)
        self.in_flight += 1
        self.most_at_once = max(self.most_at_once, self.in_flight)
        try:
            # Wait until every model has been asked: only concurrent requests get here.
            deadline = time.monotonic() + 3
            while self.most_at_once < self.together and time.monotonic() < deadline:
                await asyncio.sleep(0.01)
            answer = self.answers[body["model"]]
            if answer is None:
                return httpx.Response(200, headers={"content-type": "text/event-stream"},
                                      stream=_Hanging(self.cancelled))
            if isinstance(answer, int):
                return httpx.Response(answer, json={"error": {"message": "nope"}})
            return httpx.Response(200, content=_frames(answer),
                                  headers={"content-type": "text/event-stream"})
        finally:
            self.in_flight -= 1


@pytest.fixture
def server(tmp_path, monkeypatch):
    def start(answers: dict, *, together: int = 0, local_only: bool = False):
        monkeypatch.setenv("KOVA_HOME", str(tmp_path))
        cfg = load_config()
        cfg.models[:] = [
            ModelEntry(name="lab", base_url="http://127.0.0.1:9/v1", model="lab-id", context=8192),
            ModelEntry(name="cloud", base_url="https://cloud.example/v1", model="cloud-id", context=8192),
            ModelEntry(name="tiny", base_url="http://127.0.0.1:9/v1", model="tiny-id", context=8192),
        ]
        cfg.active = "lab"
        cfg.privacy_local_only = local_only
        fake = FakeModels(answers, together=together)
        app = create_app(cfg, transport=fake.transport)
        return app, fake, {"Authorization": f"Bearer {ensure_token(cfg.home)}"}
    return start


def _events(response: httpx.Response) -> list[dict]:
    return [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: ")]


def _chat(client, headers) -> str:
    return client.post("/chats", headers=headers).json()["id"]


def test_compare_runs_every_model_at_once_and_writes_nothing(server):
    app, fake, headers = server({"lab-id": "local answer", "cloud-id": "cloud answer"}, together=2)
    with TestClient(app) as client:
        chat = _chat(client, headers)
        response = client.post(f"/chats/{chat}/compare", headers=headers,
                               json={"text": "which is better?", "models": ["lab", "cloud"]})
        assert response.status_code == 200
        events = _events(response)
        assert fake.most_at_once == 2
        assert events[0]["type"] == "start" and events[0]["models"] == ["lab", "cloud"]
        texts = {}
        for event in events:
            if event["type"] == "text":
                texts[event["model"]] = texts.get(event["model"], "") + event["text"]
        assert texts == {"lab": "local answer", "cloud": "cloud answer"}
        done = {e["model"]: e for e in events if e["type"] == "model_done"}
        assert done["lab"]["usage"]["completion_tokens"] == 3
        assert done["lab"]["usage"]["model"] == "lab"
        assert events[-1]["type"] == "done" and events[-1]["kept"] == ["cloud", "lab"]
        # A plain answer: no tool schemas, the same user words for both models.
        assert all("tools" not in request for request in fake.requests)
        assert {r["messages"][-1]["content"] for r in fake.requests} == {"which is better?"}
        assert client.get(f"/chats/{chat}", headers=headers).json()["messages"] == []


def test_one_failing_model_does_not_stop_the_others(server):
    app, fake, headers = server({"lab-id": "fine", "tiny-id": 500})
    with TestClient(app) as client:
        chat = _chat(client, headers)
        events = _events(client.post(f"/chats/{chat}/compare", headers=headers,
                                     json={"text": "hi", "models": ["lab", "tiny"]}))
        assert [e["model"] for e in events if e["type"] == "model_done"] == ["lab"]
        assert [e["model"] for e in events if e["type"] == "model_error"] == ["tiny"]
        assert events[-1]["kept"] == ["lab"]


def test_compare_sees_the_chat_so_far_without_tool_rounds(server):
    app, fake, headers = server({"lab-id": "a", "tiny-id": "b"})
    with TestClient(app) as client:
        chat = _chat(client, headers)
        store = app.state.store
        store.add_message(chat, "user", "earlier question")
        store.add_message(chat, "assistant", None, tool_calls=[
            {"id": "c1", "type": "function", "function": {"name": "web_search", "arguments": "{}"}}])
        store.add_message(chat, "tool", "tool output", tool_call_id="c1")
        store.add_message(chat, "assistant", "earlier answer")
        client.post(f"/chats/{chat}/compare", headers=headers,
                    json={"text": "and now?", "models": ["lab", "tiny"]})
        roles = [(m["role"], m["content"]) for m in fake.requests[0]["messages"][1:]]
        assert roles == [("user", "earlier question"), ("assistant", "earlier answer"),
                         ("user", "and now?")]


def test_local_only_refuses_a_cloud_model_before_sending(server):
    app, fake, headers = server({"lab-id": "a", "cloud-id": "b", "tiny-id": "c"}, local_only=True)
    with TestClient(app) as client:
        chat = _chat(client, headers)
        refused = client.post(f"/chats/{chat}/compare", headers=headers,
                              json={"text": "hi", "models": ["lab", "cloud"]})
        assert refused.status_code == 409
        assert fake.requests == []
        allowed = client.post(f"/chats/{chat}/compare", headers=headers,
                              json={"text": "hi", "models": ["lab", "tiny"]})
        assert allowed.status_code == 200
        assert len(fake.requests) == 2


def test_compare_validates_models(server):
    app, fake, headers = server({"lab-id": "a"})
    with TestClient(app) as client:
        chat = _chat(client, headers)
        url = f"/chats/{chat}/compare"
        assert client.post(url, headers=headers, json={"text": "hi", "models": ["lab"]}).status_code == 422
        assert client.post(url, headers=headers,
                           json={"text": "hi", "models": ["lab", "lab"]}).status_code == 422
        assert client.post(url, headers=headers,
                           json={"text": "hi", "models": ["a", "b", "c", "d", "e"]}).status_code == 422
        assert client.post(url, headers=headers,
                           json={"text": "hi", "models": ["lab", "ghost"]}).status_code == 404
        assert client.post("/chats/missing/compare", headers=headers,
                           json={"text": "hi", "models": ["lab", "tiny"]}).status_code == 404
        assert client.post(url, json={"text": "hi", "models": ["lab", "tiny"]}).status_code == 401


def test_keep_saves_one_turn_with_model_and_usage(server):
    app, fake, headers = server({"lab-id": "local answer", "cloud-id": "cloud answer"})
    with TestClient(app) as client:
        chat = _chat(client, headers)
        events = _events(client.post(f"/chats/{chat}/compare", headers=headers,
                                     json={"text": "pick me", "models": ["lab", "cloud"]}))
        compare_id = events[0]["compare_id"]
        kept = client.post(f"/chats/{chat}/compare/keep", headers=headers,
                           json={"compare_id": compare_id, "model": "cloud"})
        assert kept.status_code == 200
        assert kept.json()["model"] == "cloud"
        saved = client.get(f"/chats/{chat}", headers=headers).json()
        messages = [(m["role"], m["content"]) for m in saved["messages"]]
        assert messages == [("user", "pick me"), ("assistant", "cloud answer")]
        assert saved["messages"][1]["usage"]["model"] == "cloud"
        assert saved["messages"][1]["usage"]["completion_tokens"] == 3
        assert saved["title"] == "pick me"
        # The same comparison cannot be kept twice, and the others are gone.
        again = client.post(f"/chats/{chat}/compare/keep", headers=headers,
                            json={"compare_id": compare_id, "model": "lab"})
        assert again.status_code == 404
        # The next normal turn sees the kept answer as history.
        assert app.state.store.last_turn_model(chat) == "cloud"


def test_keep_rejects_another_chat_or_an_unfinished_model(server):
    app, fake, headers = server({"lab-id": "a", "tiny-id": 500})
    with TestClient(app) as client:
        chat, other = _chat(client, headers), _chat(client, headers)
        events = _events(client.post(f"/chats/{chat}/compare", headers=headers,
                                     json={"text": "hi", "models": ["lab", "tiny"]}))
        compare_id = events[0]["compare_id"]
        assert client.post(f"/chats/{other}/compare/keep", headers=headers,
                           json={"compare_id": compare_id, "model": "lab"}).status_code == 404
        assert client.post(f"/chats/{chat}/compare/keep", headers=headers,
                           json={"compare_id": compare_id, "model": "tiny"}).status_code == 409
        assert client.post(f"/chats/{chat}/compare/keep", headers=headers,
                           json={"compare_id": "nope", "model": "lab"}).status_code == 404
        assert client.get(f"/chats/{chat}", headers=headers).json()["messages"] == []


def test_stop_cancels_every_model_and_keeps_finished_answers(server):
    app, fake, headers = server({"lab-id": "quick", "tiny-id": None})
    with TestClient(app) as client:
        chat = _chat(client, headers)
        with ThreadPoolExecutor() as pool:
            stream = pool.submit(client.post, f"/chats/{chat}/compare", headers=headers,
                                 json={"text": "go", "models": ["lab", "tiny"]})
            deadline = time.monotonic() + 3
            while len(fake.requests) < 2 and time.monotonic() < deadline:
                time.sleep(0.01)
            # Wait for the quick model to finish before stopping the slow one.
            while not any(app.state.compares[c]["results"] for c in list(app.state.compares)) \
                    and time.monotonic() < deadline:
                time.sleep(0.01)
            assert client.post(f"/chats/{chat}/stop", headers=headers).json() == {"stopped": True}
            assert fake.cancelled.wait(3)
            events = _events(stream.result(timeout=3))
        assert events[-1]["type"] == "stopped" and events[-1]["kept"] == ["lab"]
        assert not app.state.chat_turns.get(chat)
        compare_id = events[0]["compare_id"]
        kept = client.post(f"/chats/{chat}/compare/keep", headers=headers,
                           json={"compare_id": compare_id, "model": "lab"})
        assert kept.status_code == 200


def test_keep_stops_a_compare_still_running(server):
    app, fake, headers = server({"lab-id": "quick", "tiny-id": None})
    with TestClient(app) as client:
        chat = _chat(client, headers)
        with ThreadPoolExecutor() as pool:
            stream = pool.submit(client.post, f"/chats/{chat}/compare", headers=headers,
                                 json={"text": "go", "models": ["lab", "tiny"]})
            deadline = time.monotonic() + 3
            while not any(item["results"] for item in list(app.state.compares.values())) \
                    and time.monotonic() < deadline:
                time.sleep(0.01)
            compare_id = next(iter(app.state.compares))
            kept = client.post(f"/chats/{chat}/compare/keep", headers=headers,
                               json={"compare_id": compare_id, "model": "lab"})
            assert kept.status_code == 200
            assert fake.cancelled.wait(3)
            assert _events(stream.result(timeout=3))[-1]["type"] == "stopped"
        messages = client.get(f"/chats/{chat}", headers=headers).json()["messages"]
        assert [(m["role"], m["content"]) for m in messages] == [("user", "go"), ("assistant", "quick")]


def test_turning_on_local_only_cancels_a_running_compare(server):
    app, fake, headers = server({"lab-id": "quick", "tiny-id": None})
    with TestClient(app) as client:
        chat = _chat(client, headers)
        with ThreadPoolExecutor() as pool:
            stream = pool.submit(client.post, f"/chats/{chat}/compare", headers=headers,
                                 json={"text": "go", "models": ["lab", "tiny"]})
            deadline = time.monotonic() + 3
            while len(fake.requests) < 2 and time.monotonic() < deadline:
                time.sleep(0.01)
            assert client.patch("/privacy", headers=headers, json={"local_only": True}).status_code == 200
            assert fake.cancelled.wait(3)
            assert _events(stream.result(timeout=3))[-1]["type"] == "stopped"


def test_a_running_compare_blocks_a_second_one(server):
    app, fake, headers = server({"lab-id": None, "tiny-id": None})
    with TestClient(app) as client:
        chat = _chat(client, headers)
        with ThreadPoolExecutor() as pool:
            stream = pool.submit(client.post, f"/chats/{chat}/compare", headers=headers,
                                 json={"text": "go", "models": ["lab", "tiny"]})
            deadline = time.monotonic() + 3
            while len(fake.requests) < 2 and time.monotonic() < deadline:
                time.sleep(0.01)
            again = client.post(f"/chats/{chat}/compare", headers=headers,
                                json={"text": "again", "models": ["lab", "tiny"]})
            assert again.status_code == 409
            client.post(f"/chats/{chat}/stop", headers=headers)
            stream.result(timeout=3)
