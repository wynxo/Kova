import sqlite3
import time
from datetime import datetime, timedelta, timezone

import pytest

from kova.store import Store


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / "kova.db")


SAMPLE_TOOL_CALLS = [
    {
        "id": "call_1",
        "type": "function",
        "function": {
            "name": "memory_save",
            "arguments": '{"title": "Dog", "body": "Rex"}',
        },
    }
]


def test_chat_roundtrip(store):
    chat = store.create_chat("Test")
    assert chat["id"] and chat["created"]
    cid = chat["id"]

    store.add_message(cid, "user", "hello")
    store.add_message(cid, "assistant", None, tool_calls=SAMPLE_TOOL_CALLS)
    store.add_message(cid, "tool", None, tool_call_id="call_1")

    assert store.get_messages(cid) == [
        {"role": "user", "content": "hello"},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": SAMPLE_TOOL_CALLS,
        },
        {"role": "tool", "content": None, "tool_call_id": "call_1"},
    ]


def test_chats_default_to_work_mode_with_web(store):
    chat = store.create_chat()
    assert chat["mode"] == "work" and chat["web"] is True

    fetched = store.get_chat(chat["id"])
    assert fetched["mode"] == "work" and fetched["web"] is True

    listed = store.list_chats()[0]
    assert listed["mode"] == "work" and listed["web"] is True


def test_create_chat_with_mode_and_web(store):
    chat = store.create_chat("Small talk", mode="chat", web=False)
    assert chat["mode"] == "chat" and chat["web"] is False

    fetched = store.get_chat(chat["id"])
    assert fetched["mode"] == "chat" and fetched["web"] is False

    listed = store.list_chats()[0]
    assert listed["mode"] == "chat" and listed["web"] is False


def test_create_chat_rejects_unknown_mode(store):
    with pytest.raises(ValueError, match="unknown mode 'code'"):
        store.create_chat(mode="code")


def test_update_chat_changes_mode_web_and_title(store):
    chat = store.create_chat()
    cid = chat["id"]

    store.update_chat(cid, mode="chat", web=False)
    assert store.get_chat(cid)["mode"] == "chat"
    assert store.get_chat(cid)["web"] is False

    store.update_chat(cid, mode="work", web=True, title="Renamed")
    fetched = store.get_chat(cid)
    assert (fetched["title"], fetched["mode"], fetched["web"]) == ("Renamed", "work", True)

    with pytest.raises(ValueError, match="unknown mode 'bogus'"):
        store.update_chat(cid, mode="bogus")


def test_migration_adds_mode_and_web_to_existing_chats(tmp_path):
    path = tmp_path / "kova.db"
    conn = sqlite3.connect(path)
    conn.executescript(
        "CREATE TABLE chats (id TEXT PRIMARY KEY, title TEXT, created TEXT NOT NULL);"
        "INSERT INTO chats (id, title, created) "
        "VALUES ('legacy', 'Old chat', '2026-01-01T00:00:00+00:00');"
    )
    conn.commit()
    conn.close()

    store = Store(path)
    legacy = store.get_chat("legacy")
    assert (legacy["mode"], legacy["web"]) == ("work", True)  # today's behaviour is kept

    store.update_chat("legacy", mode="chat", web=False)
    assert (store.get_chat("legacy")["mode"], store.get_chat("legacy")["web"]) == ("chat", False)


def test_list_newest_first(store):
    first = store.create_chat("first")
    time.sleep(0.001)
    second = store.create_chat("second")
    time.sleep(0.001)
    third = store.create_chat("third")
    time.sleep(0.001)
    fourth = store.create_chat("fourth")

    assert [c["id"] for c in store.list_chats()] == [
        fourth["id"],
        third["id"],
        second["id"],
        first["id"],
    ]


def test_list_chats_includes_title_last_message_and_count(store):
    old = store.create_chat()
    store.create_chat("newer but empty")
    time.sleep(0.001)
    store.add_message(old["id"], "user", "hello there")
    store.add_message(old["id"], "assistant", "hi")
    store.set_title(old["id"], "Greeting")

    listed = store.list_chats()
    assert listed[0]["id"] == old["id"]  # newest activity comes first
    assert listed[0]["title"] == "Greeting"
    assert listed[0]["message_count"] == 2
    assert listed[0]["last_message"] >= listed[0]["created"]
    assert listed[1]["message_count"] == 0
    assert listed[1]["last_message"] == listed[1]["created"]


def test_delete_chat_removes_messages(store):
    chat = store.create_chat()
    cid = chat["id"]
    store.add_message(cid, "user", "hi")
    assert store.get_chat(cid)["messages"] == [{"role": "user", "content": "hi"}]

    assert store.delete_chat(cid) is True
    assert store.get_chat(cid) is None
    assert store.get_messages(cid) == []


def test_reminders_due_and_done(store):
    now = datetime(2026, 11, 12, 10, 0, 0, tzinfo=timezone.utc)
    past = now - timedelta(minutes=1)
    future = now + timedelta(hours=1)

    past_id = store.add_reminder(past, "past reminder")
    store.add_reminder(future, "future reminder")

    assert [r["text"] for r in store.due_reminders(now)] == ["past reminder"]

    store.mark_reminder_done(past_id)
    assert store.due_reminders(now) == []

    listed = store.list_reminders()
    assert [r["text"] for r in listed] == ["future reminder"]
    assert listed[0]["done"] is False


def test_add_reminder_requires_tz(store):
    with pytest.raises(ValueError):
        store.add_reminder(datetime(2026, 11, 12, 10, 0, 0), "no tz")


def test_reminders_sorted_by_real_time_across_timezones(tmp_path):
    from datetime import datetime, timedelta, timezone

    store = Store(tmp_path / "kova.db")
    berlin = timezone(timedelta(hours=1))
    # 10:00+01:00 is 09:00 UTC, so it comes before 09:30 UTC (text order would get this wrong)
    a = store.add_reminder(datetime(2026, 11, 12, 9, 30, tzinfo=timezone.utc), "later")
    b = store.add_reminder(datetime(2026, 11, 12, 10, 0, tzinfo=berlin), "earlier")
    assert [r["id"] for r in store.list_reminders()] == [b, a]


def test_import_chat_keeps_mode_and_web(tmp_path):
    store = Store(tmp_path / "kova.db")
    assert store.import_chat({"id": "abc", "title": "t", "mode": "chat", "web": False, "messages": []})
    chat = store.get_chat("abc")
    assert chat["mode"] == "chat" and chat["web"] is False
    assert store.import_chat({"id": "old", "messages": []})  # backups from before modes
    old = store.get_chat("old")
    assert old["mode"] == "work" and old["web"] is True


def test_fork_preserves_context_and_is_independent(store):
    source = store.create_chat('Original', mode='chat', web=False)
    cid = source['id']
    first = store.add_message(cid, 'user', 'earlier')
    store.add_message(cid, 'assistant', 'later')
    assert store.save_compaction(cid, 0, first, 'summary')
    fork = store.fork_chat(cid)
    assert fork['id'] != cid
    assert fork['mode'] == 'chat' and not fork['web']
    assert store.get_context(fork['id']) == store.get_context(cid)
    store.add_message(fork['id'], 'user', 'different')
    assert len(store.get_messages(cid)) == 2
    assert store.fork_chat('missing') is None
