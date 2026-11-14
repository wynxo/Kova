"""Phone pairing (#66): LAN listener, one-use codes, device cookies and the safety rails."""

import json
import socket
import threading
import time
from contextlib import contextmanager

import httpx
import pytest
import uvicorn
from typer.testing import CliRunner

from kova import cli, pairing
from kova.client import KovaClient
from kova.config import ensure_token, load_config, save_config
from kova.pairing import DEVICE_COOKIE, Devices, Pairing, device_name, is_private_client
from kova.server import create_app

LAN = "127.0.0.2"  # stands in for 192.168.x.y: a second local address on this machine
IPHONE = "Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15 Mobile/15E148"


def _can_bind(host):
    with socket.socket() as probe:
        try:
            probe.bind((host, 0))
            return True
        except OSError:
            return False


needs_lan = pytest.mark.skipif(not _can_bind(LAN), reason="this OS has no 127.0.0.2 to stand in for a LAN address")


# --------------------------------------------------------------------- units
def test_private_clients_only():
    for host in ("192.168.1.20", "10.0.0.5", "172.16.3.4", "127.0.0.1", "::1", "fe80::1", "::ffff:192.168.0.2"):
        assert is_private_client(host), host
    for host in ("8.8.8.8", "1.1.1.1", "2606:4700::1111", "", None, "testclient", "::ffff:8.8.8.8"):
        assert not is_private_client(host), host


def test_lan_address_override_must_be_private(monkeypatch):
    monkeypatch.setenv("KOVA_LAN_ADDRESS", "192.168.4.2")
    assert pairing.lan_address() == "192.168.4.2"
    monkeypatch.setenv("KOVA_LAN_ADDRESS", "8.8.8.8")
    assert pairing.lan_address() is None


def test_device_names_from_user_agents():
    assert device_name(IPHONE) == "iPhone"
    assert device_name("Mozilla/5.0 (Linux; Android 15; Pixel 9)") == "Android phone"
    assert device_name("") == "Phone"


def test_devices_store_only_hashes_and_survive_reload(tmp_path):
    path = tmp_path / "devices.json"
    devices = Devices(path)
    first, token = devices.add("iPhone")
    second, _ = devices.add("iPhone")
    assert second["name"] == "iPhone 2"
    assert token not in path.read_text()
    assert set(first) == {"id", "name", "created", "last_seen"}
    reloaded = Devices(path)
    assert reloaded.find(token)["id"] == first["id"]
    assert reloaded.find("wrong") is None and reloaded.find(None) is None
    assert reloaded.revoke(first["id"]) == 1
    assert Devices(path).find(token) is None
    assert Devices(path).revoke() == 1 and Devices(path).list() == []


def test_pairing_codes_are_one_use_expire_and_limit_failures(monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(pairing.time, "monotonic", lambda: clock[0])
    codes = Pairing()
    code = codes.start()
    assert codes.status(code.id)[0] == "waiting"
    assert codes.claim(code.secret, "phone") is code
    code.device = {"id": "d1"}
    assert codes.claim(code.secret, "phone") is None, "a claimed code cannot be used twice"
    assert codes.status(code.id)[0] == "paired"
    late = codes.start()
    clock[0] += pairing.PAIR_SECONDS + 1
    assert codes.claim(late.secret, "phone") is None
    assert codes.status(late.id)[0] == "expired"
    for _ in range(pairing.FAILED_LIMIT - 2):
        codes.claim("guess", "phone")
    assert codes.blocked("phone") and not codes.blocked("other")
    clock[0] += pairing.FAILED_WINDOW + 1
    assert not codes.blocked("phone")
    for _ in range(pairing.MAX_OPEN_CODES + 3):
        codes.start()
    assert len(codes.codes) <= pairing.MAX_OPEN_CODES + 1


def test_bind_setting_round_trips(tmp_path):
    cfg = load_config(tmp_path)
    assert cfg.bind == "loopback"
    cfg.bind = "lan"
    save_config(cfg)
    assert load_config(tmp_path).bind == "lan"
    (tmp_path / "config.toml").write_text((tmp_path / "config.toml").read_text().replace('"lan"', '"0.0.0.0"'))
    assert load_config(tmp_path).bind == "loopback", "anything but lan means loopback"


# ------------------------------------------------------------- real sockets
@contextmanager
def kova_server(tmp_path, monkeypatch, bind="loopback"):
    monkeypatch.setenv("KOVA_LAN_ADDRESS", LAN)
    listener = socket.socket(); listener.bind(("127.0.0.1", 0))
    port = listener.getsockname()[1]
    cfg = load_config(tmp_path / "home"); cfg.port = port; cfg.bind = bind; save_config(cfg)
    app = create_app(cfg)
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error",
                                           timeout_graceful_shutdown=2))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
    thread.start()
    deadline = time.monotonic() + 5
    while not server.started and time.monotonic() < deadline:
        time.sleep(.01)
    assert server.started
    client = KovaClient(f"http://127.0.0.1:{port}", ensure_token(cfg.home))
    try:
        yield client, cfg, port, app
    finally:
        client.http.close(); server.should_exit = True; thread.join(10); listener.close()


