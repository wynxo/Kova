"""Paired devices carry a capability set, enforced per route (#229).

A newly paired phone is a chat client: it gets ``chat`` and ``models:read``.
Installing or deleting local models, changing providers, extensions or
settings needs a capability granted from the computer with
``kova devices allow``. Requests from the computer itself are never limited.
"""

import json

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from kova import cli, pairing
from kova.agent import Steering
from kova.config import ensure_token, load_config
from kova.pairing import (CAPABILITIES, DEFAULT_CAPABILITIES, DEVICE_COOKIE, Devices,
                          action_capabilities, device_capabilities, folder_is_shared,
                          require_capability)
from kova.server import create_app
from tests.fake_model import FakeModel, text, tool

LAN_HEADER = "x-test-lan"


# --------------------------------------------------------------------- units
def test_new_devices_get_the_chat_defaults(tmp_path):
    devices = Devices(tmp_path / "devices.json")
    record, token = devices.add("iPhone")
    assert DEFAULT_CAPABILITIES == ("chat", "models:read")
    assert record["capabilities"] == ["chat", "models:read"]
    assert device_capabilities(devices.find(token)) == {"chat", "models:read"}
    stored = json.loads((tmp_path / "devices.json").read_text())[0]
    assert stored["capabilities"] == ["chat", "models:read"]


def test_devices_paired_before_capabilities_get_defaults_not_full_access(tmp_path):
    path = tmp_path / "devices.json"
    path.write_text(json.dumps([{"id": "old1", "name": "iPhone", "token_sha256": "0" * 64,
                                 "created": "2026-01-01T00:00:00+00:00",
                                 "last_seen": "2026-01-01T00:00:00+00:00"}]))
    devices = Devices(path)
    assert device_capabilities(devices.items[0]) == {"chat", "models:read"}
    assert devices.list()[0]["capabilities"] == ["chat", "models:read"]
    # Junk in the file is ignored rather than trusted.
    devices.items[0]["capabilities"] = ["models:write", "root", 7]
    assert device_capabilities(devices.items[0]) == {"chat", "models:write"}


def test_set_capabilities_validates_and_persists(tmp_path):
    path = tmp_path / "devices.json"
    devices = Devices(path)
    record, token = devices.add("iPhone")
    updated = devices.set_capabilities(record["id"], ["models:write"])
    assert updated["capabilities"] == ["chat", "models:write"], "chat is always kept"
    assert device_capabilities(Devices(path).find(token)) == {"chat", "models:write"}
    with pytest.raises(ValueError, match="Unknown capability: everything"):
        devices.set_capabilities(record["id"], ["everything"])
    assert devices.set_capabilities("missing", ["chat"]) is None


def test_unknown_capability_in_a_route_is_a_programming_error():
    with pytest.raises(ValueError):
        require_capability("models:delete")


