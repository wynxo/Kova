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
from kova.voice import TranscriptionResult, MAX_AUDIO_BYTES
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


@pytest.mark.real_default
def test_composer_model_and_effort_are_scoped_to_the_chat(tmp_path):
    from kova.config import ModelEntry, ProviderEntry

    cfg = load_config(tmp_path)
    cfg.files_path = tmp_path / "files"
    cloud = ProviderEntry("api", "Example API", "https://example.test/v1", kind="openai")
    cfg.providers.append(cloud)
    cfg.models[0].model = "demo-local"
    cfg.models.append(ModelEntry("reasoning", cloud.base_url, "gpt-5.2", provider=cloud.id))
    fake = FakeModel([text("One"), text("Two"), text("Three"), text("Four"), text("Five")])
    headers = {"Authorization": f"Bearer {ensure_token(cfg.home)}"}
    app = create_app(cfg, transport=fake.transport)
    with TestClient(app) as client:
        models = {m["name"]: m for m in client.get("/models", headers=headers).json()}
        assert models["local"]["local"] and models["local"]["reasoning_efforts"] == []
        assert not models["reasoning"]["local"] and models["reasoning"]["tools"]
        assert models["reasoning"]["reasoning_efforts"] == ["off", "low", "medium", "high"]
        chat = client.post("/chats", headers=headers, json={
            "mode": "chat", "model": "reasoning", "reasoning_effort": "high",
        }).json()
        other = client.post("/chats", headers=headers, json={"model": "local"}).json()
        path = f"/chats/{chat['id']}"

        def send(target, message):
            response = client.post(f"{target}/messages", headers=headers, json={"text": message})
            assert response.status_code == 200
            events = _parse_sse(response.text)
            assert events[-1]["type"] == "done"
            return events

        assert not any(e["type"] == "model_switch" for e in send(path, "first"))
        assert fake.requests[-1]["reasoning_effort"] == "high"
        assert client.get("/health").json()["model"] == "local"
        assert client.patch(path, headers=headers, json={"model": "missing"}).status_code == 404
        assert client.patch(path, headers=headers, json={
            "model": "local", "reasoning_effort": "high",
        }).status_code == 422
        assert client.patch(path, headers=headers, json={"reasoning_effort": "off"}).json()["reasoning_effort"] == "off"
        send(path, "still this model")
        assert fake.requests[-1]["reasoning_effort"] == "none"
        done = send(f"/chats/{other['id']}", "another chat")[-1]
        assert fake.requests[-1]["model"] == "demo-local"
        assert "reasoning_effort" not in fake.requests[-1]
        app.state.store.record_usage(other["id"], done["message_id"], {
            "model": "local", "local": True, "completion_tokens": 30, "ms_generation": 2000,
        })
        models = {m["name"]: m for m in client.get("/models", headers=headers).json()}
        assert models["local"]["last_local_speed"] == {"tokens_per_second": 15, "estimated": False}
        assert models["reasoning"]["last_local_speed"] is None

        switched = client.patch(path, headers=headers, json={"model": "local"}).json()
        assert (switched["model"], switched["reasoning_effort"], switched["mode"]) == ("local", None, "chat")
        events = send(path, "now local")
        assert [e for e in events if e["type"] == "model_switch"] == [{"type": "model_switch", "model": "local"}]
        saved = client.get(path, headers=headers).json()
        assert [m.get("model_switch") for m in saved["messages"] if m["role"] == "user"] == [None, None, "local"]
        fork = client.post(f"{path}/fork", headers=headers).json()
        assert fork["model"] == "local"
        assert fork["messages"][-2]["model_switch"] == "local"
        assert not any({"turn_model", "model_switch"} & m.keys() for request in fake.requests for m in request["messages"])

        response = client.post("/models/active", headers=headers, json={
            "name": "reasoning", "reasoning_effort": "medium",
        })
        assert response.status_code == 200
        assert load_config(cfg.home).active_model().reasoning_effort == "medium"
        assert client.get("/health").json()["model"] == "reasoning"
        assert client.get(path, headers=headers).json()["model"] == "local"
        default_chat = client.post("/chats", headers=headers, json={"mode": "chat"}).json()
        send(f"/chats/{default_chat['id']}", "use my default")
        assert fake.requests[-1]["reasoning_effort"] == "medium"


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
        ("patch", "/models", {"name": "m", "vision": True}),
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
    body = response.json()
    assert response.status_code == 200
    assert (body["version"], body["model"], body["model_ok"]) == (__version__, "local", False)
    assert body["model_checked_at"]


