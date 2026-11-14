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
        ("patch", "/chats/abc", {"title": "Hi"}),
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
    assert response.json() == {"version": __version__, "model": "local", "model_ok": False}


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


def test_memory_over_budget_returns_422(server):
    client, headers, cfg, fake = server
    response = client.post("/memory", json={"title": "Huge", "body": "x" * 9000},
                           headers=headers)
    assert response.status_code == 422
    assert "over budget by" in response.json()["detail"]
    assert client.get("/memory", headers=headers).json() == []


def test_memory_location_keeps_the_configured_budget(server, tmp_path):
    client, headers, cfg, fake = server
    response = client.post("/memory/location",
                           json={"path": str(tmp_path / "vault"), "obsidian": False},
                           headers=headers)
    assert response.status_code == 200
    assert client.app.state.memory.budget == cfg.memory_budget


def test_memory_location_is_normalised(server, tmp_path, monkeypatch):
    """The live memory folder must match what a restart would read (#90)."""
    client, headers, cfg, fake = server
    home = tmp_path / 'home'
    home.mkdir()
    monkeypatch.setenv('HOME', str(home))
    for bad in ('', '   ', 'relative/dir', 'bad\0path'):
        assert client.post('/memory/location', json={'path': bad}, headers=headers).status_code == 422
    response = client.post('/memory/location', json={'path': '~/notes'}, headers=headers)
    assert response.status_code == 200
    root = (home / 'notes').resolve()
    assert client.app.state.memory.root == root
    assert client.app.state.agent.memory.root == root
    assert cfg.memory_path == root
    assert load_config(cfg.home).memory_path == root
    assert client.post('/memory', json={'title': 'Groceries', 'body': 'milk'},
                       headers=headers).status_code == 200
    assert (root / 'groceries.md').is_file()


def test_memory_settings_roundtrip_and_guards(server):
    client, headers, cfg, fake = server
    settings = client.get("/memory/settings", headers=headers).json()
    assert settings["review"] is False and settings["review_every"] == 12
    updated = client.patch("/memory/settings",
                           json={"review": True, "review_every": 4}, headers=headers).json()
    assert (updated["review"], updated["review_every"]) == (True, 4)
    assert cfg.memory_review is True and cfg.memory_review_every == 4
    assert client.app.state.agent.memory_review is not None
    for bad in ({"review_every": 0}, {"consolidate_hour": 24},
                {"consolidate_min_confidence": 2}, {"consolidate_max_loss": -1}):
        assert client.patch("/memory/settings", json=bad, headers=headers).status_code == 422
    # Switching the passes back off drops the agent hook again.
    assert client.patch("/memory/settings",
                        json={"review": False}, headers=headers).json()["review"] is False
    assert client.app.state.agent.memory_review is None


def test_memory_revisions_lists_recorded_changes(server):
    client, headers, cfg, fake = server
    assert client.get("/memory/revisions", headers=headers).json() == []
    client.post("/memory", json={"title": "Dog", "body": "Rex"}, headers=headers)
    from kova.review import record_revision
    record_revision(client.app.state.store, client.app.state.memory,
                    "dog", "consolidate", before="old text")
    entries = client.get("/memory/revisions", headers=headers).json()
    assert [(entry["slug"], entry["kind"], entry["undone"]) for entry in entries] == [
        ("dog", "consolidate", False)]


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


def test_first_reply_sets_trimmed_title(server):
    client, headers, cfg, fake = server
    chat_id = client.post("/chats", headers=headers).json()["id"]
    text = "This is a fairly long first message that should be trimmed at a word boundary"
    response = client.post(f"/chats/{chat_id}/messages", json={"text": text}, headers=headers)
    assert _parse_sse(response.text)[-1]["type"] == "done"

    chat = client.get(f"/chats/{chat_id}", headers=headers).json()
    assert chat["title"] == "This is a fairly long first message that"


def test_list_chats_reports_title_when_and_count(server):
    client, headers, cfg, fake = server
    chat_id = client.post("/chats", headers=headers).json()["id"]
    response = client.post(f"/chats/{chat_id}/messages", json={"text": "hello"}, headers=headers)
    assert _parse_sse(response.text)[-1]["type"] == "done"

    listed = client.get("/chats", headers=headers).json()
    assert listed[0]["id"] == chat_id
    assert listed[0]["title"] == "hello"
    assert listed[0]["message_count"] == 2
    assert listed[0]["last_message"] >= listed[0]["created"]


def test_rename_chat(server):
    client, headers, cfg, fake = server
    chat_id = client.post("/chats", headers=headers).json()["id"]
    response = client.patch(f"/chats/{chat_id}", json={"title": "  Groceries  "}, headers=headers)
    assert response.json() == {"id": chat_id, "title": "Groceries", "mode": "work", "web": True, "permission": "manual", "cwd": str(cfg.files_path)}
    assert client.get("/chats", headers=headers).json()[0]["title"] == "Groceries"

    assert client.patch("/chats/abc", json={"title": "x"}, headers=headers).status_code == 404
    assert client.patch(f"/chats/{chat_id}", json={"title": "  "}, headers=headers).status_code == 422