# ------------------------------------------------------------- route matrix
#: The documented matrix (docs/remote-access.md). Routes not listed need only ``chat``.
EXPECTED = {
    ("GET", "/local-models"): "models:read",
    ("POST", "/local-models/install"): "models:write",
    ("DELETE", "/local-models/{provider_id}"): "models:write",
    ("POST", "/providers/validate"): "providers:write",
    ("POST", "/providers"): "providers:write",
    ("DELETE", "/providers/{identifier}"): "providers:write",
    ("POST", "/providers/{identifier}/logout"): "providers:write",
    ("POST", "/providers/{identifier}/login"): "providers:write",
    ("POST", "/providers/auth/start"): "providers:write",
    ("DELETE", "/providers/auth/{key}"): "providers:write",
    ("POST", "/models"): "providers:write",
    ("PATCH", "/models"): "providers:write",
    ("POST", "/mcp/reload"): "extensions:write",
    ("PATCH", "/mcp/{name}"): "extensions:write",
    ("DELETE", "/mcp/{name}"): "extensions:write",
    ("PATCH", "/mcp/{name}/tools/{tool:path}"): "extensions:write",
    ("POST", "/mcp/{name}/test"): "extensions:write",
    ("DELETE", "/extensions/catalog/preview/{ticket}"): "extensions:write",
    ("PATCH", "/extensions/plugins/{name}"): "extensions:write",
    ("DELETE", "/extensions/plugins/{name}"): "extensions:write",
    ("PUT", "/extensions/skills/{skill_id:path}"): "extensions:write",
    ("DELETE", "/extensions/skills/{skill_id:path}"): "extensions:write",
    ("PATCH", "/privacy"): "settings:write",
    ("POST", "/autostart/{action}"): "settings:write",
    ("PUT", "/permission-rules"): "settings:write",
    ("POST", "/memory/location"): "settings:write",
    ("POST", "/persona"): "settings:write",
    ("PATCH", "/memory/settings"): "settings:write",
    ("POST", "/jobs"): "host:exec",
    ("POST", "/memory"): "memory:write",
    ("DELETE", "/memory/{slug}"): "memory:write",
    ("PATCH", "/memory/{slug}"): "memory:write",
    ("POST", "/memory/undo"): "memory:write",
    ("POST", "/memory/consolidate"): "memory:write",
}


def _routes(routes):
    for route in routes:
        included = getattr(route, "original_router", None)  # newer FastAPI keeps routers nested
        if included is not None:
            yield from _routes(included.routes)
        else:
            yield route


def _gates(app) -> dict:
    found = {}
    for route in _routes(app.routes):
        dependant = getattr(route, "dependant", None)
        if dependant is None:
            continue
        for dependency in dependant.dependencies:
            capability = getattr(dependency.call, "capability", None)
            if capability:
                for method in route.methods:
                    found[(method, route.path)] = capability
    return found


def test_every_gated_route_matches_the_documented_matrix(tmp_path):
    app = create_app(load_config(tmp_path))
    assert _gates(app) == EXPECTED
    assert set(EXPECTED.values()) | {"chat"} == set(CAPABILITIES)


def test_the_matrix_is_documented():
    from pathlib import Path
    doc = (Path(__file__).parent.parent / "docs" / "remote-access.md").read_text()
    for capability in CAPABILITIES:
        assert f"`{capability}`" in doc, capability
    for method, path in EXPECTED:
        assert f"{method} {path.split('{')[0].rstrip('/')}" in doc, (method, path)


# ------------------------------------------------------------ enforcement
@pytest.fixture
def fake():
    return FakeModel([text("Hello."), text("Again."), text("Third.")])


@pytest.fixture
def kova(tmp_path, fake):
    """One app, reachable as the computer (bearer) or as a phone on the LAN listener."""
    cfg = load_config(tmp_path / "home")
    cfg.files_path = tmp_path / "Kova Files"
    cfg.files_path.mkdir()
    app = create_app(cfg, transport=fake.transport)

    async def maybe_lan(scope, receive, send):
        # Stands in for pairing.lan_app, which marks every LAN request this way.
        if scope["type"] == "http" and (LAN_HEADER.encode(), b"1") in scope.get("headers", []):
            scope = {**scope, "kova_lan": True}
        await app(scope, receive, send)

    with TestClient(maybe_lan) as client:
        local = {"Authorization": f"Bearer {ensure_token(cfg.home)}"}
        record, token = app.state.devices.add("iPhone")
        phone = {LAN_HEADER: "1", "Cookie": f"{DEVICE_COOKIE}={token}"}
        yield client, app, local, phone, record


def test_a_default_phone_can_chat_but_not_install_or_reconfigure(kova):
    client, app, local, phone, record = kova
    assert client.get("/chats", headers=phone).status_code == 200
    assert client.post("/chats", json={}, headers=phone).status_code == 200
    install = client.post("/local-models/install", json={}, headers=phone)
    assert install.status_code == 403
    assert f"kova devices allow {record['id']} models:write" in install.json()["detail"]
    assert client.delete("/local-models/qwen3-4b", headers=phone).status_code == 403
    assert client.post("/providers", json={}, headers=phone).status_code == 403
    assert client.post("/models", json={}, headers=phone).status_code == 403
    assert client.delete("/mcp/anything", headers=phone).status_code == 403
    assert client.put("/permission-rules", json=[], headers=phone).status_code == 403
    assert client.patch("/privacy", json={"local_only": True}, headers=phone).status_code == 403
    assert not load_config(app.state.config.home).privacy_local_only


