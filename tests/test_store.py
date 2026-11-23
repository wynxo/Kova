import json
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


def test_migration_adds_pin_to_existing_chats(tmp_path):
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
    assert store.get_chat("legacy")["pinned"] is False
    assert store.list_chats()[0]["pinned"] is False

    store.update_chat("legacy", pinned=True)
    assert Store(path).get_chat("legacy")["pinned"] is True


def test_pinned_chats_sort_first_until_unpinned(tmp_path):
    path = tmp_path / "kova.db"
    store = Store(path)
    first = store.create_chat("first")
    time.sleep(0.001)
    second = store.create_chat("second")
    assert [chat["id"] for chat in store.list_chats()] == [second["id"], first["id"]]

    store.update_chat(first["id"], pinned=True)
    assert [chat["id"] for chat in store.list_chats()] == [first["id"], second["id"]]
    assert Store(path).get_chat(first["id"])["pinned"] is True

    store.update_chat(first["id"], pinned=False)
    assert [chat["id"] for chat in Store(path).list_chats()] == [second["id"], first["id"]]


def test_search_results_keep_the_pin(store):
    pinned = store.create_chat("Pinned")
    store.add_message(pinned["id"], "user", "apple pie")
    time.sleep(0.001)
    newer = store.create_chat("Newer")
    store.add_message(newer["id"], "user", "apple tart")
    store.update_chat(pinned["id"], pinned=True)

    results = store.search_chats("apple")
    assert [result["id"] for result in results] == [pinned["id"], newer["id"]]
    assert [result["pinned"] for result in results] == [True, False]


def test_bot_inbox_preview_is_plain_text(store):
    bot = store.create_chat("Research", kind="bot", bot_id="researcher")
    store.add_message(bot["id"], "assistant",
                      "## Plan\n\n**Start small.** Read [the notes](https://example.com) "
                      "and run `check.py` *twice*.\n\n- one\n- two")
    [row] = [chat for chat in store.list_chats() if chat["id"] == bot["id"]]
    assert row["message_preview"] == "Plan Start small. Read the notes and run check.py twice. one two"


def test_search_workspace_filters_before_limit(store):
    kova = store.create_chat("Plan")
    bot = store.create_chat("Research", kind="bot", bot_id="researcher")
    group = store.create_chat("Review", kind="group", members=["researcher"])
    for chat in (kova, bot, group):
        store.add_message(chat["id"], "user", "a matching note")
    store.update_chat(bot["id"], pinned=True)

    [regular] = store.search_chats("matching", limit=1, workspace="kova")
    assert (regular["id"], regular["kind"]) == (kova["id"], "chat")
    assert [row["kind"] for row in store.search_chats("matching", workspace="bots")] == ["bot", "group"]
    assert len(store.search_chats("matching")) == 3


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


def test_project_migration_preserves_existing_chat(tmp_path):
    path = tmp_path / "kova.db"
    folder = str(tmp_path / "workspace")
    old = Store(path)
    chat = old.create_chat("Existing", cwd=folder)
    old.add_message(chat["id"], "user", "Keep this message")
    old._conn.close()
    with sqlite3.connect(path) as db:
        columns = {row[1] for row in db.execute("PRAGMA table_info(chats)")}
        if "project_id" in columns:
            db.execute("ALTER TABLE chats DROP COLUMN project_id")
        db.execute("DROP TABLE IF EXISTS projects")

    migrated = Store(path)
    saved = migrated.get_chat(chat["id"])
    assert saved["project_id"] is None
    assert (saved["title"], saved["created"], saved["cwd"]) == ("Existing", chat["created"], folder)
    assert saved["messages"] == [{"role": "user", "content": "Keep this message"}]
    project = migrated.create_project("Research", cwd=folder)
    grouped = migrated.create_chat(project_id=project["id"])
    migrated._conn.close()

    reopened = Store(path)
    assert reopened.get_project(project["id"]) == project
    assert reopened.get_chat(grouped["id"])["project_id"] == project["id"]
    assert reopened.get_chat(grouped["id"])["cwd"] == folder
    assert reopened.get_chat(chat["id"]) == saved