def _phone(port, **headers):
    origin = f"http://{LAN}:{port}"
    return httpx.Client(base_url=origin, timeout=5,
                        headers={"Origin": origin, "User-Agent": IPHONE, "Sec-Fetch-Site": "same-origin", **headers})


def _pair(client, port):
    code = client.pair_start()
    secret = code["url"].split("#pair=", 1)[1]
    phone = _phone(port)
    claimed = phone.post("/app/pair", json={"secret": secret})
    assert claimed.status_code == 200, claimed.text
    return phone, code, secret, claimed


@needs_lan
def test_phone_access_is_off_by_default_and_nothing_listens_on_the_lan(tmp_path, monkeypatch):
    with kova_server(tmp_path, monkeypatch) as (client, cfg, port, app):
        status = client.phone_status()
        assert status == {"enabled": False, "listening": False, "address": None, "error": None, "devices": []}
        with pytest.raises(Exception) as refused:
            client.pair_start()
        assert getattr(refused.value, "status", None) == 409
        with pytest.raises((httpx.ConnectError, httpx.ConnectTimeout)):  # Windows times out instead of refusing
            httpx.get(f"http://{LAN}:{port}/app", timeout=2)


@needs_lan
def test_pair_a_phone_end_to_end(tmp_path, monkeypatch):
    with kova_server(tmp_path, monkeypatch) as (client, cfg, port, app):
        status = client.set_phone(True)
        assert status["listening"] and status["address"] == f"http://{LAN}:{port}"
        assert load_config(cfg.home).bind == "lan"
        anonymous = _phone(port)
        assert anonymous.get("/app").status_code == 200, "the page itself loads before pairing"
        not_paired = anonymous.get("/chats")
        assert not_paired.status_code == 401 and "kova pair" in not_paired.text
        code = client.pair_start()
        assert code["url"].startswith(f"http://{LAN}:{port}/app#pair=")
        assert code["qr"].startswith("data:image/svg+xml")
        assert client.pair_status(code["id"])["status"] == "waiting"
        secret = code["url"].split("#pair=", 1)[1]
        claimed = anonymous.post("/app/pair", json={"secret": secret})
        assert claimed.status_code == 200
        cookie = claimed.headers["set-cookie"]
        assert DEVICE_COOKIE in cookie and "HttpOnly" in cookie and "SameSite=strict" in cookie
        assert claimed.json()["device"]["name"] == "iPhone"
        paired = client.pair_status(code["id"])
        assert paired["status"] == "paired" and paired["device"]["name"] == "iPhone"
        # The phone now works like the desktop page.
        assert anonymous.get("/chats").status_code == 200
        chat = anonymous.post("/chats", json={})
        assert chat.status_code == 200
        assert client.get_chat(chat.json()["id"]) is not None
        # One use only.
        again = _phone(port).post("/app/pair", json={"secret": secret})
        assert again.status_code == 401
        # Only a hash is on disk; a restart keeps the phone paired.
        token = anonymous.cookies.get(DEVICE_COOKIE)
        assert token and token not in (cfg.home / "devices.json").read_text()
        assert Devices(cfg.home / "devices.json").find(token) is not None
        assert [d["name"] for d in client.devices_list()] == ["iPhone"]