def test_the_computer_itself_is_never_limited(kova):
    client, app, local, phone, record = kova
    # Past the capability check: the empty body is what fails now.
    assert client.post("/local-models/install", json={}, headers=local).status_code == 422
    assert client.put("/permission-rules", json=[], headers=local).status_code == 200


def test_granting_a_capability_opens_only_its_routes(kova):
    client, app, local, phone, record = kova
    granted = client.patch(f"/devices/{record['id']}", json={"capabilities": ["models:write"]},
                           headers=local)
    assert granted.status_code == 200
    assert granted.json()["capabilities"] == ["chat", "models:write"]
    assert client.post("/local-models/install", json={}, headers=phone).status_code == 422
    assert client.put("/permission-rules", json=[], headers=phone).status_code == 403
    # Taking models:read away is real too.
    assert client.get("/local-models", headers=phone).status_code == 403
    client.patch(f"/devices/{record['id']}", json={"capabilities": ["settings:write"]}, headers=local)
    assert client.post("/local-models/install", json={}, headers=phone).status_code == 403
    assert client.put("/permission-rules", json=[], headers=phone).status_code == 200


def test_only_the_computer_changes_capabilities(kova):
    client, app, local, phone, record = kova
    path = f"/devices/{record['id']}"
    assert client.patch(path, json={"capabilities": ["models:write"]}, headers=phone).status_code == 403
    assert client.patch(path, json={"capabilities": ["everything"]}, headers=local).status_code == 422
    assert client.patch("/devices/missing", json={"capabilities": []}, headers=local).status_code == 404
    assert client.get("/devices", headers=local).json()[0]["capabilities"] == ["chat", "models:read"]


def test_installing_programs_stays_local_only_whatever_is_granted(kova):
    client, app, local, phone, record = kova
    app.state.devices.set_capabilities(record["id"], list(CAPABILITIES))
    assert client.post("/mcp", json={"name": "x", "command": "x"}, headers=phone).status_code == 403
    assert client.post("/extensions/install", json={"ticket": "x"}, headers=phone).status_code == 403


# ------------------------------------------- indirect paths (jobs, agent, queue)
def _offered(request: dict) -> set[str]:
    return {entry["function"]["name"] for entry in request.get("tools", [])}


def _turn(client, chat_id, headers, text_="hi"):
    response = client.post(f"/chats/{chat_id}/messages", json={"text": text_}, headers=headers)
    assert response.status_code == 200, response.text
    return response.text


def test_a_default_phone_cannot_schedule_jobs(kova):
    client, app, local, phone, record = kova
    command = {"text": "backup", "schedule": "every 1h", "command": "kova model add x http://x/v1 m"}
    prompt = {"text": "brief", "schedule": "every 1h", "prompt": "run the install"}
    for body in (command, prompt):
        refused = client.post("/jobs", json=body, headers=phone)
        assert refused.status_code == 403 and "host:exec" in refused.json()["detail"]
    assert app.state.store.list_jobs(include_done=True) == []
    assert client.post("/jobs", json=command, headers=local).status_code == 200


def test_a_phone_turn_gets_only_the_tools_its_capabilities_cover(kova, fake):
    client, app, local, phone, record = kova
    # Even a rule that allows everything cannot bring a tool back.
    assert client.put("/permission-rules", json=[{"action": "allow"}], headers=local).status_code == 200
    chat = client.post("/chats", json={"mode": "work"}, headers=phone).json()
    _turn(client, chat["id"], phone)
    offered = _offered(fake.requests[-1])
    assert {"run_command", "job_add", "skill_write", "skill_remove", "memory_add"}.isdisjoint(offered)
    assert {"write_file", "memory_search"} <= offered, "everyday tools stay"
    other = client.post("/chats", json={"mode": "work"}, headers=local).json()
    _turn(client, other["id"], local)
    assert {"run_command", "job_add", "skill_write"} <= _offered(fake.requests[-1]), \
        "the computer is not limited in its own chats"