def test_create_chat_with_mode_and_web(server):
    client, headers, cfg, fake = server
    default = client.post("/chats", headers=headers).json()
    assert (default["mode"], default["web"]) == ("work", True)

    created = client.post("/chats", json={"title": "Small talk", "mode": "chat", "web": False},
                          headers=headers).json()
    assert (created["mode"], created["web"]) == ("chat", False)
    fetched = client.get(f"/chats/{created['id']}", headers=headers).json()
    assert (fetched["mode"], fetched["web"]) == ("chat", False)
    listed = client.get("/chats", headers=headers).json()
    assert (listed[0]["mode"], listed[0]["web"]) == ("chat", False)

    assert client.post("/chats", json={"mode": "code"}, headers=headers).status_code == 422


def test_patch_chat_mode_and_web(server):
    client, headers, cfg, fake = server
    chat_id = client.post("/chats", headers=headers).json()["id"]

    patched = client.patch(f"/chats/{chat_id}", json={"mode": "chat", "web": False}, headers=headers)
    assert (patched.json()["mode"], patched.json()["web"]) == ("chat", False)
    fetched = client.get(f"/chats/{chat_id}", headers=headers).json()
    assert (fetched["mode"], fetched["web"]) == ("chat", False)

    back = client.patch(f"/chats/{chat_id}", json={"mode": "work", "web": True}, headers=headers)
    assert (back.json()["mode"], back.json()["web"]) == ("work", True)

    assert client.patch(f"/chats/{chat_id}", json={"mode": "code"}, headers=headers).status_code == 422
    assert client.patch("/chats/abc", json={"mode": "chat"}, headers=headers).status_code == 404
    assert client.patch(f"/chats/{chat_id}", json={}, headers=headers).status_code == 422


def test_chat_mode_turn_limits_tools_and_names_mode(server):
    client, headers, cfg, fake = server
    chat_id = client.post("/chats", json={"mode": "chat", "web": False}, headers=headers).json()["id"]
    response = client.post(f"/chats/{chat_id}/messages", json={"text": "hello"}, headers=headers)
    assert _parse_sse(response.text)[-1]["type"] == "done"
    assert "tools" not in fake.requests[0]
    prompt = fake.requests[0]["messages"][0]["content"]
    assert "Current mode: chat" in prompt


def test_unknown_chat_404(server):
    client, headers, cfg, fake = server
    assert client.get("/chats/abc", headers=headers).status_code == 404
    assert client.delete("/chats/abc", headers=headers).status_code == 404
    response = client.post("/chats/abc/messages", json={"text": "hi"}, headers=headers)
    assert response.status_code == 404


def test_manual_compaction_endpoint(server):
    client, headers, cfg, fake = server
    chat_id = client.post('/chats', headers=headers).json()['id']
    store = client.app.state.store
    for i in range(4):
        store.add_message(chat_id, 'user', 'message ' + str(i) + 'x' * 100)
        store.add_message(chat_id, 'assistant', 'reply')
    response = client.post(f'/chats/{chat_id}/compact', headers=headers)
    assert response.status_code == 200
    assert response.json()['type'] == 'compacted'
    assert response.json()['summary'] == 'Hello there!'
    assert 'Hello there!' in str(store.get_context(chat_id))


def test_permission_rules_authenticated_validated_and_persisted(server):
    client, headers, cfg, fake = server
    assert client.get('/permission-rules').status_code == 401
    rules = [{'action':'deny','tool':'run_command','pattern':'rm *','message':'Use trash'}]
    assert client.put('/permission-rules',headers=headers,json=rules).status_code == 200
    assert client.get('/permission-rules',headers=headers).json()[0]['message'] == 'Use trash'
    from kova.permissions import Approvals
    assert Approvals(path=cfg.home/'permission-rules.json').rules[0].action == 'deny'
    for invalid in [{'action':'oops'}, {'action':'allow','tool':4}, {'action':'allow','scope':'session'}]:
        assert client.put('/permission-rules',headers=headers,json=[invalid]).status_code == 422
    assert client.get('/permission-rules',headers=headers).json()[0]['action'] == 'deny'


def test_persona_api_rejects_invalid_identity_and_persists(server):
    client, headers, cfg, _ = server
    assert client.post('/persona', headers=headers, json={'name': 'Nova', 'tone': 'unknown'}).status_code == 422
    assert client.post('/persona', headers=headers, json={'name': 'Nova', 'tone': 'concise'}).status_code == 200
    assert client.get('/persona', headers=headers).json() == {'name': 'Nova', 'tone': 'concise', 'configured': True}
    assert load_config(cfg.home).persona_name == 'Nova'