# ------------------------------------------------------- bots (task 1.4)
def _pre_bot_database(path):
    """What an older Kova left behind: no chat kind and no author columns."""
    conn = sqlite3.connect(path)
    conn.executescript(
        "CREATE TABLE chats (id TEXT PRIMARY KEY, title TEXT, created TEXT NOT NULL, "
        "mode TEXT NOT NULL DEFAULT 'work', web INTEGER NOT NULL DEFAULT 1, "
        "pinned INTEGER NOT NULL DEFAULT 0);"
        "CREATE TABLE messages (id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "chat_id TEXT NOT NULL, role TEXT NOT NULL, content TEXT, tool_calls TEXT, "
        "tool_call_id TEXT, created TEXT);"
        "INSERT INTO chats (id, title, created) "
        "VALUES ('legacy', 'Old chat', '2026-01-01T00:00:00+00:00');"
        "INSERT INTO messages (chat_id, role, content, created) VALUES "
        "('legacy', 'user', 'hello', '2026-01-01T00:00:00+00:00'), "
        "('legacy', 'assistant', 'hi there', '2026-01-01T00:00:01+00:00');"
    )
    conn.commit()
    conn.close()


def test_a_pre_bot_database_migrates_without_changing_a_single_row(tmp_path):
    path = tmp_path / "kova.db"
    _pre_bot_database(path)

    store = Store(path)
    chat = store.get_chat("legacy", include_message_meta=True)
    assert (chat["kind"], chat["bot_id"]) == ("chat", None)
    assert store.list_chats()[0]["kind"] == "chat"
    assert store.list_chats()[0]["bot_id"] is None
    # A row written before bots existed belongs to the user and the default
    # assistant, so its shape is exactly what it was.
    assert chat["messages"][0] == {"role": "user", "content": "hello", "id": 1,
                                   "created": "2026-01-01T00:00:00+00:00"}
    assert "author" not in chat["messages"][1] and "hop" not in chat["messages"][1]

    # The new columns work on the migrated database, and survive a reopen.
    bot_chat = store.create_chat("Bots", kind="bot", bot_id="researcher")
    message_id = store.add_message(bot_chat["id"], "assistant", "found it",
                                   author="researcher", author_name="Researcher",
                                   bot_turn_id="turn-1", hop=2)
    store._conn.close()
    reopened = Store(path)
    saved = reopened.get_chat(bot_chat["id"], include_message_meta=True)
    assert (saved["kind"], saved["bot_id"]) == ("bot", "researcher")
    reply = saved["messages"][0]
    assert reply["id"] == message_id and reply["content"] == "found it"
    assert (reply["author"], reply["author_name"]) == ("researcher", "Researcher")
    assert (reply["bot_turn_id"], reply["hop"]) == ("turn-1", 2)


def test_a_chats_kind_is_fixed_to_a_known_shape(store):
    assert store.create_chat("plain")["kind"] == "chat"
    with pytest.raises(ValueError, match="bot_id"):
        store.create_chat("no bot", kind="bot")
    with pytest.raises(ValueError, match="only a bot chat"):
        store.create_chat("odd", bot_id="researcher")
    with pytest.raises(ValueError, match="unknown chat kind"):
        store.create_chat("odd", kind="squad")
    # Incognito stays conversation-only with the default assistant.
    with pytest.raises(ValueError, match="Incognito"):
        store.create_chat("private bot", ephemeral=True, kind="bot", bot_id="researcher")
    assert [chat["title"] for chat in store.list_chats(include_ephemeral=True)] == ["plain"]


def test_default_rows_keep_their_shape_and_only_bot_rows_carry_an_author(store):
    chat = store.create_chat()
    store.add_message(chat["id"], "user", "hello")
    store.add_message(chat["id"], "assistant", "hi")
    assert store.get_messages(chat["id"]) == [
        {"role": "user", "content": "hello"}, {"role": "assistant", "content": "hi"}]
    with_meta = store.get_messages(chat["id"], include_message_meta=True)
    assert all("author" not in row and "bot_turn_id" not in row for row in with_meta)