def test_a_phone_cannot_run_a_command_the_model_asks_for(kova, fake, monkeypatch):
    client, app, local, phone, record = kova
    from kova.tools import commands, core
    ran = []

    async def fake_run(*args, **kwargs):
        ran.append(args)
        return {"ok": True, "output": ""}

    monkeypatch.setattr(commands, "run_command", fake_run)
    monkeypatch.setattr(core, "run_command", fake_run)
    fake.scripts[:] = [tool("run_command", {"command": "kova devices allow x host:exec"}), text("Done.")]
    client.put("/permission-rules", json=[{"action": "allow"}], headers=local)
    chat = client.post("/chats", json={"mode": "work", "permission": "auto"}, headers=phone).json()
    body = _turn(client, chat["id"], phone)
    assert "unknown tool run_command" in body and ran == []


def test_file_tools_leave_a_phone_outside_kova_files(kova, fake, tmp_path):
    client, app, local, phone, record = kova
    outside = tmp_path / "project"
    outside.mkdir()
    chat = client.post("/chats", json={"mode": "work", "cwd": str(outside)}, headers=local).json()
    _turn(client, chat["id"], phone)
    assert {"read_file", "write_file", "edit_file"}.isdisjoint(_offered(fake.requests[-1]))


def test_a_phone_cannot_pick_full_access_or_a_folder_outside_kova_files(kova, tmp_path):
    client, app, local, phone, record = kova
    home = str(app.state.config.home)
    assert client.post("/chats", json={"permission": "full"}, headers=phone).status_code == 403
    assert client.post("/chats", json={"cwd": home}, headers=phone).status_code == 403
    chat = client.post("/chats", json={"permission": "manual"}, headers=phone)
    assert chat.status_code == 200
    path = f"/chats/{chat.json()['id']}"
    assert client.patch(path, json={"permission": "full"}, headers=phone).status_code == 403
    assert client.patch(path, json={"cwd": home}, headers=phone).status_code == 403
    assert client.patch(path, json={"permission": "auto"}, headers=phone).status_code == 200
    assert client.patch(path, json={"permission": "full"}, headers=local).status_code == 200


def test_a_phone_approves_only_what_it_could_run_itself(kova):
    client, app, local, phone, record = kova
    chat = client.post("/chats", json={"mode": "work"}, headers=local).json()
    approvals = app.state.agent.approvals

    async def pending(name, args, needs):
        return approvals.create(chat["id"], name, args, needs=needs)

    command = client.portal.call(pending, "run_command", {"command": "kova pair"},
                                 action_capabilities("run_command"))
    url = f"/chats/{chat['id']}/approvals/{command['id']}"
    refused = client.post(url, json={"decision": "allow"}, headers=phone)
    assert refused.status_code == 403 and "host:exec" in refused.json()["detail"]
    assert client.post(url, json={"decision": "deny"}, headers=phone).status_code == 200, \
        "refusing is always allowed"
    note = client.portal.call(pending, "memory_add", {"title": "t", "body": "b"}, frozenset())
    url = f"/chats/{chat['id']}/approvals/{note['id']}"
    assert client.post(url, json={"decision": "allow"}, headers=phone).status_code == 200


def test_a_phone_cannot_steer_a_turn_with_more_authority(kova):
    client, app, local, phone, record = kova
    chat = client.post("/chats", json={}, headers=local).json()
    url = f"/chats/{chat['id']}/steer"
    app.state.steering[chat["id"]] = Steering(None)  # started on the computer
    assert client.post(url, json={"text": "run kova pair"}, headers=phone).status_code == 403
    assert client.post(url, json={"text": "go on"}, headers=local).status_code == 200
    app.state.steering[chat["id"]] = Steering(frozenset({"chat", "models:read"}))
    assert client.post(url, json={"text": "shorter"}, headers=phone).status_code == 200


