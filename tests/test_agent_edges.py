"""Edge cases for the agent loop, prompt building and history helpers.

The happy paths live in ``test_agent.py``; this file probes empty, unicode,
over-long and interrupted inputs, the ten-round tool limit, and the invariant
that a trimmed request never contains a half-answered tool call.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from kova.agent import (
    MAX_TOOL_RESULT_CHARS,
    Agent,
    build_system_prompt,
    derive_title,
    repair_tool_pairs,
    select_tools,
    trim_history,
)
from kova.config import ModelEntry, load_config
from kova.memory import Memory, Note
from kova.store import Store
from kova.tools import Tool, ToolContext, build_tools
from tests.fake_model import FakeModel, text, tool


@pytest.fixture
def setup(tmp_path):
    cfg = load_config(tmp_path)
    store = Store(tmp_path / "db.sqlite")
    memory = Memory(cfg.memory_path)
    tools = build_tools(ToolContext(cfg, store, memory, lambda: datetime.now().astimezone()))
    chat = store.create_chat()
    return cfg, store, memory, tools, chat["id"]


async def run_turn(setup, scripts, message="hello", model=None):
    cfg, store, memory, tools, chat_id = setup
    fake = FakeModel(scripts)
    agent = Agent(cfg, store, memory, tools, transport=fake.transport)
    events = [event async for event in agent.run_turn(chat_id, message, model=model)]
    return events, fake


def batch(*specs):
    """One streamed assistant message carrying several tool calls.

    ``specs`` are ``(name, args, call_id)`` triples in the order the model emits
    them.
    """
    return [{"tool_calls": [
        {"index": index, "id": call_id, "type": "function",
         "function": {"name": name, "arguments": json.dumps(args)}}
        for index, (name, args, call_id) in enumerate(specs)
    ]}]


def tool_results(events):
    return [event for event in events if event["type"] == "tool_result"]


def assert_tool_pairs_wired(messages):
    """Every assistant tool call has a reply, and every reply a call."""
    pending: set[str] = set()
    for message in messages:
        role = message["role"]
        if role == "assistant":
            assert not pending, f"unanswered tool calls {pending} before assistant message"
            if message.get("tool_calls"):
                pending = {call["id"] for call in message["tool_calls"]}
        elif role == "tool":
            assert message["tool_call_id"] in pending, f"orphan tool reply {message['tool_call_id']}"
            pending.discard(message["tool_call_id"])
        elif role == "user":
            assert not pending, f"unanswered tool calls {pending} before user message"
    assert not pending, f"unanswered tool calls {pending} at the end of the request"


# -- derive_title ---------------------------------------------------------

def test_derive_title_empty_and_whitespace_use_the_default():
    assert derive_title("") == "New chat"
    assert derive_title("   ") == "New chat"
    assert derive_title("\n\t  \r\n") == "New chat"


def test_derive_title_cuts_at_a_word_boundary():
    title = derive_title("The quick brown fox jumps over the lazy dog")
    assert title == "The quick brown fox jumps over the lazy"
    assert len(title) <= 40
    assert not title.endswith(" ")


def test_derive_title_hard_cuts_a_first_word_longer_than_the_limit():
    title = derive_title("x" * 500)
    assert title == "x" * 40
    assert derive_title("abcdefghij rest", limit=10) == "abcdefghij"
    assert derive_title("one two three", limit=3) == "one"


def test_derive_title_collapses_whitespace_and_keeps_unicode():
    assert derive_title("hello\tworld\nagain  now") == "hello world again now"
    assert derive_title("猫" * 30) == "猫" * 30
    assert derive_title("Café résumé naïve thoughts and more words here") == (
        "Café résumé naïve thoughts and more"
    )
    title = derive_title("🐶 dogs are the best")
    assert title == "🐶 dogs are the best"
    assert len(title) <= 40


def test_derive_title_does_not_split_multi_codepoint_emoji():
    title = derive_title("👨‍👩‍👧‍👦" * 10, limit=13)
    assert len(title) <= 13
    assert not title.endswith("\u200d")


# -- repair_tool_pairs ----------------------------------------------------

def test_repair_tool_pairs_empty_and_plain_history():
    assert repair_tool_pairs([]) == []
    plain = [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "hello"},
    ]
    assert repair_tool_pairs(plain) == plain


def test_repair_tool_pairs_leaves_complete_batches_untouched():
    messages = [
        {"role": "user", "content": "save two things"},
        {"role": "assistant", "content": "working", "tool_calls": [
            {"id": "a", "type": "function", "function": {"name": "x", "arguments": "{}"}},
            {"id": "b", "type": "function", "function": {"name": "y", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "a", "content": "first"},
        {"role": "tool", "tool_call_id": "b", "content": "second"},
        {"role": "user", "content": "thanks"},
    ]
    assert repair_tool_pairs(messages) == messages


def test_repair_tool_pairs_fills_only_the_missing_replies_in_order():
    messages = [
        {"role": "assistant", "content": "working", "tool_calls": [
            {"id": "a", "type": "function", "function": {"name": "x", "arguments": "{}"}},
            {"id": "b", "type": "function", "function": {"name": "y", "arguments": "{}"}},
            {"id": "c", "type": "function", "function": {"name": "z", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "b", "content": "second"},
        {"role": "user", "content": "again"},
    ]
    fixed = repair_tool_pairs(messages)
    assert fixed[0] == messages[0]
    assert fixed[0]["content"] == "working"  # assistant text is preserved
    assert [(m["role"], m.get("tool_call_id")) for m in fixed[1:4]] == [
        ("tool", "b"), ("tool", "a"), ("tool", "c")]
    assert fixed[4] == {"role": "user", "content": "again"}


def test_repair_tool_pairs_fixes_several_interrupted_batches():
    messages = [
        {"role": "user", "content": "go"},
        {"role": "assistant", "content": None, "tool_calls": [
            {"id": "a", "type": "function", "function": {"name": "x", "arguments": "{}"}}]},
        {"role": "assistant", "content": None, "tool_calls": [
            {"id": "b", "type": "function", "function": {"name": "y", "arguments": "{}"}}]},
        {"role": "user", "content": "hello?"},
    ]
    fixed = repair_tool_pairs(messages)
    assert [(m["role"], m.get("tool_call_id")) for m in fixed] == [
        ("user", None),
        ("assistant", None), ("tool", "a"),
        ("assistant", None), ("tool", "b"),
        ("user", None),
    ]
    # repair is idempotent: a second pass changes nothing
    assert repair_tool_pairs(fixed) == fixed


def test_repair_tool_pairs_ignores_an_empty_tool_calls_list():
    messages = [
        {"role": "assistant", "content": "plain", "tool_calls": []},
        {"role": "user", "content": "next"},
    ]
    assert repair_tool_pairs(messages) == messages


# -- trim_history ---------------------------------------------------------

def test_trim_history_empty_and_within_budget():
    assert trim_history([], 1000, 0) == []
    messages = [{"role": "user", "content": "hi"},
                {"role": "assistant", "content": "hello"}]
    assert trim_history(messages, 1000, 0) == messages


def test_trim_history_keeps_the_latest_user_message_even_when_it_alone_overflows():
    messages = [
        {"role": "user", "content": "old " * 100},
        {"role": "assistant", "content": "answer"},
        {"role": "user", "content": "latest " * 1000},
    ]
    trimmed = trim_history(messages, 100, 0)
    assert trimmed == [messages[2]]


def test_trim_history_never_starts_with_an_orphan_tool_reply():
    messages = [
        {"role": "user", "content": "u" * 100},
        {"role": "assistant", "content": None, "tool_calls": [
            {"id": "a", "type": "function", "function": {"name": "x", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "a", "content": "r" * 400},
        {"role": "user", "content": "latest"},
    ]
    trimmed = trim_history(messages, 100, 0)
    assert trimmed[0]["role"] != "tool"
    assert trimmed == [messages[3]]


def test_trim_history_keeps_an_assistant_call_and_its_reply_together():
    messages = [
        {"role": "user", "content": "u" * 500},
        {"role": "assistant", "content": None, "tool_calls": [
            {"id": "a", "type": "function", "function": {"name": "x", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "a", "content": "r" * 10},
        {"role": "user", "content": "latest"},
    ]
    trimmed = trim_history(messages, 100, 0)
    assert trimmed == messages[1:]
    assert_tool_pairs_wired(trimmed)


def test_trim_history_with_a_negative_budget_keeps_only_the_latest_user():
    messages = [
        {"role": "user", "content": "old"},
        {"role": "assistant", "content": "mid"},
        {"role": "user", "content": "new"},
        {"role": "assistant", "content": "end"},
    ]
    assert trim_history(messages, 0, 100) == messages[2:]


def test_trim_history_counts_tool_call_arguments_in_the_budget():
    arguments = json.dumps({"content": "x" * 100_000})
    messages = [
        {"role": "user", "content": "write the file"},
        {"role": "assistant", "content": "on it", "tool_calls": [
            {"id": "call_1", "type": "function",
             "function": {"name": "write_file", "arguments": arguments}}]},
        {"role": "tool", "tool_call_id": "call_1", "content": "ok"},
        {"role": "user", "content": "thanks"},
    ]
    trimmed = trim_history(messages, context_tokens=1000, reserved_chars=0)
    assert len(json.dumps(trimmed)) <= 1000 * 4 * 0.75


# -- select_tools ---------------------------------------------------------

def _registry(*names):
    return {name: Tool(name, "test", {"type": "object"}, lambda args: "ok")
            for name in names}


def test_select_tools_with_no_registered_tools():
    assert select_tools("work", True, {}) == {}
    assert select_tools("chat", True, {}) == {}
    assert select_tools("chat", False, {}) == {}


def test_select_tools_does_not_mutate_the_registry_and_keeps_order():
    tools = _registry("web_search", "write_file", "memory_save")
    before = list(tools)
    selected = select_tools("work", False, tools)
    assert list(tools) == before
    assert list(selected) == ["write_file", "memory_save"]
    assert selected is not tools


def test_select_tools_unknown_mode_falls_back_to_the_work_toolset():
    # Modes are validated at the store boundary; anything unexpected gets the
    # default (full) toolset rather than silently losing tools.
    tools = _registry("web_search", "write_file")
    assert select_tools("code", True, tools) == tools
    assert select_tools("code", False, tools) == {"write_file": tools["write_file"]}


# -- build_system_prompt --------------------------------------------------

def _note(title, body, tags=(), updated="2026-01-01T00:00:00+00:00"):
    return Note(slug="note", title=title, body=body, tags=list(tags),
                updated=updated, path=Path("/tmp/note.md"))


def test_build_system_prompt_without_tools_or_memories():
    now = datetime(2026, 11, 13, 9, 30, tzinfo=timezone(timedelta(hours=2)))
    prompt = build_system_prompt(now, [], [])
    assert prompt.startswith("You are Kova")
    assert f"Current time: {now.isoformat()}" in prompt
    assert "Current mode: work" in prompt
    assert "Available tools: none" in prompt
    assert "What you remember" not in prompt


def test_build_system_prompt_lists_tools_in_order():
    now = datetime(2026, 11, 13, tzinfo=timezone.utc)
    assert "Available tools: web_search, write_file" in build_system_prompt(
        now, ["web_search", "write_file"], [])


def test_build_system_prompt_formats_unicode_memories_in_order():
    now = datetime(2026, 11, 13, tzinfo=timezone.utc)
    prompt = build_system_prompt(now, ["memory_search"], [
        _note("Café ☕", "naïve résumé 🐶", ["pets"]),
        _note("Empty", ""),
    ])
    assert "Available tools: memory_search" in prompt
    assert "What you remember about the user:\n## Café ☕\nnaïve résumé 🐶\n\n## Empty\n" in prompt


def test_build_system_prompt_unknown_mode_uses_the_work_identity():
    now = datetime(2026, 11, 13, tzinfo=timezone.utc)
    prompt = build_system_prompt(now, [], [], mode="bogus")
    assert "Use tools when they help" in prompt
    assert "You are in chat mode" not in prompt
    assert "Current mode: bogus" in prompt


# -- Agent.run_turn -------------------------------------------------------

async def test_run_turn_empty_message_completes_with_a_default_title(setup):
    events, _ = await run_turn(setup, [text("Hi")], message="")
    assert events[-1]["type"] == "done"
    assert setup[1].get_chat(setup[4])["title"] == "New chat"
    assert setup[1].get_messages(setup[4]) == [
        {"role": "user", "content": ""},
        {"role": "assistant", "content": "Hi"},
    ]


async def test_run_turn_unicode_message_sets_a_unicode_title(setup):
    message = "🐶 dogs are the best companions"
    events, _ = await run_turn(setup, [text("They are!")], message=message)
    assert events[-1]["type"] == "done"
    assert setup[1].get_chat(setup[4])["title"] == message
    assert setup[1].get_messages(setup[4])[0]["content"] == message


async def test_run_turn_very_long_message_gets_a_bounded_title(setup):
    message = "word " * 500
    events, _ = await run_turn(setup, [text("ok")], message=message)
    assert events[-1]["type"] == "done"
    title = setup[1].get_chat(setup[4])["title"]
    assert title == derive_title(message)
    assert len(title) <= 40 and not title.endswith(" ")
    assert setup[1].get_messages(setup[4])[0]["content"] == message


async def test_run_turn_title_comes_from_the_first_user_message_only(setup):
    cfg, store, memory, tools, chat_id = setup
    store.add_message(chat_id, "user", "the first message")
    fake = FakeModel([text("one"), text("two")])
    agent = Agent(cfg, store, memory, tools, transport=fake.transport)
    [event async for event in agent.run_turn(chat_id, "a later message")]
    assert store.get_chat(chat_id)["title"] == "the first message"
    [event async for event in agent.run_turn(chat_id, "another message")]
    assert store.get_chat(chat_id)["title"] == "the first message"


async def test_run_turn_repairs_an_interrupted_tool_call_in_history(setup):
    cfg, store, memory, tools, chat_id = setup
    store.add_message(chat_id, "user", "earlier question")
    store.add_message(chat_id, "assistant", None, tool_calls=[
        {"id": "call_lost", "type": "function",
         "function": {"name": "memory_save", "arguments": "{}"}}])
    events, fake = await run_turn(setup, [text("answer")], message="new question")
    assert events[-1]["type"] == "done"
    sent = fake.requests[0]["messages"]
    assert_tool_pairs_wired(sent)
    assert sent[2]["role"] == "assistant"
    assert sent[3] == {"role": "tool", "tool_call_id": "call_lost",
                       "content": "Error: interrupted"}
    assert sent[4] == {"role": "user", "content": "new question"}


async def test_run_turn_executes_a_batch_of_tool_calls_in_order(setup):
    setup[3]["echo"] = Tool("echo", "test", {"type": "object"},
                            lambda args: f"echo {args['value']}")
    scripts = [
        batch(
            ("memory_save", {"title": "Dog", "body": "Rex is my dog"}, "call_a"),
            ("missing_tool", {}, "call_b"),
            ("echo", {"value": "hello"}, "call_c"),
        ),
        text("All done"),
    ]
    events, fake = await run_turn(setup, scripts)
    results = tool_results(events)
    assert [(r["name"], r["ok"]) for r in results] == [
        ("memory_save", True), ("missing_tool", False), ("echo", True)]
    assert setup[2].get("dog").body == "Rex is my dog"
    stored = setup[1].get_messages(setup[4])
    assert stored[1]["tool_calls"] and [c["id"] for c in stored[1]["tool_calls"]] == [
        "call_a", "call_b", "call_c"]
    assert [m.get("tool_call_id") for m in stored[2:5]] == ["call_a", "call_b", "call_c"]
    assert stored[4]["content"] == "echo hello"
    assert_tool_pairs_wired(fake.requests[1]["messages"])


async def test_run_turn_stops_after_ten_tool_rounds(setup):
    scripts = [tool("missing", {}) for _ in range(12)] + [text("never used")]
    events, fake = await run_turn(setup, scripts)
    assert len(fake.requests) == 11
    assert "tools" not in fake.requests[-1]
    assert len(tool_results(events)) == 10
    assert events[-1]["type"] == "done"
    stored = setup[1].get_messages(setup[4])
    assert sum(1 for m in stored if m.get("tool_calls")) == 10
    assert stored[-1]["role"] == "assistant" and not stored[-1].get("tool_calls")


async def test_run_turn_tool_exception_is_reported_and_the_turn_continues(setup):
    def boom(args):
        raise RuntimeError("boom")

    setup[3]["boom"] = Tool("boom", "test", {"type": "object"}, boom)
    events, fake = await run_turn(setup, [tool("boom", {}, "call_boom"), text("Sorry")])
    result = tool_results(events)[0]
    assert result["name"] == "boom" and result["ok"] is False
    assert result["summary"] == "Error: boom"
    assert fake.requests[1]["messages"][-1] == {
        "role": "tool", "tool_call_id": "call_boom", "content": "Error: boom"}
    assert events[-1]["type"] == "done"


async def test_run_turn_truncates_a_huge_tool_result(setup):
    setup[3]["big"] = Tool("big", "test", {"type": "object"},
                           lambda args: "x" * (MAX_TOOL_RESULT_CHARS + 500))
    events, _ = await run_turn(setup, [tool("big", {}), text("done")])
    events_result = tool_results(events)[0]
    assert events_result["ok"] is True
    assert len(events_result["summary"]) == 200
    stored = setup[1].get_messages(setup[4])
    content = next(m["content"] for m in stored
                   if m["role"] == "tool" and m["tool_call_id"] == "call_1")
    assert content == "x" * MAX_TOOL_RESULT_CHARS + "\n[truncated]"


async def test_run_turn_survives_tool_arguments_that_are_valid_json_but_not_an_object(setup):
    setup[3]["needs_dict"] = Tool("needs_dict", "test", {"type": "object"},
                                  lambda args: args.get("path"))
    script = [{"tool_calls": [{"index": 0, "id": "call_null", "type": "function",
                               "function": {"name": "needs_dict", "arguments": "null"}}]}]
    events, _ = await run_turn(setup, [script, text("Recovered")])
    result = tool_results(events)[0]
    assert result["ok"] is False and result["summary"].startswith("Error: ")
    assert events[-1]["type"] == "done"


async def test_run_turn_memory_search_failure_does_not_block_the_chat(setup):
    cfg, store, memory, tools, chat_id = setup

    def broken(query, limit=5):
        raise RuntimeError("memory index is on fire")

    memory.search = broken
    fake = FakeModel([text("Still here")])
    agent = Agent(cfg, store, memory, tools, transport=fake.transport)
    events = [event async for event in agent.run_turn(chat_id, "hello")]
    assert events[-1]["type"] == "done"
    assert "What you remember" not in fake.requests[0]["messages"][0]["content"]
    assert store.get_messages(chat_id)[-1]["content"] == "Still here"


async def test_run_turn_uses_the_requested_model_for_one_turn_only(setup):
    cfg, store, memory, tools, chat_id = setup
    cfg.models.append(ModelEntry(name="other", base_url="http://model.test/v1",
                                 model="other-model", context=32768))
    fake = FakeModel([text("from other")])
    agent = Agent(cfg, store, memory, tools, transport=fake.transport)
    events = [event async for event in agent.run_turn(chat_id, "hello", model="other")]
    assert fake.requests[0]["model"] == "other-model"
    assert events[-1]["model"] == "other"

    fake_default = FakeModel([text("from default")])
    agent_default = Agent(cfg, store, memory, tools, transport=fake_default.transport)
    events = [event async for event in agent_default.run_turn(chat_id, "again")]
    assert fake_default.requests[0]["model"] == "test-model"
    assert events[-1]["model"] == "local"


async def test_run_turn_streams_reasoning_without_storing_it(setup):
    script = [{"reasoning_content": "thinking hard"}, {"content": "answer"}]
    events, _ = await run_turn(setup, [script])
    assert events[0] == {"type": "reasoning", "text": "thinking hard"}
    stored = setup[1].get_messages(setup[4])
    assert stored[-1] == {"role": "assistant", "content": "answer"}


async def test_run_turn_trims_old_history_but_keeps_the_latest_question(setup):
    cfg, store, memory, tools, chat_id = setup
    cfg.models[0].context = 100
    store.add_message(chat_id, "user", "old " * 1500)
    store.add_message(chat_id, "assistant", None, tool_calls=[
        {"id": "call_old", "type": "function",
         "function": {"name": "memory_search", "arguments": "{}"}}])
    store.add_message(chat_id, "tool", "old result", tool_call_id="call_old")
    _, fake = await run_turn(setup, [text("fresh")], message="current question")
    sent = fake.requests[0]["messages"]
    assert [m["role"] for m in sent] == ["system", "user"]
    assert sent[1] == {"role": "user", "content": "current question"}


async def test_run_turn_never_sends_a_half_answered_tool_pair(setup):
    cfg, store, memory, tools, chat_id = setup
    cfg.models[0].context = 2000
    store.add_message(chat_id, "user", "old " * 1500)
    store.add_message(chat_id, "assistant", None, tool_calls=[
        {"id": "call_old", "type": "function",
         "function": {"name": "memory_search", "arguments": "{}"}}])
    store.add_message(chat_id, "tool", "old result", tool_call_id="call_old")
    _, fake = await run_turn(setup, [text("Earlier context included a tool result."), text("fresh")], message="current question")
    sent = fake.requests[-1]["messages"]
    assert sent[0]["role"] == "system"
    assert sent[-1] == {"role": "user", "content": "current question"}
    assert "Earlier context" in str(sent)
    assert not any(m["role"] == "user" and m["content"].startswith("old") for m in sent)
    assert_tool_pairs_wired(sent)
