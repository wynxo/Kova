"""/bots routes, avatars, direct bot chats and the bot_changed event (1.5).

A bot is a folder of files; these routes are the only writer the app uses, so
this file covers the full lifecycle through HTTP: create, validate, list with a
broken file, edit, delete, the avatar's type and size checks, a direct chat that
really answers as its bot, and the fallback when its bot is gone.
"""

from __future__ import annotations

import json
import stat

import pytest
from fastapi.testclient import TestClient

from kova.config import ModelEntry, ensure_token, load_config
from kova.server import create_app
from tests.fake_model import FakeModel, text


PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 64


@pytest.fixture
def kova(tmp_path):
    cfg = load_config(tmp_path / "home")
    cfg.files_path = tmp_path / "Kova Files"
    cfg.files_path.mkdir()
    cfg.models.append(ModelEntry(name="big", base_url="http://big.test/v1", model="big-model"))
    fake = FakeModel([text("Hello from the bot.")])
    app = create_app(cfg, transport=fake.transport)
    with TestClient(app) as client:
        headers = {"Authorization": f"Bearer {ensure_token(cfg.home)}"}
        yield client, app, cfg, headers, fake


def make(client, headers, **overrides):
    body = {"id": "researcher", "name": "Researcher",
            "persona": "You research questions and cite sources.",
            "model": {"name": "big"}}
    body.update(overrides)
    response = client.post("/bots", json=body, headers=headers)
    assert response.status_code == 200, response.text
    return response.json()


# ---------------------------------------------------------------- lifecycle
def test_a_bot_is_created_read_edited_and_deleted(kova):
    client, app, cfg, headers, fake = kova
    created = make(client, headers)
    assert created["state"] == "ok" and created["handle"] == "researcher"
    assert created["model"] == "big" and created["persona"].startswith("You research")
    folder = cfg.home / "bots" / "researcher"
    assert (folder / "bot.toml").is_file() and (folder / "persona.md").read_text().startswith("You")
    assert stat.S_IMODE((folder / "bot.toml").stat().st_mode) == 0o600

    listed = client.get("/bots", headers=headers).json()
    assert [(bot["id"], bot["state"], bot["status"]) for bot in listed] == [
        ("researcher", "ok", "idle")]

    one = client.get("/bots/researcher", headers=headers).json()
    assert one["definition"]["model"]["name"] == "big"
    assert one["definition"]["permissions"]["ceiling"] == "manual"

    edited = client.patch("/bots/researcher", headers=headers,
                          json={"name": "Sources", "permissions": {"tools": ["web_*"]},
                                "persona": "Find and cite."}).json()
    assert edited["name"] == "Sources"
    assert edited["definition"]["permissions"]["tools"] == ["web_*"]
    assert (folder / "persona.md").read_text() == "Find and cite."
    # A partial patch leaves every other field alone.
    assert edited["handle"] == "researcher" and edited["definition"]["memory"]["budget"] == 4000

    assert client.delete("/bots/researcher", headers=headers).json() == {"ok": True}
    assert not folder.exists()
    assert client.get("/bots", headers=headers).json() == []


def test_validate_only_checks_a_draft_without_writing_it(kova):
    client, app, cfg, headers, fake = kova
    draft = {"id": "researcher", "name": "Researcher", "avatar": {"colour": "chartreuse"}}
    refused = client.post("/bots", json=draft, params={"validate_only": "true"}, headers=headers)
    assert refused.status_code == 422 and "unknown chat colour" in refused.json()["detail"]
    ok = client.post("/bots", json={"id": "coder", "name": "Coder"},
                     params={"validate_only": "true"}, headers=headers)
    assert ok.status_code == 200 and ok.json()["ok"] is True
    assert not (cfg.home / "bots" / "coder").exists()


def test_a_duplicate_bot_or_handle_is_refused(kova):
    client, app, cfg, headers, fake = kova
    make(client, headers)
    again = client.post("/bots", json={"id": "researcher", "name": "Other"}, headers=headers)
    assert again.status_code == 409
    clash = client.post("/bots", json={"id": "coder", "name": "Coder", "handle": "researcher"},
                        headers=headers)
    assert clash.status_code == 422 and "already used" in clash.json()["detail"]


def test_a_broken_file_is_listed_with_its_error_and_cannot_be_opened(kova):
    client, app, cfg, headers, fake = kova
    folder = cfg.home / "bots" / "broken"
    folder.mkdir(parents=True)
    (folder / "bot.toml").write_text("id = [unclosed")
    listed = client.get("/bots", headers=headers).json()
    assert listed[0]["id"] == "broken" and listed[0]["state"] == "error"
    assert "TOML" in listed[0]["error"] and "\n" not in listed[0]["error"]
    broken = client.get("/bots/broken", headers=headers)
    assert broken.status_code == 422 and "TOML" in broken.json()["detail"]
    assert client.get("/bots/missing", headers=headers).status_code == 404