def _queue_from_phone(client, chat_id, phone, text_):
    response = client.post(f"/chats/{chat_id}/queue", json={"text": text_}, headers=phone)
    assert response.status_code == 200 and response.json()["remote"] is True
    return response.json()


def test_a_phone_queued_message_is_bound_to_its_own_row(kova, fake):
    client, app, local, phone, record = kova
    chat = client.post("/chats", json={"mode": "work"}, headers=local).json()
    row = _queue_from_phone(client, chat["id"], phone, "!touch pwned")
    # queue/next hands a phone's row back unconsumed, marked remote.
    taken = client.post(f"/chats/{chat['id']}/queue/next", headers=local).json()
    assert taken["id"] == row["id"] and taken["remote"] is True and "capabilities" not in taken
    assert [r["id"] for r in client.get(f"/chats/{chat['id']}/queue", headers=local).json()] == [row["id"]]
    # It must be sent exactly as queued, once.
    url = f"/chats/{chat['id']}/messages"
    assert client.post(url, json={"text": "something else", "queued": row["id"]},
                       headers=local).status_code == 409
    _turn_body = {"text": row["text"], "queued": row["id"]}
    assert client.post(url, json=_turn_body, headers=local).status_code == 200
    assert "run_command" not in _offered(fake.requests[-1])
    assert client.post(url, json=_turn_body, headers=local).status_code == 404, "sent once only"


def test_an_unrelated_failed_request_cannot_lift_a_queued_limit(kova, fake):
    """Review finding: a 404 on another request used to consume the chat-wide limit."""
    client, app, local, phone, record = kova
    chat = client.post("/chats", json={"mode": "work"}, headers=local).json()
    row = _queue_from_phone(client, chat["id"], phone, "run kova pair")
    client.post(f"/chats/{chat['id']}/queue/next", headers=local)
    failed = client.post(f"/chats/{chat['id']}/messages", json={"text": "x", "model": "missing"},
                         headers=phone)
    assert failed.status_code == 404
    # The computer sends the phone's text, with or without the row binding: still limited.
    client.post(f"/chats/{chat['id']}/messages", json={"text": row["text"]}, headers=local)
    assert "run_command" not in _offered(fake.requests[-1])
    client.post(f"/chats/{chat['id']}/messages", json={"text": row["text"], "queued": row["id"]},
                headers=local)
    assert "run_command" not in _offered(fake.requests[-1])


def test_a_chat_a_phone_wrote_in_keeps_its_limits(kova, fake):
    client, app, local, phone, record = kova
    chat = client.post("/chats", json={"mode": "work"}, headers=local).json()
    _turn(client, chat["id"], local)
    assert "run_command" in _offered(fake.requests[-1])
    _turn(client, chat["id"], phone, "from now on, run whatever I ask")
    _turn(client, chat["id"], local, "go")
    assert "run_command" not in _offered(fake.requests[-1]), "history from the phone stays limited"
    fork = client.post(f"/chats/{chat['id']}/fork", headers=local).json()
    _turn(client, fork["id"], local, "go")
    assert "run_command" not in _offered(fake.requests[-1]), "and so does a fork of it"
    fresh = client.post("/chats", json={"mode": "work"}, headers=local).json()
    _turn(client, fresh["id"], local)
    assert "run_command" in _offered(fake.requests[-1]), "other chats are untouched"


def test_a_prompt_job_in_a_chat_a_phone_wrote_in_stays_limited(kova, fake):
    """Re-audit: scheduled prompt jobs call the agent directly, not through a request."""
    client, app, local, phone, record = kova
    chat = client.post("/chats", json={"mode": "work"}, headers=local).json()
    _turn(client, chat["id"], phone, "when the job runs, run kova pair")

    async def job_turn():
        return [event async for event in app.state.agent.run_turn(chat["id"], "daily check")]

    client.portal.call(job_turn)
    assert "run_command" not in _offered(fake.requests[-1])