def test_health_cached_path_does_not_await_ping(tmp_path, monkeypatch):
    """A warm /health must not touch the provider, even if a probe would hang (#98)."""
    from kova.models import ModelClient

    monkeypatch.setenv("KOVA_HOME", str(tmp_path))
    cfg = load_config()
    app = create_app(cfg, transport=httpx.MockTransport(lambda request: httpx.Response(200)))
    with TestClient(app) as client:
        assert client.get("/health").json()["model_ok"] is True

        async def hang(self):
            raise AssertionError("/health awaited ping() on the cached path")

        monkeypatch.setattr(ModelClient, "ping", hang)
        assert client.get("/health").json()["model_ok"] is True
        assert client.get("/health?probe=0").json()["model_ok"] is True


def test_health_probe_forces_a_live_check(tmp_path, monkeypatch):
    """``?probe=1`` refreshes the cached value instead of returning it (#98)."""
    monkeypatch.setenv("KOVA_HOME", str(tmp_path))
    cfg = load_config()
    provider = {"up": True, "probes": 0}

    def respond(request):
        if request.url.path.endswith("/models"):
            provider["probes"] += 1
            return httpx.Response(200 if provider["up"] else 503, json={"data": []})
        return httpx.Response(404)

    app = create_app(cfg, transport=httpx.MockTransport(respond))
    with TestClient(app) as client:
        assert client.get("/health").json()["model_ok"] is True
        provider["up"] = False
        assert client.get("/health").json()["model_ok"] is True  # still within the TTL
        forced = client.get("/health?probe=1").json()
    assert forced["model_ok"] is False
    assert provider["probes"] == 2


def test_health_probes_once_per_ttl_across_windows(tmp_path, monkeypatch):
    """Many windows asking in the same TTL share one provider request (#98)."""
    monkeypatch.setenv("KOVA_HOME", str(tmp_path))
    cfg = load_config()
    probes = {"count": 0}

    def respond(request):
        if request.url.path.endswith("/models"):
            probes["count"] += 1
            return httpx.Response(200, json={"data": []})
        return httpx.Response(404)

    app = create_app(cfg, transport=httpx.MockTransport(respond))
    with TestClient(app) as client:
        for _ in range(5):
            assert client.get("/health").json()["model_ok"] is True
            assert client.get("/health?probe=0").json()["model_ok"] is True
    assert probes["count"] == 1


