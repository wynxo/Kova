"""Chat search: the FTS index, the LIKE fallback, migrations and the API."""

import sqlite3
import time

import pytest
from fastapi.testclient import TestClient

import kova.store as store_module
from kova.config import ensure_token, load_config
from kova.server import create_app
from kova.store import Store
from tests.fake_model import FakeModel, text


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / "kova.db")


def test_search_finds_user_and_assistant_text(store):
    chat = store.create_chat("Apple pie")
    store.add_message(chat["id"], "user", "How do I bake an apple pie?")
    store.add_message(chat["id"], "assistant", "Use apples, flour and butter.")

    results = store.search_chats("apple")
    assert len(results) == 1
    result = results[0]
    assert result["id"] == chat["id"]
    assert result["title"] == "Apple pie"
    assert result["role"] in ("user", "assistant")
    assert "apple" in result["snippet"].lower()
    assert result["message_count"] == 2


def test_search_matches_prefixes_and_ignores_tool_text(store):
    chat = store.create_chat("Tools")
    store.add_message(chat["id"], "user", "Look up zebra facts")
    store.add_message(chat["id"], "assistant", None,
                      tool_calls=[{"id": "1", "type": "function",
                                   "function": {"name": "web_search",
                                                "arguments": '{"query": "zebra"}'}}])
    store.add_message(chat["id"], "tool", "zebra tool output", tool_call_id="1")
    tool_only = store.create_chat("Only tool")
    store.add_message(tool_only["id"], "tool", "secret zebra output", tool_call_id="2")

    results = store.search_chats("zeb")
    assert [r["id"] for r in results] == [chat["id"]]
    assert store.search_chats("secret") == []


def test_search_is_newest_first(store):
    older = store.create_chat("older")
    store.add_message(older["id"], "user", "needle in the older chat")
    time.sleep(0.01)
    newer = store.create_chat("newer")
    store.add_message(newer["id"], "user", "needle in the newer chat")

    assert [r["id"] for r in store.search_chats("needle")] == [newer["id"], older["id"]]


def test_search_stays_in_sync_with_inserts_and_deletes(store):
    chat = store.create_chat()
    store.add_message(chat["id"], "user", "first needle")
    assert store.search_chats("needle")
    store.delete_chat(chat["id"])
    assert store.search_chats("needle") == []


def test_fts_migration_indexes_an_existing_database(tmp_path):
    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE chats (id TEXT PRIMARY KEY, title TEXT, created TEXT NOT NULL);
        CREATE TABLE messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id TEXT NOT NULL,
            role TEXT NOT NULL,
            content TEXT,
            tool_calls TEXT,
            tool_call_id TEXT
        );
        """
    )
    conn.execute("INSERT INTO chats VALUES ('old1', 'Old chat', '2026-01-01T00:00:00+00:00')")
    conn.execute("INSERT INTO messages (chat_id, role, content) VALUES ('old1', 'user', 'legacy needle')")
    conn.execute("INSERT INTO messages (chat_id, role, content) VALUES ('old1', 'assistant', 'found it')")
    conn.commit()
    conn.close()

    store = Store(path)
    assert store.fts_enabled
    results = store.search_chats("legacy")
    assert [r["id"] for r in results] == ["old1"]
    assert "legacy" in results[0]["snippet"]


def test_like_fallback_when_fts5_is_unavailable(tmp_path, monkeypatch):
    monkeypatch.setattr(store_module, "_fts5_available", lambda conn: False)
    store = Store(tmp_path / "kova.db")
    assert store.fts_enabled is False

    chat = store.create_chat("Fallback")
    store.add_message(chat["id"], "user", "A long message starts here and then mentions a needle in passing.")
    store.add_message(chat["id"], "assistant", "Nothing relevant.")
    tool_only = store.create_chat("Tool")
    store.add_message(tool_only["id"], "tool", "needle tool output", tool_call_id="1")

    results = store.search_chats("needle")
    assert [r["id"] for r in results] == [chat["id"]]
    assert "needle" in results[0]["snippet"]
    assert store.search_chats("missing") == []

    store.delete_chat(chat["id"])
    assert store.search_chats("needle") == []


@pytest.fixture
def server(tmp_path, monkeypatch):
    monkeypatch.setenv("KOVA_HOME", str(tmp_path))
    cfg = load_config()
    fake = FakeModel([text("Hello there!")])
    app = create_app(cfg, transport=fake.transport)
    headers = {"Authorization": f"Bearer {ensure_token(cfg.home)}"}
    with TestClient(app) as client:
        yield client, headers, cfg, fake


def test_search_endpoint_returns_snippets_newest_first(server):
    client, headers, cfg, fake = server
    store = client.app.state.store
    older = store.create_chat("Older chat")
    store.add_message(older["id"], "user", "apple pie from last week")
    time.sleep(0.01)
    newer = store.create_chat("Newer chat")
    store.add_message(newer["id"], "assistant", "an apple a day")

    response = client.get("/chats/search", params={"q": "apple"}, headers=headers)
    assert response.status_code == 200
    results = response.json()
    assert [r["id"] for r in results] == [newer["id"], older["id"]]
    assert "apple" in results[0]["snippet"].lower()
    assert results[0]["title"] == "Newer chat"

    assert client.get("/chats/search", params={"q": "nothing"}).status_code == 401
    empty = client.get("/chats/search", params={"q": "nothing"}, headers=headers)
    assert empty.status_code == 200 and empty.json() == []
    limited = client.get("/chats/search", params={"q": "apple", "limit": 1}, headers=headers)
    assert [r["id"] for r in limited.json()] == [newer["id"]]
