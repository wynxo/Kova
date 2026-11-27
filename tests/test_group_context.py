"""The model-facing view of the same transcript differs for each group bot."""

from copy import deepcopy

import pytest

from kova.group_context import view_for
from kova.store import Store


def user(content, **meta):
    return {"role": "user", "content": content, **meta}


def reply(author, content, **meta):
    return {"role": "assistant", "author": author, "content": content, **meta}


def call(author, ident, name, **meta):
    return reply(author, None, tool_calls=[
        {"id": ident, "type": "function", "function": {"name": name, "arguments": "{}"}}
    ], **meta)


def result(author, ident, content, **meta):
    return {"role": "tool", "author": author, "tool_call_id": ident, "content": content, **meta}


def test_own_tools_are_model_messages_and_input_rows_are_not_mutated():
    rows = [user("Find it", turn_id="one"),
            call("coder", "a", "read_file", bot_turn_id="bot-one"),
            result("coder", "a", "Own result", id=3), reply("coder", "Found it")]
    original = deepcopy(rows)
    view = view_for("coder", rows, {})
    assert view == [user("@you: Find it"),
                    {"role": "assistant", "content": None, "tool_calls": rows[1]["tool_calls"]},
                    {"role": "tool", "tool_call_id": "a", "content": "Own result"},
                    {"role": "assistant", "content": "Found it"}]
    view[1]["tool_calls"][0]["function"]["name"] = "changed"
    assert rows == original


def test_default_assistant_owns_rows_with_an_unset_author():
    assert view_for("kova", [user("Hi"), reply(None, "Hello")], {}) == [
        user("@you: Hi"), {"role": "assistant", "content": "Hello"}]


@pytest.mark.parametrize("share_work", ["replies", "tools", "everything"])
def test_other_work_is_summarized_or_quoted_at_the_requested_level(share_work):
    rows = [user("Find it"),
            call("coder", "a", "read_file", reasoning="r" * 2000 + "HIDDEN_REASON",
                 bot_turn_id="bot-one"),
            result("coder", "a", "x" * 1998 + "\ny" + "HIDDEN_RESULT", bot_turn_id="bot-one"),
            call("coder", "b", "write_file", bot_turn_id="bot-one"),
            result("coder", "b", "Saved", bot_turn_id="bot-one"),
            reply("coder", "Found it", bot_turn_id="bot-one")]
    [message] = view_for("critic", rows, {"share_work": share_work}, handles={"coder": "dev"})
    assert message["role"] == "user"
    content = message["content"]
    assert content.startswith("@you: Find it\n\n@dev: Found it")
    assert "HIDDEN_RESULT" not in content and "HIDDEN_REASON" not in content
    if share_work == "replies":
        assert "(@dev used 2 tools: read_file, write_file)" in content
        assert "Saved" not in content and "xxxxx" not in content
    else:
        assert f'> read_file: {"x" * 1998}\n> y' in content and "> write_file: Saved" in content
    assert ("> Reasoning: " + "r" * 2000 in content) == (share_work == "everything")


def test_hidden_current_replies_hide_the_work_but_keep_historical_and_own_replies():
    rows = [user("Old", turn_id="old"), reply("critic", "Old critic", turn_id="old"),
            user("New", turn_id="new"), reply("critic", "Early critic", turn_id="new"),
            user("Steer", turn_id="new"), call("critic", "a", "read_file", turn_id="new"),
            result("critic", "a", "Current result", turn_id="new"),
            reply("critic", "New critic", turn_id="new"),
            reply("coder", "My reply", turn_id="new"),
            reply("critic", "(pass)", turn_id="new")]
    assert view_for("coder", rows, {"see_earlier_replies": False, "share_work": "everything"}) == [
        user("@you: Old\n\n@critic: Old critic\n\n@you: New\n\n@you: Steer"),
        {"role": "assistant", "content": "My reply"}]


def test_a_stored_snapshot_keeps_the_turn_id_a_steer_needs(tmp_path):
    # The attributed snapshot is the only source of rows in a group, so it must
    # carry turn ids: without them a steer looks like a new turn and a Panel
    # bot sees the replies it is meant to answer independently of.
    store = Store(tmp_path / "kova.db")
    chat_id = store.create_chat()["id"]
    store.add_message(chat_id, "user", "Old", turn_id="old")
    store.add_message(chat_id, "assistant", "Old critic", author="critic", turn_id="old")
    store.add_message(chat_id, "user", "New", turn_id="new")
    store.add_message(chat_id, "assistant", "Early critic", author="critic", turn_id="new")
    store.add_message(chat_id, "user", "Steer", turn_id="new")
    store.add_message(chat_id, "assistant", "My reply", author="coder", turn_id="new")
    _, _, entries = store.context_snapshot(chat_id, attributed=True)
    rows = [message for _, message in entries]
    assert [row["turn_id"] for row in rows] == ["old", "old", "new", "new", "new", "new"]
    assert view_for("coder", rows, {"see_earlier_replies": False}) == [
        user("@you: Old\n\n@critic: Old critic\n\n@you: New\n\n@you: Steer"),
        {"role": "assistant", "content": "My reply"}]


def test_abandoned_tools_and_passes_do_not_leak_into_another_reply():
    rows = [user("First"), call("critic", "a", "read_file", bot_turn_id="abandoned"),
            result("critic", "a", "Abandoned work", bot_turn_id="abandoned"),
            reply("critic", "(pass)", bot_turn_id="pass"),
            reply("critic", "Nothing to add", bot_turn_id="final"),
            reply("coder", " (pass) ")]
    assert view_for("coder", rows, {"share_work": "everything"}) == [
        user("@you: First\n\n@critic: Nothing to add")]


def test_reused_call_ids_keep_each_rounds_tool_name_and_count():
    rows = [user("Fix it"), call("coder", "call_0", "read_file"),
            result("coder", "call_0", "Before"), call("coder", "call_0", "write_file"),
            result("coder", "call_0", "After"), reply("coder", "Fixed it")]
    [summary] = view_for("critic", rows, {})
    assert "(@coder used 2 tools: read_file, write_file)" in summary["content"]
    [quoted] = view_for("critic", rows, {"share_work": "tools"})
    assert "> read_file: Before\n> write_file: After" in quoted["content"]


def test_trimming_drops_old_joined_text_and_leaves_current_user_and_tool_pairs():
    old = "old" * 80
    rows = [user(old), reply("critic", old), user("Current ask"),
            call("coder", "a", "read_file"), result("coder", "a", "x"),
            reply("critic", "A later reply")]
    assert view_for("coder", rows, {}, context_tokens=42, reserved_chars=10) == [
        user("@you: Current ask"),
        {"role": "assistant", "content": None, "tool_calls": rows[3]["tool_calls"]},
        {"role": "tool", "tool_call_id": "a", "content": "x"},
        user("@critic: A later reply")]
    [merged] = view_for("coder", rows[:3], {}, context_tokens=42)
    assert merged == user("@you: Current ask")


def test_compaction_summary_is_shared_without_speaker_attribution():
    assert view_for("coder", [user("New")], {}, summary="Shared facts") == [
        user("Conversation summary (earlier context):\nShared facts\n\n@you: New")]


def test_trimming_does_not_charge_separators_for_dropped_rows():
    rows = [user("Very old") for _ in range(100)] + [user("Keep this"), user("Latest")]
    [merged] = view_for("kova", rows, {}, context_tokens=11)
    assert merged == user("@you: Keep this\n\n@you: Latest")