def test_chat_stream_events(server):
    client, headers, cfg, fake = server
    chat_id = client.post("/chats", headers=headers).json()["id"]
    response = client.post(f"/chats/{chat_id}/messages", json={"text": "hello"}, headers=headers)
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    events = _parse_sse(response.text)
    assert events[-1]["type"] == "done"
    assert isinstance(events[-1]["message_id"], int)
    assert events[-2]['type'] == 'egress'
    assert events[-2]['items'][0]['kind'] == 'model'
    assert all(event["type"] == "text" for event in events[:-2])
    assert "".join(event.get("text", "") for event in events[:-1]) == "Hello there!"
    assert events[-1]["usage"]["completion_tokens"] > 0
    messages = client.get(f"/chats/{chat_id}", headers=headers).json()["messages"]
    usage = messages[1].pop("usage")
    assert messages[1]["id"] == events[-1]["message_id"]
    assert messages[1]["created"] == events[-1]["created"]
    assert messages[0]["created"] <= messages[1]["created"]
    for message in messages:
        message.pop("id")
        message.pop("created")
    assert messages == [{"role": "user", "content": "hello"},
                         {"role": "assistant", "content": "Hello there!"}]
    # The fake server sends no usage block, so the counts are marked as estimates.
    assert usage == events[-1]["usage"] and usage["estimated"] is True


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
    monkeypatch.setenv('USERPROFILE', str(home))  # Windows expands ~ from this
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
    assert response.json() == {"id": chat_id, "title": "Groceries", "mode": "work", "web": True, "permission": "manual", "cwd": str(cfg.files_path), "pinned": False, "colour": None, "icon": None}
    assert client.get("/chats", headers=headers).json()[0]["title"] == "Groceries"

    assert client.patch("/chats/abc", json={"title": "x"}, headers=headers).status_code == 404
    assert client.patch(f"/chats/{chat_id}", json={"title": "  "}, headers=headers).status_code == 422


def test_pin_and_unpin_chat(server):
    client, headers, cfg, fake = server
    older = client.post("/chats", headers=headers).json()["id"]
    newer = client.post("/chats", headers=headers).json()["id"]

    pinned = client.patch(f"/chats/{older}", json={"pinned": True}, headers=headers)
    assert pinned.json()["pinned"] is True
    assert client.get(f"/chats/{older}", headers=headers).json()["pinned"] is True
    listed = client.get("/chats", headers=headers).json()
    assert [chat["id"] for chat in listed] == [older, newer]
    assert [chat["pinned"] for chat in listed] == [True, False]

    # A pin is its own change; no other setting has to travel with it.
    unpinned = client.patch(f"/chats/{older}", json={"pinned": False}, headers=headers)
    assert unpinned.json()["pinned"] is False
    assert [chat["id"] for chat in client.get("/chats", headers=headers).json()] == [newer, older]
    assert client.patch("/chats/missing", json={"pinned": True}, headers=headers).status_code == 404


def test_incognito_chat_can_be_pinned_in_ram(server):
    client, headers, cfg, fake = server
    chat = client.post("/chats", json={"ephemeral": True, "title": "Incognito"}, headers=headers).json()
    assert client.patch(f"/chats/{chat['id']}", json={"pinned": True}, headers=headers).json()["pinned"] is True
    assert client.get(f"/chats/{chat['id']}", headers=headers).json()["pinned"] is True
    assert any(item["id"] == chat["id"] and item["pinned"]
               for item in client.get("/chats", headers=headers).json())


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


def test_file_mentions_stay_inside_the_chats_working_folder(server, tmp_path):
    """@ lists relative names only, under the chat's own folder (#197)."""
    client, headers, cfg, fake = server
    project = tmp_path / "project"
    (project / "src").mkdir(parents=True)
    (project / "src" / "main.py").write_text("print('hi')\n")
    (project / "src" / "math.py").write_text("1 + 1\n")
    (project / "readme.md").write_text("notes\n")
    (project / ".git").mkdir()
    (project / ".git" / "config").write_text("[core]\n")
    outside = tmp_path / "secret.txt"
    outside.write_text("not this one\n")
    (cfg.files_path).mkdir(parents=True, exist_ok=True)
    (cfg.files_path / "default.txt").write_text("default folder\n")

    chat = client.post("/chats", headers=headers, json={"cwd": str(project)}).json()
    response = client.get(f"/chats/{chat['id']}/files", headers=headers)
    assert response.status_code == 200
    paths = response.json()["paths"]
    assert "src/main.py" in paths
    assert "readme.md" in paths
    assert "src/" in paths  # folders are browsable by the next @token
    assert all(not path.startswith(("/", "..")) for path in paths)
    assert not any("secret" in path or ".git" in path for path in paths)

    filtered = client.get(f"/chats/{chat['id']}/files?q=MAIN", headers=headers).json()["paths"]
    assert filtered == ["src/main.py"]
    assert len(client.get(f"/chats/{chat['id']}/files?limit=1", headers=headers).json()["paths"]) == 1

    # Before a chat exists the composer asks the default folder.
    default = client.get("/files", headers=headers).json()["paths"]
    assert "default.txt" in default
    assert "src/main.py" not in default

    assert client.get("/chats/missing/files", headers=headers).status_code == 404
    assert client.get("/chats", headers={}).status_code == 401
    assert client.get("/files", headers={}).status_code == 401


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