def test_a_fork_keeps_who_wrote_each_row(store):
    chat = store.create_chat()
    store.add_message(chat["id"], "user", "hello")
    store.add_message(chat["id"], "assistant", "answer", author="researcher",
                      author_name="Researcher", bot_turn_id="bt1", hop=1)
    fork = store.fork_chat(chat["id"])
    saved = store.get_messages(fork["id"], include_message_meta=True)
    assert saved[0].get("author") is None
    assert (saved[1]["author"], saved[1]["author_name"]) == ("researcher", "Researcher")
    assert (saved[1]["bot_turn_id"], saved[1]["hop"]) == ("bt1", 1)
    assert (fork["kind"], fork["bot_id"]) == ("chat", None)


def test_an_import_keeps_a_bot_chat_and_its_authors(tmp_path):
    source = Store(tmp_path / "a.db")
    chat = source.create_chat("Direct", kind="bot", bot_id="researcher")
    source.add_message(chat["id"], "user", "hello")
    source.add_message(chat["id"], "assistant", "answer", author="researcher",
                       author_name="Researcher", bot_turn_id="bt1")
    exported = json.loads(json.dumps(source.get_chat(chat["id"], include_created=True,
                                                     include_message_meta=True)))
    target = Store(tmp_path / "b.db")
    assert target.import_chat(exported)
    saved = target.get_chat(chat["id"], include_message_meta=True)
    assert (saved["kind"], saved["bot_id"]) == ("bot", "researcher")
    assert saved["messages"][1]["author"] == "researcher"
    # A hand-edited backup cannot invent a bot chat without a bot.
    assert target.import_chat({**exported, "id": "odd", "bot_id": None})
    assert target.get_chat("odd")["kind"] == "chat"
    assert target.import_chat({**exported, "id": "weird", "kind": "squad"})
    assert target.get_chat("weird")["kind"] == "chat"


def test_search_results_say_who_wrote_the_matching_message(store):
    chat = store.create_chat()
    store.add_message(chat["id"], "assistant", "the researcher found sources",
                      author="researcher", author_name="Researcher")
    [result] = store.search_chats("researcher")
    assert result["author"] == "researcher"
    store.add_message(chat["id"], "user", "a plain note about researchers")
    assert all("author" in result for result in store.search_chats("researcher"))


def test_pre_group_database_preserves_bot_chat_and_adds_membership(tmp_path):
    path = tmp_path / "kova.db"
    store = Store(path)
    direct = store.create_chat("Direct", kind="bot", bot_id="coder")
    store.add_message(direct["id"], "assistant", "Old reply", author="coder")
    with store._conn:
        store._conn.execute("DROP TABLE IF EXISTS chat_members")
        if "group_settings" in {row["name"] for row in store._conn.execute("PRAGMA table_info(chats)")}:
            store._conn.execute("ALTER TABLE chats DROP COLUMN group_settings")
    store._conn.close()

    store = Store(path)
    saved = store.get_chat(direct["id"], include_message_meta=True)
    assert (saved["kind"], saved["bot_id"]) == ("bot", "coder")
    assert saved["messages"][0]["content"] == "Old reply"
    assert saved["messages"][0]["author"] == "coder"
    group = store.create_chat("Group", kind="group", members=["coder", "kova"],
                              group_settings={"share_work": "tools"})
    cid = group["id"]
    store.update_chat_member(cid, "coder", muted=True, settings={"max_rounds": 2})
    store._conn.close()

    reopened = Store(path)
    saved = reopened.get_chat(cid)
    assert saved["group_settings"] == {"share_work": "tools"}
    assert [(m["bot_id"], m["muted"], m["settings"]) for m in saved["members"]] == [
        ("coder", True, {"max_rounds": 2}), ("kova", False, {})]
    fork = reopened.fork_chat(cid)
    assert fork["group_settings"] == saved["group_settings"] and fork["members"] == saved["members"]
    assert reopened.delete_chat(cid)
    assert not reopened._conn.execute("SELECT 1 FROM chat_members WHERE chat_id = ?", (cid,)).fetchone()
