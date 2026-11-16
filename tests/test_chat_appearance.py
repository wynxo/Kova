"""Per-chat colour and icon, first slice (#213).

A chat may carry an optional colour (one key of a small fixed palette of
theme tokens, never free-form hex) and an optional icon (one vendored icon
name). Both are stored on the chat with a migration for older databases,
shown in list/get responses, set through PATCH with validation, and cosmetic
changes never mark a chat as written in by a phone.
"""

import sqlite3
import time

import pytest
from fastapi.testclient import TestClient

from kova.chat_appearance import (CHAT_COLOURS, CHAT_ICONS, normalize_colour,
                                  normalize_icon)
from kova.config import ensure_token, load_config
from kova.pairing import DEVICE_COOKIE
from kova.server import create_app
from kova.store import Store
from tests.fake_model import FakeModel, text

LAN_HEADER = "x-test-lan"


# ------------------------------------------------------------- validation
def test_palette_has_eight_theme_token_colours():
    assert len(CHAT_COLOURS) == 8
    assert set(CHAT_COLOURS) == {"accent", "success", "warning", "danger",
                                 "info", "keyword", "string", "function"}
    for variable in CHAT_COLOURS.values():
        assert variable.startswith("var(--") and variable.endswith(")")


def test_icons_come_from_the_vendored_set():
    assert len(CHAT_ICONS) >= 8
    assert "folder" in CHAT_ICONS and "message-square" in CHAT_ICONS


def test_normalize_accepts_trims_and_clears():
    assert normalize_colour("Accent") == "accent"
    assert normalize_colour("") is None
    assert normalize_colour("  ") is None
    assert normalize_colour(None) is None
    assert normalize_icon(" Folder ") == "folder"
    assert normalize_icon("") is None
    assert normalize_icon(None) is None


def test_normalize_rejects_hex_and_unknown_names():
    for bad in ("#ff0000", "red", "accent-soft", "not-a-colour"):
        with pytest.raises(ValueError, match="unknown chat colour"):
            normalize_colour(bad)
    for bad in ("star", "i-folder", "folder;rm"):
        with pytest.raises(ValueError, match="unknown chat icon"):
            normalize_icon(bad)


# -------------------------------------------------------------- migration
def test_legacy_database_without_appearance_columns_still_opens(tmp_path):
    path = tmp_path / "kova.db"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE chats (id TEXT PRIMARY KEY, title TEXT, created TEXT NOT NULL);")
    conn.execute("INSERT INTO chats (id, title, created) VALUES ('abc', 'Old', '2026-01-01T00:00:00+00:00')")
    conn.commit()
    conn.close()
    store = Store(path, default_cwd=str(tmp_path))
    chats = store.list_chats()
    assert [(row["id"], row["colour"], row["icon"]) for row in chats] == [("abc", None, None)]
    assert store.get_chat("abc")["colour"] is None
    store.update_chat("abc", colour="accent", icon="folder")
    assert (store.get_chat("abc")["colour"], store.get_chat("abc")["icon"]) == ("accent", "folder")


def test_legacy_database_gains_pin_and_appearance_together(tmp_path):
    """One migration adds the pin and the appearance columns to an old file."""
    path = tmp_path / "kova.db"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE chats (id TEXT PRIMARY KEY, title TEXT, created TEXT NOT NULL);")
    conn.execute("INSERT INTO chats (id, title, created) VALUES ('abc', 'Old', '2026-01-01T00:00:00+00:00')")
    conn.commit()
    conn.close()

    store = Store(path, default_cwd=str(tmp_path))
    row = store.list_chats()[0]
    assert (row["pinned"], row["colour"], row["icon"]) == (False, None, None)
    assert (store.get_chat("abc")["pinned"], store.get_chat("abc")["colour"]) == (False, None)

    store.update_chat("abc", pinned=True, colour="danger", icon="bell")
    reopened = Store(path, default_cwd=str(tmp_path))
    chat = reopened.get_chat("abc")
    assert (chat["pinned"], chat["colour"], chat["icon"]) == (True, "danger", "bell")
    assert [c["id"] for c in reopened.list_chats()] == ["abc"]


