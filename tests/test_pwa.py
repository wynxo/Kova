"""PWA HTTP contract, one-use shares and native attachment handoff."""
import json
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi.testclient import TestClient
from kova import attachments
from kova.config import ensure_token, load_config
from kova.server import create_app
from tests.fake_model import FakeModel, text
from tests.test_server import server  # noqa: F401  (pytest fixture)


def login(app, client, name="demo"):
    app.state.web_sessions[name] = time.monotonic() + 60
    client.cookies.set("kova_session", name)


def test_manifest_and_text_share(tmp_path):
    config = load_config(tmp_path)
    config.persona_name = "Nova"
    app = create_app(config)
    with TestClient(app, base_url="http://localhost") as client:
        manifest = client.get("/app/manifest.webmanifest")
        assert manifest.headers["content-type"] == "application/manifest+json"
        assert manifest.headers["cache-control"] == "no-store"
        assert manifest.json()["name"] == "Nova"
        assert manifest.json()["share_target"]["method"] == "POST"
        assert client.get("/app/sw.js").headers["cache-control"] == "no-store"
        assert client.post("/app/share", data={"text": "hello"}).status_code == 401
        login(app, client)
        response = client.post("/app/share", data={"title": "Title", "text": "hello",
                                                   "url": "https://example.com"},
                               follow_redirects=False)
        assert response.status_code == 303
        token = response.headers["location"].split("#share=")[1]
        draft = client.post("/app/share/" + token).json()
        assert draft["text"] == "Title\n\nhello\n\nhttps://example.com"
        assert draft["chat"] is None and draft["attachments"] == []
        assert client.post("/app/share/" + token).status_code == 404


def test_file_share_becomes_native_attachment(tmp_path):
    config = load_config(tmp_path)
    app = create_app(config)
    with TestClient(app, base_url="http://localhost") as client:
        login(app, client)
        response = client.post("/app/share", data={"title": "Title", "text": "hello",
                                                   "url": "https://example.com"},
                               files={"files": ("note.txt", b"content", "text/plain")},
                               follow_redirects=False)
        assert response.status_code == 303
        token = response.headers["location"].split("#share=")[1]

        # Another browser session cannot redeem the same one-use share.
        login(app, client, "other")
        assert client.post("/app/share/" + token).status_code == 404
        login(app, client)
        draft = client.post("/app/share/" + token).json()
        assert draft["text"] == "Title\n\nhello\n\nhttps://example.com"
        assert "note.txt" not in draft["text"] and "Base64" not in draft["text"]
        assert "Y29udGVudA==" not in json.dumps(draft)
        assert draft["chat"]["id"]
        [row] = draft["attachments"]
        assert row["chat_id"] == draft["chat"]["id"]
        assert (row["name"], row["mime"], row["size"]) == ("note.txt", "text/plain", 7)
        assert "path" not in row
        assert (tmp_path / "attachments" / row["chat_id"]).is_dir()
        assert client.post("/app/share/" + token).status_code == 404


def test_expired_file_share_removes_its_draft_chat(tmp_path):
    config = load_config(tmp_path)
    app = create_app(config)
    with TestClient(app, base_url="http://localhost") as client:
        login(app, client)
        response = client.post("/app/share", data={"text": "hello"},
                               files={"files": ("note.txt", b"content", "text/plain")},
                               follow_redirects=False)
        token = response.headers["location"].split("#share=")[1]
        chat_id = app.state.web_shares[token]["chat"]["id"]
        app.state.web_shares[token]["expires"] = time.monotonic() - 1
        assert client.post("/app/share/" + token).status_code == 404
        assert client.get("/chats").json() == []
        assert app.state.store.get_chat(chat_id) is None


def test_expired_file_share_is_pruned_by_browser_activity(tmp_path):
    """A draft is not kept until the next share; ordinary use expires it."""
    config = load_config(tmp_path)
    app = create_app(config)
    with TestClient(app, base_url="http://localhost") as client:
        login(app, client)
        response = client.post("/app/share", data={"text": "hello"},
                               files={"files": ("note.txt", b"content", "text/plain")},
                               follow_redirects=False)
        token = response.headers["location"].split("#share=")[1]
        chat_id = app.state.web_shares[token]["chat"]["id"]
        app.state.web_shares[token]["expires"] = time.monotonic() - 1

        assert client.get("/chats").status_code == 200

        assert token not in app.state.web_shares
        assert app.state.store.get_chat(chat_id) is None
        assert not list((tmp_path / "attachments" / chat_id).glob("*"))


