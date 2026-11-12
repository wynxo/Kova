"""Server API tests: auth, chats, SSE turns, memory, reminders and models."""

import json
import time
from datetime import datetime, timedelta

import httpx
import pytest
from fastapi.testclient import TestClient

from kova import __version__
from kova.config import ensure_token, load_config
from kova.server import create_app
from tests.fake_model import FakeModel, text


@pytest.fixture
def server(tmp_path, monkeypatch):
    """A running app over a temp KOVA_HOME with a scripted model."""
    monkeypatch.setenv("KOVA_HOME", str(tmp_path))
    cfg = load_config()
    fake = FakeModel([text("Hello there!"), text("Second")])
    app = create_app(cfg, transport=fake.transport)
    headers = {"Authorization": f"Bearer {ensure_token(cfg.home)}"}
    with TestClient(app) as client:
        yield client, headers, cfg, fake


def _parse_sse(raw):
    """Parse SSE frames and check the exact wire format."""
    events = []
    for frame in raw.split("\n\n"):
        if not frame.strip():
            continue
        first, second = frame.splitlines()[:2]
        assert first.startswith("event: ") and second.startswith("data: ")
        event = json.loads(second[len("data: "):])
        assert first == f"event: {event['type']}"
        events.append(event)
    return events


def test_auth_required(server):
    client, headers, cfg, fake = server
    calls = [
        ("get", "/chats", None),
        ("post", "/chats", None),
        ("get", "/chats/abc", None),
        ("delete", "/chats/abc", None),
        ("post", "/chats/abc/messages", {"text": "hi"}),
        ("get", "/memory", None),
        ("post", "/memory", {"title": "T", "body": "B"}),
        ("delete", "/memory/note", None),
        ("get", "/reminders", None),
        ("post", "/reminders", {"when": "2030-01-01T00:00", "text": "x"}),
        ("delete", "/reminders/1", None),
        ("get", "/models", None),
        ("post", "/models", {"name": "m", "base_url": "http://localhost/v1", "model": "x"}),
        ("post", "/models/active", {"name": "m"}),
        ("get", "/events", None),
    ]
    for method, path, payload in calls:
        response = getattr(client, method)(path, **({"json": payload} if payload else {}))
        assert response.status_code == 401, (method, path)
    assert client.get("/health").status_code == 200


def test_health_ok_when_model_down(tmp_path, monkeypatch):
    monkeypatch.setenv("KOVA_HOME", str(tmp_path))
    cfg = load_config()

    def down(request):
        raise httpx.ConnectError("down", request=request)

    app = create_app(cfg, transport=httpx.MockTransport(down))
    with TestClient(app) as client:
        response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"version": __version__, "model": "tiel", "model_ok": False}


def test_chat_stream_events(server):
    client, headers, cfg, fake = server
    chat_id = client.post("/chats", headers=headers).json()["id"]
    response = client.post(f"/chats/{chat_id}/messages", json={"text": "hello"}, headers=headers)
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    events = _parse_sse(response.text)
    assert events[-1]["type"] == "done"
    assert isinstance(events[-1]["message_id"], int)
    assert all(event["type"] == "text" for event in events[:-1])
    assert "".join(event.get("text", "") for event in events[:-1]) == "Hello there!"
    messages = client.get(f"/chats/{chat_id}", headers=headers).json()["messages"]
    assert messages == [{"role": "user", "content": "hello"},
                         {"role": "assistant", "content": "Hello there!"}]


def test_disconnect_does_not_lose_the_turn(server):
    client, headers, cfg, fake = server
    chat_id = client.post("/chats", headers=headers).json()["id"]
    with client.stream("POST", f"/chats/{chat_id}/messages", json={"text": "hello"},
                       headers=headers) as response:
        for _line in response.iter_lines():
            break  # drop the connection right after the first SSE line
    messages = []
    for _ in range(200):  # the turn keeps running in its background task
        messages = client.get(f"/chats/{chat_id}", headers=headers).json()["messages"]
        if len(messages) == 2:
            break
        time.sleep(0.02)
    assert [m["role"] for m in messages] == ["user", "assistant"]
    assert messages[1]["content"] == "Hello there!"


def test_memory_crud(server):
    client, headers, cfg, fake = server
    response = client.post("/memory", json={"title": "Dog", "body": "Rex", "tags": ["pet"]},
                           headers=headers)
    body = response.json()
    assert (body["slug"], body["title"], body["body"], body["tags"]) == ("dog", "Dog", "Rex", ["pet"])
    listed = client.get("/memory", headers=headers).json()
    assert [n["slug"] for n in listed] == ["dog"]
    found = client.get("/memory", params={"q": "dog"}, headers=headers).json()
    assert [n["slug"] for n in found] == ["dog"]
    assert client.delete("/memory/dog", headers=headers).json() == {"ok": True}
    assert client.get("/memory", headers=headers).json() == []
    assert client.delete("/memory/dog", headers=headers).status_code == 404


def test_reminders_crud(server):
    client, headers, cfg, fake = server
    when = (datetime.now().astimezone() + timedelta(days=1)).isoformat()
    response = client.post("/reminders", json={"when": when, "text": "call mom"}, headers=headers)
    body = response.json()
    assert (body["id"], body["when"], body["text"], body["done"]) == (1, when, "call mom", False)
    listed = client.get("/reminders", headers=headers).json()
    assert [r["text"] for r in listed] == ["call mom"]
    assert client.delete("/reminders/1", headers=headers).json() == {"ok": True}
    assert client.get("/reminders", headers=headers).json() == []
    assert client.delete("/reminders/1", headers=headers).status_code == 404


def test_models_switch(server):
    client, headers, cfg, fake = server
    added = client.post("/models", json={"name": "ollama", "base_url": "http://localhost:11434/v1",
                                         "model": "qwen3", "context": 4096}, headers=headers)
    assert (added.json()["name"], added.json()["context"]) == ("ollama", 4096)
    missing = client.post("/models/active", json={"name": "ghost"}, headers=headers)
    assert missing.status_code == 404
    activated = client.post("/models/active", json={"name": "ollama"}, headers=headers)
    assert activated.json() == {"active": "ollama"}
    health = client.get("/health").json()
    assert health["model"] == "ollama" and health["model_ok"] is True
    reloaded = load_config(cfg.home)
    assert reloaded.active == "ollama"
    assert reloaded.active_model().model == "qwen3"
    chat_id = client.post("/chats", headers=headers).json()["id"]
    response = client.post(f"/chats/{chat_id}/messages", json={"text": "hi"}, headers=headers)
    assert _parse_sse(response.text)[-1]["type"] == "done"
    assert fake.requests[-1]["model"] == "qwen3"


def test_unknown_chat_404(server):
    client, headers, cfg, fake = server
    assert client.get("/chats/abc", headers=headers).status_code == 404
    assert client.delete("/chats/abc", headers=headers).status_code == 404
    response = client.post("/chats/abc/messages", json={"text": "hi"}, headers=headers)
    assert response.status_code == 404