def test_consolidate_reports_a_model_failure(server):
    """An unreachable model is an expected condition, not an empty 500 (#114)."""
    client, headers, cfg, fake = server
    assert client.post('/memory', json={'title': 'Dog', 'body': 'Rex'}, headers=headers).status_code == 200
    def refuse(request):
        return httpx.Response(503, json={'error': 'down'})
    client.app.state.agent.transport = httpx.MockTransport(refuse)
    response = client.post('/memory/consolidate', json={'dry_run': True}, headers=headers)
    assert response.status_code == 502
    assert 'HTTP 503' in response.json()['detail']


def test_no_model_after_removing_the_last_provider(server):
    """An empty model list is a setup state, not a raw KeyError (#92)."""
    client, headers, cfg, fake = server
    chat_id = client.post('/chats', headers=headers).json()['id']
    cfg.models.clear()
    cfg.active = ''
    response = client.post(f'/chats/{chat_id}/messages', json={'text': 'hi'}, headers=headers)
    assert response.status_code == 200
    events = _parse_sse(response.text)
    assert events[-1] == {'type': 'error', 'message': 'No model is set up yet. Run: kova setup'}
    compact = client.post(f'/chats/{chat_id}/compact', headers=headers)
    assert compact.status_code == 422
    assert compact.json()['detail'] == 'No model is set up yet. Run: kova setup'
    consolidate = client.post('/memory/consolidate', json={'dry_run': True}, headers=headers)
    assert consolidate.status_code == 422
    assert consolidate.json()['detail'] == 'No model is set up yet. Run: kova setup'


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


def test_a_fresh_app_knows_its_events_keepalive(tmp_path):
    """create_app sets the /events ping interval itself; without it /events is a 500."""
    from kova.server import EVENTS_KEEPALIVE
    app = create_app(load_config(tmp_path / "home"))
    assert app.state.events_keepalive == EVENTS_KEEPALIVE


def test_events_stream_pings_so_a_dead_stream_can_be_noticed(tmp_path):
    """/events pings at once and on every quiet keepalive interval (#93)."""
    import socket
    import threading
    import uvicorn
    cfg = load_config(tmp_path / "home")
    app = create_app(cfg)
    app.state.events_keepalive = 0.05
    listener = socket.socket(); listener.bind(("127.0.0.1", 0))
    port = listener.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
    thread.start()
    deadline = time.monotonic() + 30
    while not server.started and time.monotonic() < deadline:
        time.sleep(.01)
    try:
        headers = {"Authorization": f"Bearer {ensure_token(cfg.home)}"}
        seen = []
        with httpx.stream("GET", f"http://127.0.0.1:{port}/events", headers=headers, timeout=5) as response:
            assert response.status_code == 200
            for line in response.iter_lines():
                if line.startswith("event: "):
                    seen.append(line[len("event: "):])
                if len(seen) == 3:
                    break
        assert seen == ["ping", "ping", "ping"]
    finally:
        server.should_exit = True
        thread.join(5)
        listener.close()