def test_every_change_publishes_bot_changed(kova):
    client, app, cfg, headers, fake = kova
    queue = app.state.bus.subscribe()
    make(client, headers)
    client.patch("/bots/researcher", json={"name": "Sources"}, headers=headers)
    client.delete("/bots/researcher", headers=headers)
    events = [queue.get_nowait() for _ in range(queue.qsize())]
    assert [(event["type"], event["id"], event["action"]) for event in events] == [
        ("bot_changed", "researcher", "created"),
        ("bot_changed", "researcher", "updated"),
        ("bot_changed", "researcher", "deleted"),
    ]


def test_a_bot_shows_working_while_one_of_its_chats_answers(kova):
    client, app, cfg, headers, fake = kova
    make(client, headers)
    chat = client.post("/chats", json={"bot_id": "researcher"}, headers=headers).json()
    app.state.conversation_states.states[chat["id"]] = "working"
    [listed] = client.get("/bots", headers=headers).json()
    assert listed["status"] == "working"


# -------------------------------------------------------------------- chats
def test_a_direct_bot_chat_uses_the_bots_defaults_and_answers_as_the_bot(kova):
    client, app, cfg, headers, fake = kova
    make(client, headers, model={"name": "big", "mode": "chat", "web": False})
    chat = client.post("/chats", json={"bot_id": "researcher"}, headers=headers).json()
    assert (chat["kind"], chat["bot_id"]) == ("bot", "researcher")
    assert chat["mode"] == "chat" and chat["web"] is False
    assert chat["cwd"] == str(cfg.files_path / "bots" / "researcher")
    assert (cfg.files_path / "bots" / "researcher").is_dir()

    listed = client.get("/chats", headers=headers).json()
    assert [(row["kind"], row["bot_id"]) for row in listed] == [("bot", "researcher")]
    one = client.get(f"/chats/{chat['id']}", headers=headers).json()
    assert one["bot"]["name"] == "Researcher" and one["bot"]["model"] == "big"

    fake.scripts[:] = [text("Hello from the bot.")]
    reply = client.post(f"/chats/{chat['id']}/messages", json={"text": "hi"}, headers=headers)
    assert reply.status_code == 200
    assert fake.requests[-1]["model"] == "big-model"
    assert '"author": "researcher"' in reply.text
    rows = app.state.store.get_messages(chat["id"], include_message_meta=True)
    assert rows[0].get("author") is None
    assert rows[-1]["author"] == "researcher" and rows[-1]["author_name"] == "Researcher"


def test_an_explicit_mode_or_folder_wins_over_the_bots_defaults(kova, tmp_path):
    client, app, cfg, headers, fake = kova
    make(client, headers, model={"name": "", "mode": "work", "web": False})
    folder = tmp_path / "elsewhere"
    folder.mkdir()
    chat = client.post("/chats", json={"bot_id": "researcher", "mode": "chat",
                                       "web": True, "cwd": str(folder)}, headers=headers).json()
    assert (chat["mode"], chat["web"]) == ("chat", True)
    assert chat["cwd"] == str(folder)


def test_a_chat_without_a_bot_stays_a_plain_chat(kova):
    client, app, cfg, headers, fake = kova
    chat = client.post("/chats", json={}, headers=headers).json()
    assert (chat["kind"], chat["bot_id"]) == ("chat", None)
    assert "bot" not in client.get(f"/chats/{chat['id']}", headers=headers).json()


def test_a_chat_whose_bot_is_gone_answers_with_the_default_assistant(kova):
    client, app, cfg, headers, fake = kova
    make(client, headers)
    chat = client.post("/chats", json={"bot_id": "researcher"}, headers=headers).json()
    client.delete("/bots/researcher", headers=headers)
    assert client.get(f"/chats/{chat['id']}", headers=headers).status_code == 200
    fake.scripts[:] = [text("Plain hello.")]
    reply = client.post(f"/chats/{chat['id']}/messages", json={"text": "hi"}, headers=headers)
    assert reply.status_code == 200
    assert fake.requests[-1]["model"] == "test-model"
    assert '"author"' not in reply.text


def test_a_bots_chat_cannot_be_incognito_or_change_its_bot(kova):
    client, app, cfg, headers, fake = kova
    make(client, headers)
    refused = client.post("/chats", json={"bot_id": "researcher", "ephemeral": True},
                          headers=headers)
    assert refused.status_code == 422
    assert client.post("/chats", json={"bot_id": "missing"}, headers=headers).status_code == 404
    chat = client.post("/chats", json={"bot_id": "researcher"}, headers=headers).json()
    fixed = client.patch(f"/chats/{chat['id']}", json={"bot_id": "coder"}, headers=headers)
    assert fixed.status_code == 409


