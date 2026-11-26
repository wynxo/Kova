"""The group runner: routing, hand-offs, take-turns and every guard (task 2.5).

Each test runs a real group chat through the HTTP API against a scripted model,
so the order of model requests is the order bots answered in.
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from kova.config import ensure_token, load_config
from kova.server import create_app
from tests.fake_model import FakeModel, text


@pytest.fixture
def crew(tmp_path):
    cfg = load_config(tmp_path / "home")
    cfg.files_path = tmp_path / "Kova Files"
    cfg.files_path.mkdir()
    cfg.memory_path = tmp_path / "memory"
    cfg.active_model().model = "test-model"
    fake = FakeModel([])
    app = create_app(cfg, transport=fake.transport)
    with TestClient(app) as client:
        client.headers["Authorization"] = f"Bearer {ensure_token(cfg.home)}"
        for bot_id, name in (("researcher", "Researcher"), ("coder", "Coder"), ("critic", "Critic")):
            response = client.post("/bots", json={"id": bot_id, "name": name,
                                                  "persona": f"You are the {name}.",
                                                  "model": {"mode": "chat", "web": False}})
            assert response.status_code == 200, response.text
        yield client, app, fake


def group(client, members=("researcher", "coder"), **fields):
    response = client.post("/chats", json={"members": list(members), "mode": "chat",
                                           "web": False, **fields})
    assert response.status_code == 200, response.text
    return response.json()["id"]


def send(client, chat_id, message, **fields):
    response = client.post(f"/chats/{chat_id}/messages", json={"text": message, **fields})
    assert response.status_code == 200, response.text
    return [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: ")]


def of(events, kind):
    return [event for event in events if event.get("type") == kind]


def started(events):
    return [event["author"] for event in of(events, "bot_turn_start")]


def last_user(request):
    return [m["content"] for m in request["messages"] if m["role"] == "user"][-1]


def test_mentions_answer_in_order_and_each_bot_sees_the_others(crew):
    client, app, fake = crew
    chat_id = group(client, preset="ask-one", group={"answer": {"lead": "researcher"}})
    fake.scripts[:] = [text("Three sources found."), text("CSV written.")]
    events = send(client, chat_id, "@researcher find it, then @coder save it")
    assert started(events) == ["researcher", "coder"]
    assert [e["status"] for e in of(events, "bot_turn_end")] == ["done", "done"]
    assert of(events, "group_plan")[0]["queue"] == [
        {"author": "researcher", "reason": "mention", "hop": 0},
        {"author": "coder", "reason": "mention", "hop": 0}]
    # Every streamed event of a bot's turn carries its author.
    assert {e["author"] for e in of(events, "text")} == {"researcher", "coder"}
    [done] = of(events, "done")
    assert [turn["author"] for turn in done["bot_turns"]] == ["researcher", "coder"]
    # The coder's model sees the user and the researcher's reply, labelled.
    coder_view = fake.requests[1]["messages"]
    assert coder_view[0]["role"] == "system" and "You are the Coder." in coder_view[0]["content"]
    assert "@researcher (Researcher)" in coder_view[0]["content"]
    assert "@you: @researcher find it" in last_user(fake.requests[1])
    assert "@researcher: Three sources found." in last_user(fake.requests[1])
    # The user's message is stored once, both replies under their authors.
    rows = app.state.store.get_messages(chat_id, include_message_meta=True)
    assert [(row["role"], row.get("author")) for row in rows] == [
        ("user", None), ("assistant", "researcher"), ("assistant", "coder")]


def test_without_a_mention_the_lead_answers_and_can_hand_off(crew):
    client, _, fake = crew
    chat_id = group(client, preset="ask-one", group={"answer": {"lead": "researcher"}})
    fake.scripts[:] = [text("Found it. @coder please save it."), text("Saved.")]
    events = send(client, chat_id, "find it")
    assert started(events) == ["researcher", "coder"]
    starts = of(events, "bot_turn_start")
    assert (starts[1]["reason"], starts[1]["hop"], starts[1]["triggered_by"]) == (
        "handoff", 1, "researcher")


def test_panel_bots_answer_without_seeing_each_other(crew):
    client, _, fake = crew
    chat_id = group(client, preset="panel")
    fake.scripts[:] = [text("Yes."), text("No.")]
    events = send(client, chat_id, "Is it good?")
    assert started(events) == ["researcher", "coder"]
    assert "@researcher: Yes." not in json.dumps(fake.requests[1]["messages"])


def test_mentioned_mode_without_a_mention_stores_the_message_and_asks_nobody(crew):
    client, app, fake = crew
    chat_id = group(client, group={"answer": {"mode": "mentioned"}})
    events = send(client, chat_id, "just a note")
    assert started(events) == [] and fake.requests == []
    assert of(events, "notice")[0]["kind"] == "group_nobody"
    assert of(events, "done")[0]["bot_turns"] == []
    rows = app.state.store.get_messages(chat_id)
    assert [(row["role"], row["content"]) for row in rows] == [("user", "just a note")]


def test_an_answer_override_is_used_for_one_message(crew):
    client, _, fake = crew
    chat_id = group(client, preset="ask-one", group={"answer": {"lead": "researcher"}})
    fake.scripts[:] = [text("A."), text("B."), text("C.")]
    events = send(client, chat_id, "everyone?", answer={"mode": "everyone"})
    assert started(events) == ["researcher", "coder"]
    assert of(events, "group_plan")[0]["override"] is True
    assert started(send(client, chat_id, "and now?")) == ["researcher"]


def test_an_invalid_override_or_one_outside_a_group_is_refused(crew):
    client, _, _ = crew
    chat_id = group(client)
    bad = client.post(f"/chats/{chat_id}/messages", json={"text": "x", "answer": {"mode": "loud"}})
    assert bad.status_code == 422
    plain = client.post("/chats", json={}).json()["id"]
    outside = client.post(f"/chats/{plain}/messages", json={"text": "x", "answer": {"mode": "everyone"}})
    assert outside.status_code == 422


def test_ping_pong_between_two_bots_is_stopped(crew):
    client, _, fake = crew
    chat_id = group(client, preset="ask-one", group={
        "answer": {"lead": "researcher"}, "replies": {"max_hops": 8, "ping_pong": 2}})
    fake.scripts[:] = [text(f"Over to you @{'coder' if i % 2 == 0 else 'researcher'} ({i})")
                       for i in range(10)]
    events = send(client, chat_id, "go")
    assert started(events) == ["researcher", "coder", "researcher"]
    [limit] = of(events, "group_limit")
    assert (limit["limit"], limit["max"], limit["dropped"]) == ("ping_pong", 2, ["coder"])


def test_the_hop_limit_ends_a_chain_of_hand_offs(crew):
    client, _, fake = crew
    chat_id = group(client, members=("researcher", "coder", "critic"), preset="ask-one",
                    group={"answer": {"lead": "researcher"},
                           "replies": {"max_hops": 1, "ping_pong": 8}})
    fake.scripts[:] = [text("@coder next"), text("@critic next"), text("never")]
    events = send(client, chat_id, "go")
    assert started(events) == ["researcher", "coder"]
    [limit] = of(events, "group_limit")
    assert (limit["limit"], limit["dropped"]) == ("max_hops", ["critic"])


def test_bot_turns_limit_caps_the_first_pass_too(crew):
    client, _, fake = crew
    chat_id = group(client, members=("researcher", "coder", "critic"), preset="panel",
                    group={"limits": {"max_bot_turns": 2}})
    fake.scripts[:] = [text("1"), text("2"), text("3")]
    events = send(client, chat_id, "all of you")
    assert started(events) == ["researcher", "coder"]
    assert of(events, "group_limit")[0]["limit"] == "max_bot_turns"


def test_a_repeated_reply_ends_the_chain(crew):
    client, _, fake = crew
    chat_id = group(client, preset="ask-one", group={"answer": {"lead": "researcher"}})
    fake.scripts[:] = [text("Same thing @coder"), text("Same  thing @coder"), text("x")]
    events = send(client, chat_id, "go")
    assert started(events) == ["researcher", "coder"]
    assert of(events, "group_limit")[0]["limit"] == "repeat"


def test_debate_takes_turns_until_everyone_passes(crew):
    client, _, fake = crew
    chat_id = group(client, preset="debate", group={"replies": {"max_hops": 8},
                                                    "limits": {"max_bot_turns": 16}})
    fake.scripts[:] = [text("Tabs."), text("Spaces."), text("Tabs, still."),
                       text("(pass)"), text("(pass)"), text("never")]
    events = send(client, chat_id, "tabs or spaces?")
    assert started(events) == ["researcher", "coder", "researcher", "coder", "researcher"]
    statuses = [e["status"] for e in of(events, "bot_turn_end")]
    assert statuses == ["done", "done", "done", "passed", "passed"]
    assert not of(events, "group_limit")
    # The debate instructions and the (pass) rule reach every bot.
    assert "Group instructions" in fake.requests[0]["messages"][0]["content"]
    assert "(pass)" in fake.requests[0]["messages"][0]["content"]


def test_a_muted_member_never_answers(crew):
    client, _, fake = crew
    chat_id = group(client, preset="panel")
    assert client.patch(f"/chats/{chat_id}/members/coder", json={"muted": True}).status_code == 200
    fake.scripts[:] = [text("Only me.")]
    assert started(send(client, chat_id, "@all hi")) == ["researcher"]


def test_a_deleted_bot_is_skipped_and_the_rest_answer(crew):
    client, _, fake = crew
    chat_id = group(client, preset="panel", members=("researcher", "coder", "critic"))
    (client.app.state.agent.config.home / "bots" / "critic" / "bot.toml").write_text("broken")
    fake.scripts[:] = [text("1"), text("2")]
    assert started(send(client, chat_id, "hi")) == ["researcher", "coder"]


def test_one_bots_error_does_not_end_the_group_turn(crew, monkeypatch):
    client, app, fake = crew
    chat_id = group(client, preset="panel")
    real = app.state.agent.run_turn

    async def flaky(chat_id, text, **kwargs):
        if kwargs["actor"].id == "researcher":
            yield {"type": "error", "message": "model fell over"}
            return
        async for event in real(chat_id, text, **kwargs):
            yield event

    monkeypatch.setattr(app.state.agent, "run_turn", flaky)
    fake.scripts[:] = [text("Still here.")]
    events = send(client, chat_id, "hi")
    ends = of(events, "bot_turn_end")
    assert [(e["author"], e["status"]) for e in ends] == [("researcher", "error"), ("coder", "done")]
    assert ends[0]["message"] == "model fell over"
    assert not of(events, "error")
    rows = app.state.store.get_messages(chat_id)
    assert [row["role"] for row in rows] == ["user", "assistant"]


def test_the_default_assistant_answers_as_a_member_too(crew):
    client, _, fake = crew
    chat_id = group(client, members=("kova", "coder"), preset="panel")
    fake.scripts[:] = [text("Kova here."), text("Coder here.")]
    events = send(client, chat_id, "hello")
    assert started(events) == ["kova", "coder"]
    assert {e["author"] for e in of(events, "text")} == {"kova", "coder"}