def test_restart_before_redeeming_a_share_leaves_no_orphan_chat(tmp_path):
    """Share tokens do not survive a restart, so neither should their drafts."""
    config = load_config(tmp_path)
    app = create_app(config)
    with TestClient(app, base_url="http://localhost") as client:
        login(app, client)
        response = client.post("/app/share", data={"text": "hello"},
                               files={"files": ("note.txt", b"content", "text/plain")},
                               follow_redirects=False)
        assert response.status_code == 303
        token = response.headers["location"].split("#share=")[1]
        chat_id = app.state.web_shares[token]["chat"]["id"]
        assert app.state.store.get_chat(chat_id) is not None

    restarted = create_app(config)
    with TestClient(restarted, base_url="http://localhost"):
        assert restarted.state.store.get_chat(chat_id) is None
        assert restarted.state.store.attachments(chat_id) == []
        assert not (tmp_path / "attachments" / chat_id).exists()


def test_redeemed_share_chat_survives_a_restart(tmp_path):
    config = load_config(tmp_path)
    app = create_app(config)
    with TestClient(app, base_url="http://localhost") as client:
        login(app, client)
        response = client.post("/app/share", data={"text": "hello"},
                               files={"files": ("note.txt", b"content", "text/plain")},
                               follow_redirects=False)
        token = response.headers["location"].split("#share=")[1]
        chat_id = client.post("/app/share/" + token).json()["chat"]["id"]

    restarted = create_app(config)
    with TestClient(restarted, base_url="http://localhost"):
        assert restarted.state.store.get_chat(chat_id) is not None


def test_startup_keeps_a_journaled_draft_chat_that_has_messages(tmp_path):
    """A crash after handoff must never make a restart delete a real chat."""
    config = load_config(tmp_path)
    app = create_app(config)
    with TestClient(app, base_url="http://localhost") as client:
        store = app.state.store
        chat_id = store.create_chat("Mine")["id"]
        store.add_message(chat_id, "user", "keep me")
        store.add_share_draft(chat_id)

    restarted = create_app(config)
    with TestClient(restarted, base_url="http://localhost"):
        assert restarted.state.store.get_chat(chat_id) is not None
        assert restarted.state.store.reap_share_drafts() == []


def test_share_uses_the_server_clock(tmp_path, monkeypatch):
    """Shares age on the same server clock as every other upload."""
    config = load_config(tmp_path)
    frozen = lambda: 0
    seen = []

    def spy(home, store, chat_id, name, data, *, now=None):
        seen.append(now)
        return {"id": "uploaded", "chat_id": chat_id, "name": name,
                "mime": "text/plain", "size": len(data), "pages": None}

    monkeypatch.setattr(attachments, "save", spy)
    app = create_app(config, now=frozen)
    with TestClient(app, base_url="http://localhost") as client:
        login(app, client)
        assert client.post("/app/share", data={"text": "hello"},
                           files={"files": ("note.txt", b"content", "text/plain")},
                           follow_redirects=False).status_code == 303
    assert seen == [frozen]


def test_share_rejections(tmp_path):
    config = load_config(tmp_path)
    app = create_app(config)
    with TestClient(app, base_url="http://localhost") as client:
        login(app, client)
        too_many = [("files", (f"note{i}.txt", b"x", "text/plain")) for i in range(4)]
        assert client.post("/app/share", data={"text": "x"}, files=too_many).status_code == 400
        rejected = client.post("/app/share", data={"text": "x"},
                               files={"files": ("bad.exe", b"x", "application/octet-stream")})
        assert rejected.status_code == 400
        assert "Unsupported" in rejected.json()["detail"]
        assert client.get("/chats").json() == []  # a failed share leaves no draft chat
        assert not list((tmp_path / "attachments").rglob("*"))  # nor its files
        assert client.post("/app/share", content=b"x" * (2 * 1024 * 1024 + 1)).status_code == 413
        assert client.post("/app/share", data={"text": "bad"},
                           headers={"Origin": "https://example.com"}).status_code == 403