def test_a_bot_chat_in_a_project_keeps_the_bots_own_folder(kova, tmp_path):
    client, app, cfg, headers, fake = kova
    make(client, headers)
    folder = tmp_path / "project"
    folder.mkdir()
    project = client.post("/projects", json={"name": "Alpha", "cwd": str(folder)},
                          headers=headers).json()
    chat = client.post("/chats", json={"bot_id": "researcher", "project_id": project["id"]},
                       headers=headers).json()
    assert chat["project_id"] == project["id"]
    assert chat["cwd"] == str(cfg.files_path / "bots" / "researcher")


# ------------------------------------------------------------------ avatars
def test_an_avatar_is_uploaded_read_and_cleared(kova):
    client, app, cfg, headers, fake = kova
    make(client, headers)
    missing = client.get("/bots/researcher/avatar", headers=headers)
    assert missing.status_code == 404
    uploaded = client.put("/bots/researcher/avatar", headers=headers,
                          files={"file": ("me.png", PNG, "image/png")})
    assert uploaded.status_code == 200, uploaded.text
    assert uploaded.json()["image"] == "avatar.png"
    image = (cfg.home / "bots" / "researcher" / "avatar.png")
    assert image.read_bytes() == PNG and stat.S_IMODE(image.stat().st_mode) == 0o600
    served = client.get("/bots/researcher/avatar", headers=headers)
    assert served.status_code == 200 and served.content == PNG
    assert served.headers["content-type"] == "image/png"
    one = client.get("/bots/researcher", headers=headers).json()
    assert one["image"] == "avatar.png"

    assert client.delete("/bots/researcher/avatar", headers=headers).status_code == 200
    assert not image.exists()
    assert client.get("/bots/researcher/avatar", headers=headers).status_code == 404


def test_an_avatar_is_type_checked_and_size_capped(kova):
    client, app, cfg, headers, fake = kova
    make(client, headers)
    not_an_image = client.put("/bots/researcher/avatar", headers=headers,
                              files={"file": ("me.png", b"<svg>not a png</svg>", "image/png")})
    assert not_an_image.status_code == 422 and "not the image type" in not_an_image.text
    big = b"\x89PNG\r\n\x1a\n" + b"0" * (256 * 1024)
    too_big = client.put("/bots/researcher/avatar", headers=headers,
                         files={"file": ("me.png", big, "image/png")})
    assert too_big.status_code == 413
    wrong_type = client.put("/bots/researcher/avatar", headers=headers,
                            files={"file": ("me.gif", PNG, "image/gif")})
    assert wrong_type.status_code == 422


def test_an_avatar_lives_inside_the_bot_folder_only(kova):
    client, app, cfg, headers, fake = kova
    make(client, headers)
    # A definition that names a path is refused outright, never joined.
    refused = client.patch("/bots/researcher", json={"avatar": {"image": "../escape.png"}},
                           headers=headers)
    assert refused.status_code == 422 and "plain file name" in refused.json()["detail"]


def test_deleting_a_bot_removes_its_private_notes_and_leaves_its_chats(kova):
    client, app, cfg, headers, fake = kova
    make(client, headers)
    chat = client.post("/chats", json={"bot_id": "researcher"}, headers=headers).json()
    client.post("/memory", json={"title": "Sources", "body": "Paper one.",
                                 "bot_id": "researcher"}, headers=headers)
    scope = cfg.memory_path / ".kova-bots" / "researcher"
    assert scope.is_dir()
    client.delete("/bots/researcher", headers=headers)
    assert not scope.exists()
    kept = client.get(f"/chats/{chat['id']}", headers=headers).json()
    assert kept["bot_id"] == "researcher", "the chat and its stored rows stay"


def test_the_definition_never_contains_what_looks_like_a_secret(kova):
    client, app, cfg, headers, fake = kova
    key = "sk-proj-abcdefghijklmnopqrstuvwxyz0123456789"
    refused = client.post("/bots", json={"id": "leaky", "name": "Leaky",
                                         "persona": f"Use {key}."}, headers=headers)
    assert refused.status_code == 422 and "looks like a secret" in refused.json()["detail"]
    assert not (cfg.home / "bots" / "leaky").exists()
    assert key not in json.dumps(client.get("/bots", headers=headers).json())


def test_a_broken_bot_can_still_be_removed(kova):
    client, app, cfg, headers, fake = kova
    folder = cfg.home / "bots" / "broken"
    folder.mkdir(parents=True)
    (folder / "bot.toml").write_text("id = [unclosed")
    assert client.delete("/bots/broken", headers=headers).status_code == 200
    assert not folder.exists()
