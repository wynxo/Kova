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
from kova.config import ensure_token, load_config
from kova.pairing import (CAPABILITIES, DEFAULT_CAPABILITIES, DEVICE_COOKIE, Devices,
                          device_capabilities, require_capability)
from kova.server import create_app

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
def kova(tmp_path):
    """One app, reachable as the computer (bearer) or as a phone on the LAN listener."""
    cfg = load_config(tmp_path)
    app = create_app(cfg)

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