def test_autostart_settings(server, monkeypatch):
    from kova import autostart
    client, headers, *_ = server
    calls = []
    monkeypatch.setattr(autostart, 'enable', lambda: calls.append('on'))
    monkeypatch.setattr(autostart, 'disable', lambda: calls.append('off'))
    monkeypatch.setattr(autostart, 'status', lambda: autostart.Status('linux', 'desktop', True, enabled=True, stale=True))
    for method, path in [('get', '/autostart'), ('post', '/autostart/on'), ('post', '/autostart/off')]:
        assert getattr(client, method)(path).status_code == 401
        response = getattr(client, method)(path, headers=headers)
        assert response.status_code == 200
        assert response.json() == {'available': True, 'enabled': True, 'stale': True}
    assert calls == ['on', 'off']
    assert client.post('/autostart/invalid', headers=headers).status_code == 400
    def fail():
        raise autostart.AutostartError('private diagnostic')
    monkeypatch.setattr(autostart, 'status', fail)
    response = client.get('/autostart', headers=headers)
    assert response.status_code == 503
    assert 'private diagnostic' not in response.text

class _FakeTranscriber:
    """A transcriber that returns a fixed result for any audio."""
    def __init__(self, result=None):
        self.result = result or TranscriptionResult("hi there")
        self.calls = 0

    def transcribe(self, audio, fmt="webm"):
        self.calls += 1
        return self.result


def _audio_file(payload=b"hello", name="t.webm"):
    return {"audio": (name, payload, "audio/webm")}


def test_transcribe_off_without_transcriber(server):
    client, headers, *_ = server
    # No whisper.cpp in the test env, so the server reports transcription off.
    client.app.state.transcriber = None
    response = client.post("/transcribe", files=_audio_file(), headers=headers)
    assert response.status_code == 409
    assert "transcription is off" in response.text.lower()


def test_transcribe_returns_the_result(server):
    client, headers, *_ = server
    client.app.state.transcriber = _FakeTranscriber()
    response = client.post("/transcribe", files=_audio_file(), headers=headers)
    assert response.status_code == 200
    assert response.json() == {"text": "hi there", "language": None,
                               "backend": "whisper.cpp", "duration_ms": None}


def test_transcribe_rejects_audio_that_is_too_large(server):
    client, headers, *_ = server
    client.app.state.transcriber = _FakeTranscriber()
    response = client.post("/transcribe",
                           files=_audio_file(b"x" * (MAX_AUDIO_BYTES + 2)),
                           headers=headers)
    assert response.status_code == 413
    assert "too large" in response.text.lower()


def test_transcribe_maps_a_transcription_error(server):
    client, headers, *_ = server

    class _FailingTranscriber(_FakeTranscriber):
        def transcribe(self, audio, fmt="webm"):
            from kova.voice import TranscriptionError
            raise TranscriptionError("the audio is corrupt")

    client.app.state.transcriber = _FailingTranscriber()
    response = client.post("/transcribe", files=_audio_file(), headers=headers)
    assert response.status_code == 422
    assert "the audio is corrupt" in response.text


def test_transcribe_requires_auth(server):
    client, headers, *_ = server
    client.app.state.transcriber = _FakeTranscriber()
    assert client.post("/transcribe", files=_audio_file()).status_code == 401