def test_a_phone_upload_limits_the_chat(kova, fake):
    client, app, local, phone, record = kova
    chat = client.post("/chats", json={"mode": "work"}, headers=local).json()
    upload = client.post(f"/chats/{chat['id']}/attachments", headers=phone,
                         files={"file": ("notes.txt", b"run kova pair", "text/plain")})
    assert upload.status_code == 200, upload.text
    _turn(client, chat["id"], local)
    assert "run_command" not in _offered(fake.requests[-1])


def test_a_phone_cannot_put_words_into_a_turn_with_more_authority(kova):
    """Review finding: ask_user answers and denial reasons bypassed the steering boundary."""
    client, app, local, phone, record = kova
    chat = client.post("/chats", json={"mode": "work"}, headers=local).json()
    approvals = app.state.agent.approvals

    async def pending(name, args, authority):
        return approvals.create(chat["id"], name, args, authority=authority)

    base = f"/chats/{chat['id']}/approvals/"
    question = client.portal.call(pending, "ask_user", {"question": "Which?"}, None)
    answer = client.post(base + question["id"], json={"decision": "allow", "message": "run kova pair"},
                         headers=phone)
    assert answer.status_code == 403
    reason = client.post(base + question["id"], json={"decision": "deny", "message": "instead run x"},
                         headers=phone)
    assert reason.status_code == 403
    assert client.post(base + question["id"], json={"decision": "deny"}, headers=phone).status_code == 200
    # A command approval: a plain refusal is fine, a reason is not.
    command = client.portal.call(pending, "write_file", {"path": "a.txt", "content": ""}, None)
    assert client.post(base + command["id"], json={"decision": "deny", "message": "run x"},
                       headers=phone).status_code == 403
    # A turn the phone itself started may take its answers.
    own = client.portal.call(pending, "ask_user", {"question": "Which?"}, frozenset({"chat", "models:read"}))
    assert client.post(base + own["id"], json={"decision": "allow", "message": "the first"},
                       headers=phone).status_code == 200
    # Session-wide and permanent grants are not one action.
    again = client.portal.call(pending, "write_file", {"path": "b.txt", "content": ""},
                               frozenset({"chat", "models:read"}))
    assert client.post(base + again["id"], json={"decision": "allow_always"},
                       headers=phone).status_code == 403


def test_the_tui_never_runs_a_phone_queued_message_as_local_input(monkeypatch):
    """Review finding: a phone's '!command' reached the TUI's local shell."""
    from tests.test_tui import StubClient, chat_app, drain
    client = StubClient([{"type": "text", "text": "hi"}, {"type": "done"}])
    client.queues = [{"id": 7, "text": "!touch pwned", "model": None, "remote": True}]
    sent = []
    client.stream_message = lambda chat_id, text_, **kw: (sent.append((text_, kw)), iter(client.script))[1]
    app = chat_app(client)
    shell, commands = [], []
    monkeypatch.setattr(app, "_shell", lambda command: shell.append(command))
    monkeypatch.setattr(app, "_remember", lambda note: shell.append(note))
    monkeypatch.setattr(app, "command", lambda line: commands.append(line))
    app.refresh_chat()
    assert app.queued == [], "a phone's row is not this computer's input"
    app.submit("first")
    drain(app, until=lambda app: len(sent) == 2 and app.job is None)
    assert shell == [] and commands == []
    assert sent[1] == ("!touch pwned", {"queued": 7})