def test_pinned_chat_sorts_first_and_keeps_its_appearance(tmp_path):
    store = Store(tmp_path / "kova.db", default_cwd=str(tmp_path))
    first = store.create_chat("Styled")
    time.sleep(0.001)
    second = store.create_chat("Newer")
    store.update_chat(first["id"], pinned=True, colour="info", icon="globe")

    listed = store.list_chats()
    assert [row["id"] for row in listed] == [first["id"], second["id"]]
    assert (listed[0]["pinned"], listed[0]["colour"], listed[0]["icon"]) == (True, "info", "globe")

    store.update_chat(first["id"], pinned=False)
    assert [row["id"] for row in store.list_chats()] == [second["id"], first["id"]]
    assert (store.get_chat(first["id"])["colour"], store.get_chat(first["id"])["icon"]) == ("info", "globe")


def test_store_update_validates_and_clears(tmp_path):
    store = Store(tmp_path / "kova.db", default_cwd=str(tmp_path))
    chat = store.create_chat("Styled")
    assert (chat["colour"], chat["icon"]) == (None, None)
    with pytest.raises(ValueError, match="unknown chat colour"):
        store.update_chat(chat["id"], colour="#ff0000")
    with pytest.raises(ValueError, match="unknown chat icon"):
        store.update_chat(chat["id"], icon="star")
    store.update_chat(chat["id"], colour="info", icon="globe")
    assert (store.get_chat(chat["id"])["colour"], store.get_chat(chat["id"])["icon"]) == ("info", "globe")
    store.update_chat(chat["id"], colour="", icon="  ")
    assert (store.get_chat(chat["id"])["colour"], store.get_chat(chat["id"])["icon"]) == (None, None)
    forked = store.fork_chat(chat["id"])
    assert (forked["colour"], forked["icon"]) == (None, None)
    store.update_chat(chat["id"], colour="danger", icon="bell")
    assert (store.fork_chat(chat["id"])["colour"], store.fork_chat(chat["id"])["icon"]) == ("danger", "bell")
    assert store.import_chat({"id": "imp", "title": "Imp", "colour": "bogus", "icon": "bogus",
                              "messages": []})
    assert (store.get_chat("imp")["colour"], store.get_chat("imp")["icon"]) == (None, None)
    assert store.import_chat({"id": "imp2", "title": "Imp2", "colour": "warning", "icon": "sun",
                              "messages": []})
    assert (store.get_chat("imp2")["colour"], store.get_chat("imp2")["icon"]) == ("warning", "sun")


# -------------------------------------------------------------------- API
@pytest.fixture
def server(tmp_path, monkeypatch):
    monkeypatch.setenv("KOVA_HOME", str(tmp_path))
    cfg = load_config()
    fake = FakeModel([text("Hello there!")])
    app = create_app(cfg, transport=fake.transport)
    headers = {"Authorization": f"Bearer {ensure_token(cfg.home)}"}
    with TestClient(app) as client:
        yield client, headers, cfg, app


def test_patch_sets_clears_and_validates_appearance(server):
    client, headers, cfg, _ = server
    chat_id = client.post("/chats", headers=headers).json()["id"]
    assert client.get(f"/chats/{chat_id}", headers=headers).json()["colour"] is None

    patched = client.patch(f"/chats/{chat_id}", json={"colour": "success", "icon": "compass"},
                           headers=headers)
    assert patched.status_code == 200
    assert (patched.json()["colour"], patched.json()["icon"]) == ("success", "compass")
    fetched = client.get(f"/chats/{chat_id}", headers=headers).json()
    assert (fetched["colour"], fetched["icon"]) == ("success", "compass")
    listed = client.get("/chats", headers=headers).json()
    assert (listed[0]["colour"], listed[0]["icon"]) == ("success", "compass")

    cleared = client.patch(f"/chats/{chat_id}", json={"colour": "", "icon": ""}, headers=headers)
    assert (cleared.json()["colour"], cleared.json()["icon"]) == (None, None)

    assert client.patch(f"/chats/{chat_id}", json={"colour": "#ff0000"},
                        headers=headers).status_code == 422
    assert client.patch(f"/chats/{chat_id}", json={"colour": "red"},
                        headers=headers).status_code == 422
    assert client.patch(f"/chats/{chat_id}", json={"icon": "star"},
                        headers=headers).status_code == 422
    assert client.patch("/chats/abc", json={"colour": "accent"},
                        headers=headers).status_code == 404


