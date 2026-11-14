"""PWA HTTP contract, one-use shares and native attachment handoff."""
import json
import time

from fastapi.testclient import TestClient
from kova.config import ensure_token, load_config
from kova.server import create_app
from tests.fake_model import FakeModel, text


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


def test_share_uses_the_server_clock_when_attachment_storage_offers_it(tmp_path, monkeypatch):
    """After the quota work lands, shares age on the same clock as uploads."""
    from kova import attachments
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
