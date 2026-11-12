"""Browser sessions, offline app assets, and actual server-side cancellation."""

import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest
from fastapi.testclient import TestClient

from kova.config import ensure_token, load_config
from kova.server import create_app
from tests.fake_model import FakeModel, text


@pytest.fixture
def desktop(tmp_path, monkeypatch):
    monkeypatch.setenv("KOVA_HOME", str(tmp_path))
    cfg = load_config()
    app = create_app(cfg, transport=FakeModel([text("Hello!")]).transport)
    headers = {"Authorization": f"Bearer {ensure_token(cfg.home)}"}
    with TestClient(app, base_url="http://127.0.0.1:7700") as client:
        yield client, headers, app


def login(client, headers):
    response = client.post("/app/session", headers=headers)
    assert response.status_code == 200
    ticket = response.json()["ticket"]
    result = client.post("/app/login", json={"ticket": ticket},
                         headers={"Origin": "http://127.0.0.1:7700"})
    assert result.status_code == 200
    return ticket, result


def test_app_and_offline_assets_need_no_model_or_token(desktop):
    client, headers, app = desktop
    page = client.get("/app")
    assert page.status_code == 200
    assert 'id="composer"' in page.text
    assert app.state.token not in page.text
    assert "frame-ancestors 'none'" in page.headers["content-security-policy"]
    assert page.headers["referrer-policy"] == "no-referrer"
    for name in ("app.js", "app.css", "vendor/marked.js", "vendor/purify.js", "vendor/highlight.js"):
        response = client.get(f"/app/assets/{name}")
        assert response.status_code == 200
        assert response.content
    assert client.get("/app/assets/../../server.py").status_code == 404
    assert client.get("/chats").status_code == 401


def test_ticket_is_bearer_only_single_use_and_sets_httponly_cookie(desktop):
    client, headers, app = desktop
    assert client.post("/app/session").status_code == 401
    ticket, response = login(client, headers)
    cookie = response.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=strict" in cookie and "path=/" in cookie
    assert app.state.token not in cookie
    assert client.post("/app/login", json={"ticket": ticket}).status_code == 401
    assert client.post("/app/session").status_code == 401  # cookie cannot mint tickets
    chat = client.post("/chats", json={"title": "Desktop"})
    assert chat.status_code == 200
    assert client.get("/chats").json()[0]["title"] == "Desktop"
    assert client.post(f"/chats/{chat.json()['id']}/messages", json={"text": "hi"}).status_code == 200


def test_expired_ticket_and_cookie_are_rejected(desktop, monkeypatch):
    client, headers, app = desktop
    import kova.web as web
    clock = [100.0]
    monkeypatch.setattr(web.time, "monotonic", lambda: clock[0])
    ticket = client.post("/app/session", headers=headers).json()["ticket"]
    clock[0] += 61
    assert client.post("/app/login", json={"ticket": ticket}).status_code == 401
    login(client, headers)
    clock[0] += 12 * 3600 + 1
    assert client.get("/chats").status_code == 401


@pytest.mark.parametrize("bad_headers", [
    {"Origin": "https://evil.example"},
    {"Origin": "http://127.0.0.1:9999"},
    {"Origin": "null"},
    {"Sec-Fetch-Site": "cross-site"},
    {"Host": "evil.example"},
])
def test_browser_login_and_cookie_api_reject_cross_origin(desktop, bad_headers):
    client, headers, app = desktop
    ticket = client.post("/app/session", headers=headers).json()["ticket"]
    assert client.post("/app/login", json={"ticket": ticket}, headers=bad_headers).status_code == 403
    login(client, headers)
    assert client.get("/chats", headers=bad_headers).status_code == 403
    assert client.post("/chats", headers=bad_headers).status_code == 403
    # The existing CLI bearer protocol remains independent of browser sessions.
    assert client.get("/chats", headers=headers).status_code == 200


def test_logout_revokes_browser_session(desktop):
    client, headers, app = desktop
    login(client, headers)
    cookie = client.cookies.get("kova_session")
    assert client.post("/app/logout").status_code == 200
    client.cookies.set("kova_session", cookie)
    assert client.get("/chats").status_code == 401


def test_stop_cancels_server_task_and_releases_chat(desktop, monkeypatch):
    client, headers, app = desktop
    chat = client.post("/chats", headers=headers).json()["id"]
    started, cancelled = threading.Event(), threading.Event()
    original = app.state.agent.run_turn

    async def slow_turn(*args, **kwargs):
        try:
            started.set()
            yield {"type": "text", "text": "Starting"}
            await asyncio.sleep(10)  # bounded even if a cancellation regression fails the test
        finally:
            cancelled.set()

    monkeypatch.setattr(app.state.agent, "run_turn", slow_turn)
    with ThreadPoolExecutor() as pool:
        stream = pool.submit(client.post, f"/chats/{chat}/messages",
                             headers=headers, json={"text": "wait"})
        assert started.wait(3)
        assert client.post(f"/chats/{chat}/stop").status_code == 401
        assert client.post(f"/chats/{chat}/stop", headers=headers).json() == {"stopped": True}
        assert cancelled.wait(3)
        assert '"type": "stopped"' in stream.result(timeout=3).text
    assert client.post(f"/chats/{chat}/stop", headers=headers).json() == {"stopped": False}
    assert client.post("/chats/missing/stop", headers=headers).status_code == 404
    monkeypatch.setattr(app.state.agent, "run_turn", original)
    assert client.post(f"/chats/{chat}/messages", headers=headers,
                       json={"text": "hello"}).status_code == 200


def test_offline_model_does_not_block_app_or_browser_login(tmp_path, monkeypatch):
    monkeypatch.setenv("KOVA_HOME", str(tmp_path))
    cfg = load_config()
    def offline(request):
        raise httpx.ConnectError("Model offline", request=request)
    app = create_app(cfg, transport=httpx.MockTransport(offline))
    with TestClient(app, base_url="http://127.0.0.1:7700") as client:
        headers = {"Authorization": f"Bearer {ensure_token(cfg.home)}"}
        assert client.get("/app").status_code == 200
        assert client.get("/health").json()["model_ok"] is False
        login(client, headers)
        chat = client.post("/chats").json()["id"]
        response = client.post(f"/chats/{chat}/messages", json={"text": "hello"})
        assert response.status_code == 200
        assert 'event: error' in response.text
        assert client.get("/chats").status_code == 200