def test_shared_image_still_needs_vision(tmp_path):
    """A native shared image follows the same per-model vision gate as a turn."""
    config = load_config(tmp_path)
    fake = FakeModel([text("A picture.")])
    app = create_app(config, transport=fake.transport)
    headers = {"Authorization": f"Bearer {ensure_token(config.home)}"}
    with TestClient(app, base_url="http://localhost") as client:
        login(app, client)
        response = client.post("/app/share", data={"text": "describe"},
                               files={"files": ("shot.png", b"\x89PNG\r\n\x1a\n", "image/png")},
                               follow_redirects=False)
        draft = client.post("/app/share/" + response.headers["location"].split("#share=")[1]).json()
        [row] = draft["attachments"]
        assert row["mime"] == "image/png"
        refused = client.post(f"/chats/{row['chat_id']}/messages", headers=headers,
                              json={"text": "describe", "attachments": [row["id"]]})
        assert refused.status_code == 400
        assert "cannot read images" in refused.json()["detail"]
        config.models[0].vision = True
        accepted = client.post(f"/chats/{row['chat_id']}/messages", headers=headers,
                               json={"text": "describe", "attachments": [row["id"]]})
        assert accepted.status_code == 200 and "event: done" in accepted.text


def test_file_share_records_normal_attachment_usage(tmp_path):
    """The no-worker share path creates ordinary attachments, not a side store."""
    config = load_config(tmp_path)
    frozen = datetime(2026, 1, 1, tzinfo=timezone.utc)
    app = create_app(config, now=lambda: frozen)
    with TestClient(app, base_url="http://localhost") as client:
        login(app, client)
        response = client.post("/app/share", data={"text": "hello"},
                               files={"files": ("note.txt", b"content", "text/plain")},
                               follow_redirects=False)
        assert response.status_code == 303
        token = response.headers["location"].split("#share=")[1]
        chat_id = app.state.web_shares[token]["chat"]["id"]
        [row] = app.state.store.attachments(chat_id)
        assert row["created"].startswith("2026-01-01T00:00:00")
        assert row["used"] == 0
        assert row["chat_id"] == chat_id


def test_share_quota_applies_before_a_draft_is_handed_out(tmp_path, monkeypatch):
    config = load_config(tmp_path)
    app = create_app(config)
    monkeypatch.setattr(attachments, "MAX_PENDING_UPLOADS", 1)
    with TestClient(app, base_url="http://localhost") as client:
        login(app, client)
        response = client.post(
            "/app/share", data={"text": "x"},
            files=[("files", ("one.txt", b"one", "text/plain")),
                   ("files", ("two.txt", b"two", "text/plain"))])
        assert response.status_code == 409
        assert "unsent attachments" in response.json()["detail"]
        assert app.state.store.list_chats() == []
        assert not list((tmp_path / "attachments").rglob("*"))


def test_shared_file_is_retained_and_forked(server):
    """A sent shared upload stays usable, survives cleanup and copies on fork."""
    client, headers, cfg, fake = server
    app = client.app
    client.base_url = "http://localhost"
    login(app, client)
    response = client.post("/app/share", data={"text": "look"},
                           files={"files": ("note.txt", b"content", "text/plain")},
                           follow_redirects=False)
    assert response.status_code == 303
    token = response.headers["location"].split("#share=")[1]
    draft = client.post("/app/share/" + token).json()
    chat_id = draft["chat"]["id"]
    [row] = draft["attachments"]

    sent = client.post(f"/chats/{chat_id}/messages", headers=headers,
                       json={"text": "look", "attachments": [row["id"]]})
    assert sent.status_code == 200 and "event: done" in sent.text
    [stored] = app.state.store.attachments(chat_id)
    assert stored["used"] == 1 and stored["created"]
    messages = client.get(f"/chats/{chat_id}", headers=headers).json()["messages"]
    assert messages[0]["attachments"] == [{"id": row["id"], "name": "note.txt"}]

    old = datetime(2026, 1, 1, tzinfo=timezone.utc)
    with app.state.store._lock:
        app.state.store._conn.execute(
            "UPDATE attachments SET created = ? WHERE id = ?", (old.isoformat(), row["id"]))
        app.state.store._conn.commit()
    later = old + attachments.PENDING_TTL + timedelta(days=1)
    assert attachments.reap(app.state.store, now=lambda: later) == 0
    assert Path(stored["path"]).exists()

    fork = client.post(f"/chats/{chat_id}/fork", headers=headers).json()
    [copied] = app.state.store.attachments(fork["id"])
    assert copied["used"] == 1 and copied["id"] != row["id"]
    assert fork["messages"][0]["attachments"] == [{"id": copied["id"], "name": "note.txt"}]