@needs_lan
def test_lan_requests_get_no_local_powers(tmp_path, monkeypatch):
    with kova_server(tmp_path, monkeypatch, bind="lan") as (client, cfg, port, app):
        assert client.phone_status()["listening"], "bind = lan starts the listener with the server"
        phone, *_ = _pair(client, port)
        # Managing access is for the computer itself, even for a paired phone.
        for method, path in (("GET", "/devices"), ("POST", "/phone/pair"), ("GET", "/phone"),
                             ("DELETE", "/devices")):
            assert phone.request(method, path).status_code == 403, path
        # The bearer token never works over the LAN.
        bearer = phone.get("/chats", headers={"Authorization": f"Bearer {ensure_token(cfg.home)}"})
        assert bearer.status_code == 403
        token_only = _phone(port).get("/models", headers={"Authorization": f"Bearer {ensure_token(cfg.home)}"})
        assert token_only.status_code == 401
        # A DNS-rebinding page arrives with a foreign Host; another site with a foreign Origin.
        assert phone.get("/chats", headers={"Host": "evil.example"}).status_code == 403
        assert phone.post("/chats", json={}, headers={"Origin": "http://evil.example"}).status_code == 403
        # Pairing codes are claimed only over the LAN, never by a local page.
        local = httpx.post(f"http://127.0.0.1:{port}/app/pair", json={"secret": "x"},
                           headers={"Origin": f"http://127.0.0.1:{port}"})
        assert local.status_code == 403


@needs_lan
def test_wrong_codes_are_rate_limited(tmp_path, monkeypatch):
    with kova_server(tmp_path, monkeypatch, bind="lan") as (client, cfg, port, app):
        phone = _phone(port)
        codes = [phone.post("/app/pair", json={"secret": f"guess-{n}"}).status_code
                 for n in range(pairing.FAILED_LIMIT + 1)]
        assert codes[:pairing.FAILED_LIMIT] == [401] * pairing.FAILED_LIMIT
        assert codes[-1] == 429


@needs_lan
def test_revoke_logout_and_turning_access_off(tmp_path, monkeypatch):
    with kova_server(tmp_path, monkeypatch, bind="lan") as (client, cfg, port, app):
        phone, *_ = _pair(client, port)
        tablet, *_ = _pair(client, port)
        assert [d["name"] for d in client.devices_list()] == ["iPhone", "iPhone 2"]
        first = client.devices_list()[0]["id"]
        assert client.device_revoke(first) == 1
        assert phone.get("/chats").status_code == 401, "a revoked phone is out at once"
        assert tablet.get("/chats").status_code == 200
        # "Disconnect" on the phone unpairs it for good.
        assert tablet.post("/app/logout").status_code == 200
        assert client.devices_list() == []
        _pair(client, port)
        assert client.device_revoke() == 1
        status = client.set_phone(False)
        assert not status["listening"] and load_config(cfg.home).bind == "loopback"
        with pytest.raises((httpx.ConnectError, httpx.ConnectTimeout)):  # Windows times out instead of refusing
            httpx.get(f"http://{LAN}:{port}/app", timeout=2)


@needs_lan
def test_kova_pair_cli_shows_a_qr_and_waits_for_the_phone(tmp_path, monkeypatch):
    with kova_server(tmp_path, monkeypatch) as (client, cfg, port, app):
        monkeypatch.setattr(cli, "get_client", lambda: client)
        statuses = iter(["waiting", "paired"])
        real_status = client.pair_status

        def fake_status(code_id):
            # Claim from a "phone" while the CLI waits.
            if next(statuses) == "paired":
                secret = app.state.pairing.codes[code_id].secret
                assert _phone(port).post("/app/pair", json={"secret": secret}).status_code == 200
            return real_status(code_id)

        monkeypatch.setattr(client, "pair_status", fake_status)
        monkeypatch.setattr(cli.time, "sleep", lambda _: None)
        result = CliRunner().invoke(cli.app, ["pair"], input="y\n")
        assert result.exit_code == 0, result.output
        assert "Phone access lets a phone" in result.output
        assert "▄" in result.output or "█" in result.output, "a QR code is drawn"
        assert f"http://{LAN}:{port}/app#pair=" in result.output
        assert "Paired iPhone" in result.output
        listed = CliRunner().invoke(cli.app, ["devices"])
        assert "iPhone" in listed.output and "phone access on" in listed.output
        device_id = client.devices_list()[0]["id"]
        assert "Unpaired 1 device" in CliRunner().invoke(cli.app, ["devices", "revoke", device_id]).output
        off = CliRunner().invoke(cli.app, ["pair", "--off"])
        assert off.exit_code == 0 and "off" in off.output
        declined = CliRunner().invoke(cli.app, ["pair"], input="n\n")
        assert "Nothing changed" in declined.output and not client.phone_status()["enabled"]