def test_projects_crud_and_chat_membership(server, tmp_path):
    client, headers, cfg, _ = server
    folder = tmp_path / "workspace"
    other = tmp_path / "other"
    folder.mkdir()
    other.mkdir()

    assert client.get("/projects").status_code == 401
    assert client.get("/projects", headers=headers).json() == []
    for body in ({"name": "   "}, {"name": "Example", "cwd": str(folder / "missing")}):
        assert client.post("/projects", headers=headers, json=body).status_code == 422
    created = client.post("/projects", headers=headers, json={
        "name": " Research ", "instructions": "Use plain language.", "cwd": str(folder),
    })
    assert created.status_code == 200, created.text
    project = created.json()
    assert project["name"] == "Research"
    assert project["created"] == project["updated"]
    path = f"/projects/{project['id']}"
    assert client.get(path, headers=headers).json() == project
    assert client.get("/projects", headers=headers).json() == [project]
    assert client.patch(path, headers=headers, json={"name": None}).status_code == 422
    assert client.patch(path, headers=headers, json={}).status_code == 422

    assert client.post("/chats", headers=headers, json={"project_id": "missing"}).status_code == 404
    assert client.post("/chats", headers=headers, json={
        "project_id": project["id"], "ephemeral": True,
    }).status_code == 422
    chat = client.post("/chats", headers=headers, json={"project_id": project["id"]}).json()
    assert chat["cwd"] == str(folder)
    chat_path = f"/chats/{chat['id']}"
    fork = client.post(f"{chat_path}/fork", headers=headers).json()
    assert fork["project_id"] == project["id"]
    explicit = client.post("/chats", headers=headers, json={
        "project_id": project["id"], "cwd": str(other),
    }).json()
    assert explicit["cwd"] == str(other)

    edited = client.patch(path, headers=headers, json={"name": "Archive", "cwd": None}).json()
    assert edited["name"] == "Archive"
    assert edited["instructions"] == project["instructions"]
    assert edited["cwd"] is None and edited["created"] == project["created"]
    assert edited["updated"] > project["updated"]
    new_chat = client.post("/chats", headers=headers, json={"project_id": project["id"]}).json()
    assert new_chat["cwd"] == str(cfg.files_path)
    assert {row["project_id"] for row in client.get("/chats", headers=headers).json()} == {project["id"]}
    before = client.get(chat_path, headers=headers).json()
    assert before["cwd"] == str(folder)

    assert client.delete(path, headers=headers).json() == {"ok": True}
    assert client.delete(path, headers=headers).status_code == 404
    assert client.get(path, headers=headers).status_code == 404
    assert client.get(chat_path, headers=headers).json() == {**before, "project_id": None}
    assert {row["project_id"] for row in client.get("/chats", headers=headers).json()} == {None}
    assert folder.is_dir() and other.is_dir()


def test_project_instructions_are_live_in_chat_prompts(server, tmp_path):
    client, headers, _, fake = server
    fake.scripts[:] = [text("One"), text("Two"), text("Three"), text("Four")]
    (tmp_path / "AGENTS.md").write_text("Repository rule: write a summary.", encoding="utf-8")
    project = client.post("/projects", headers=headers, json={
        "name": "Research", "instructions": "Project rule: use metric units.", "cwd": str(tmp_path),
    })
    assert project.status_code == 200, project.text
    project_id = project.json()["id"]
    work = client.post("/chats", headers=headers, json={"project_id": project_id}).json()
    chat_mode = client.post("/chats", headers=headers, json={"project_id": project_id, "mode": "chat"}).json()
    ungrouped = client.post("/chats", headers=headers, json={"cwd": str(tmp_path)}).json()

    def prompt(chat):
        result = client.post(f"/chats/{chat['id']}/messages", headers=headers, json={"text": "Hello"})
        assert result.status_code == 200 and _parse_sse(result.text)[-1]["type"] == "done"
        assert fake.requests[-1]["messages"][0]["role"] == "system"
        return fake.requests[-1]["messages"][0]["content"]

    work_prompt = prompt(work)
    assert "Repository rule: write a summary." in work_prompt
    assert "Project rule: use metric units." in work_prompt
    assert "Project rule: use metric units." not in prompt(ungrouped)
    assert "Project rule: use metric units." in prompt(chat_mode)
    assert client.patch(f"/projects/{project_id}", headers=headers, json={
        "instructions": "Project rule: round to two places.",
    }).status_code == 200
    updated_prompt = prompt(work)
    assert "Project rule: round to two places." in updated_prompt
    assert "Project rule: use metric units." not in updated_prompt
    saved = client.get(f"/chats/{work['id']}", headers=headers).json()
    assert all("Project rule:" not in (message["content"] or "") for message in saved["messages"])