def test_patch_combines_pin_colour_and_icon(server):
    """One PATCH accepts any combination, and each field leaves the others alone."""
    client, headers, _, _ = server
    chat_id = client.post("/chats", headers=headers).json()["id"]

    patched = client.patch(f"/chats/{chat_id}", json={"pinned": True, "colour": "info", "icon": "globe"},
                           headers=headers)
    assert patched.status_code == 200
    assert (patched.json()["pinned"], patched.json()["colour"], patched.json()["icon"]) == (True, "info", "globe")

    unpinned = client.patch(f"/chats/{chat_id}", json={"pinned": False}, headers=headers).json()
    assert (unpinned["pinned"], unpinned["colour"], unpinned["icon"]) == (False, "info", "globe")

    recoloured = client.patch(f"/chats/{chat_id}", json={"colour": "accent"}, headers=headers).json()
    assert (recoloured["pinned"], recoloured["colour"], recoloured["icon"]) == (False, "accent", "globe")

    relabelled = client.patch(f"/chats/{chat_id}", json={"icon": "folder"}, headers=headers).json()
    assert (relabelled["pinned"], relabelled["colour"], relabelled["icon"]) == (False, "accent", "folder")

    client.patch(f"/chats/{chat_id}", json={"pinned": True}, headers=headers)
    listed = client.get("/chats", headers=headers).json()[0]
    assert (listed["pinned"], listed["colour"], listed["icon"]) == (True, "accent", "folder")
    fetched = client.get(f"/chats/{chat_id}", headers=headers).json()
    assert (fetched["pinned"], fetched["colour"], fetched["icon"]) == (True, "accent", "folder")


def test_create_accepts_appearance(server):
    client, headers, _, _ = server
    created = client.post("/chats", json={"colour": "info", "icon": "globe"}, headers=headers).json()
    assert (created["colour"], created["icon"]) == ("info", "globe")
    assert client.post("/chats", json={"colour": "#fff"},
                       headers=headers).status_code == 422


# ------------------------------------------- cosmetic changes limit nothing
@pytest.fixture
def kova(tmp_path):
    cfg = load_config(tmp_path / "home")
    cfg.files_path = tmp_path / "Kova Files"
    cfg.files_path.mkdir()
    app = create_app(cfg, transport=FakeModel([text("Hello.")]).transport)

    async def maybe_lan(scope, receive, send):
        if scope["type"] == "http" and (LAN_HEADER.encode(), b"1") in scope.get("headers", []):
            scope = {**scope, "kova_lan": True}
        await app(scope, receive, send)

    with TestClient(maybe_lan) as client:
        local = {"Authorization": f"Bearer {ensure_token(cfg.home)}"}
        _, token = app.state.devices.add("iPhone")
        phone = {LAN_HEADER: "1", "Cookie": f"{DEVICE_COOKIE}={token}"}
        yield client, app, local, phone


def test_a_phone_setting_only_colour_or_icon_limits_nothing(kova):
    client, app, local, phone = kova
    chat = client.post("/chats", json={"mode": "work"}, headers=local).json()
    assert app.state.store.chat_capabilities(chat["id"]) is None

    response = client.patch(f"/chats/{chat['id']}",
                            json={"colour": "accent", "icon": "folder", "pinned": True}, headers=phone)
    assert response.status_code == 200
    assert (response.json()["colour"], response.json()["icon"], response.json()["pinned"]) == \
        ("accent", "folder", True)
    assert app.state.store.chat_capabilities(chat["id"]) is None

    assert client.patch(f"/chats/{chat['id']}", json={"title": "phone title"},
                        headers=phone).status_code == 200
    assert app.state.store.chat_capabilities(chat["id"]) == {"chat", "models:read"}