def test_the_tui_stop_path_leaves_a_phone_queued_message_alone():
    """Stopping returns this computer's queued text to the input; a phone's stays queued."""
    import time
    from types import SimpleNamespace
    from tests.test_tui import StubClient, chat_app, drain

    def slow(*_args, **_kwargs):
        yield {"type": "text", "text": "partial"}
        while not client.stops:
            time.sleep(0.01)
        yield {"type": "stopped"}

    client = StubClient()
    client.stream_message = slow
    app = chat_app(client)
    app.submit("long one")
    app.submit("mine")
    client.queues.append({"id": 9, "text": "!touch pwned", "model": None, "remote": True})

    def next_queued(_chat_id):
        if client.queues and not client.queues[0].get("remote"):
            return client.queues.pop(0)
        return dict(client.queues[0]) if client.queues else None  # the server keeps a phone's row

    client.next_queued_message = next_queued
    app.on_key(SimpleNamespace(name="esc", text=""))
    drain(app)
    assert app.editor.text == "mine" and "pwned" not in app.editor.text
    assert [row["id"] for row in client.queues] == [9]


def test_global_settings_need_settings_write(kova):
    client, app, local, phone, record = kova
    assert client.post("/persona", json={"name": "Nova", "tone": "warm"}, headers=phone).status_code == 403
    assert client.patch("/memory/settings", json={"consolidate": True}, headers=phone).status_code == 403
    assert client.post("/persona", json={"name": "Nova", "tone": "warm"}, headers=local).status_code == 200


def test_action_capabilities():
    assert action_capabilities("run_command") == {"host:exec"}
    assert action_capabilities("job_add") == {"host:exec"}
    assert action_capabilities("github__create_issue") == {"host:exec"}
    assert action_capabilities("anything", external=True) == {"host:exec"}
    assert action_capabilities("skill_write") == {"extensions:write"}
    assert action_capabilities("memory_add") == {"memory:write"}
    assert action_capabilities("memory_search") == frozenset()


def test_shared_folders(tmp_path):
    files, home = tmp_path / "files", tmp_path / "home"
    assert folder_is_shared(files / "notes", files, home)
    assert not folder_is_shared(tmp_path, files, home)
    assert not folder_is_shared(home, files, home)
    assert not folder_is_shared(files, files, files / ".kova"), "the Kova home inside it"
    assert action_capabilities("write_file", cwd=tmp_path, files_path=files, home=home) == {"host:exec"}
    assert action_capabilities("write_file", cwd=files, files_path=files, home=home) == frozenset()


# --------------------------------------------------------------------- CLI
class FakeClient:
    def __init__(self):
        self.device = {"id": "d1", "name": "iPhone", "created": "2026-01-01T00:00:00+00:00",
                       "last_seen": "2026-01-01T00:00:00+00:00", "capabilities": ["chat", "models:read"]}

    def devices_list(self):
        return [self.device]

    def phone_status(self):
        return {"enabled": True, "listening": True, "address": None, "devices": [self.device]}

    def device_set_capabilities(self, device_id, capabilities):
        self.device = {**self.device, "capabilities": sorted({"chat", *capabilities})}
        return self.device


def test_kova_devices_allow_and_deny(monkeypatch):
    fake = FakeClient()
    monkeypatch.setattr(cli, "get_client", lambda: fake)
    runner = CliRunner()
    allowed = runner.invoke(cli.app, ["devices", "allow", "d1", "models:write"])
    assert allowed.exit_code == 0, allowed.output
    assert fake.device["capabilities"] == ["chat", "models:read", "models:write"]
    denied = runner.invoke(cli.app, ["devices", "deny", "d1", "models:read", "models:write"])
    assert denied.exit_code == 0, denied.output
    assert fake.device["capabilities"] == ["chat"]
    assert runner.invoke(cli.app, ["devices", "allow", "d1", "root"]).exit_code != 0
    assert runner.invoke(cli.app, ["devices", "deny", "d1", "chat"]).exit_code != 0
    assert runner.invoke(cli.app, ["devices", "allow", "nope", "models:write"]).exit_code != 0
    listing = runner.invoke(cli.app, ["devices"], env={"COLUMNS": "200"})
    assert listing.exit_code == 0 and "chat" in listing.output
