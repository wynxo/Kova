"""Each bot's own memory is a hidden scope, isolated from everything else (1.3).

One bot's notes sit in ``<memory write dir>/.kova-bots/<id>/``, exactly like a
project scope: the global store, another bot and a project never list or search
them, the bot's write tools point only there, and the user's own notes reach a
bot as read-only context only when the bot opts in.
"""

from __future__ import annotations

import os
from datetime import datetime

import pytest
from fastapi.testclient import TestClient

from kova import bots
from kova.actors import Actor
from kova.agent import Agent
from kova.bots import BotDefinition, BotMemory, BotModel, BotPermissions
from kova.config import ensure_token, load_config
from kova.memory import BOT_SCOPE_DIR, Memory, MemoryBudgetError, MemoryStoreError
from kova.server import create_app
from kova.store import Store
from kova.tools import ToolContext, build_tools
from tests.fake_model import FakeModel, text, tool


def make_bot(**overrides) -> BotDefinition:
    values = dict(id="researcher", name="Researcher", handle="researcher",
                  persona="You research.", model=BotModel(name=""),
                  permissions=BotPermissions(tools=("memory_*", "web_*")),
                  memory=BotMemory(own=True, budget=4000, user_memory="read"))
    values.update(overrides)
    return BotDefinition(**values)


@pytest.fixture
def world(tmp_path):
    """A memory root, a store, an agent and one bot chat, all under tmp_path."""
    cfg = load_config(tmp_path)
    cfg.files_path = tmp_path / "Kova Files"
    cfg.files_path.mkdir()
    store = Store(tmp_path / "db.sqlite")
    memory = Memory(cfg.memory_path)
    tools = build_tools(ToolContext(cfg, store, memory, lambda: datetime.now().astimezone()))
    chat = store.create_chat()
    return cfg, store, memory, tools, chat["id"]


async def turn(world, actor, scripts, message="hello"):
    cfg, store, memory, tools, chat_id = world
    fake = FakeModel(scripts)
    agent = Agent(cfg, store, memory, tools, transport=fake.transport)
    events = [event async for event in agent.run_turn(chat_id, message, actor=actor)]
    return events, fake


def prompt_of(fake) -> str:
    return fake.requests[0]["messages"][0]["content"]


# ------------------------------------------------------------------- scopes
def test_a_bot_scope_is_hidden_from_the_global_store_and_the_browser(tmp_path):
    memory = Memory(tmp_path / "notes")
    memory.save("Tea", "The user drinks oolong.")
    bot = memory.bot_scope("researcher")
    bot.save("Sources", "Paper one is the best.")

    assert (bot.scope_id, bot.scope_kind) == ("researcher", "bot")
    assert [note.slug for note in memory.list()] == ["tea"]
    assert memory.get("sources") is None
    assert memory.search("paper") == []
    assert [note.slug for note in bot.list()] == ["sources"]
    assert bot.get("tea") is None
    assert (tmp_path / "notes" / BOT_SCOPE_DIR / "researcher").is_dir()


def test_bot_and_project_scopes_never_see_each_other(tmp_path):
    memory = Memory(tmp_path / "notes")
    researcher = memory.bot_scope("researcher")
    coder = memory.bot_scope("coder")
    project = memory.project_scope("alpha")
    researcher.save("Sources", "alpha research secret")
    coder.save("Scripts", "coder script secret")
    project.save("Plan", "project plan secret")

    assert researcher.get("scripts") is None and researcher.get("plan") is None
    assert coder.get("sources") is None and coder.get("plan") is None
    assert project.get("sources") is None and project.get("scripts") is None
    assert [note.slug for note in researcher.list()] == ["sources"]
    assert memory.get("sources") is None


def test_a_scope_id_is_never_joined_from_raw_input(tmp_path):
    memory = Memory(tmp_path / "notes")
    for bad in ("../escape", "a/b", "", ".hidden", "x" * 65):
        with pytest.raises(MemoryStoreError):
            memory.bot_scope(bad)
        assert not memory.remove_bot_scope(bad)


def test_a_symlinked_bot_scope_folder_is_refused(tmp_path):
    memory = Memory(tmp_path / "notes")
    memory.write_dir.mkdir(parents=True, exist_ok=True)
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    try:
        os.symlink(outside, memory.write_dir / BOT_SCOPE_DIR)
    except (OSError, NotImplementedError):  # pragma: no cover - platform without links
        pytest.skip("this platform cannot create symlinks")
    with pytest.raises(MemoryStoreError, match="link"):
        memory.bot_scope("researcher")


def test_a_bot_scope_has_its_own_budget_and_removal(tmp_path):
    memory = Memory(tmp_path / "notes", budget=8000)
    bot = memory.bot_scope("researcher", budget=20)
    assert bot.budget == 20
    with pytest.raises(MemoryBudgetError):
        bot.add("Long", "this body is far too long for twenty characters")
    bot = memory.bot_scope("researcher", budget=0)
    assert bot.budget == 0, "zero is unlimited, as for every store"
    bot.save("Sources", "x" * 500)
    other = memory.bot_scope("coder")
    other.save("Scripts", "keep me")
    assert memory.remove_bot_scope("researcher") is True
    assert not (tmp_path / "notes" / BOT_SCOPE_DIR / "researcher").exists()
    assert other.list(), "another bot's notes are untouched"
    assert memory.remove_bot_scope("researcher") is False


# -------------------------------------------------------------------- turns
async def test_a_bot_recalls_its_own_notes_and_reads_the_users_read_only(world):
    cfg, store, memory, tools, chat_id = world
    memory.save("Tea", "The user drinks oolong.")
    memory.bot_scope("researcher").save("Sources", "Paper one is the best.")
    actor = Actor.for_chat(store.get_chat(chat_id), make_bot(), files_path=cfg.files_path)
    _, fake = await turn(world, actor, [text("Hi")], message="paper tea")
    prompt = prompt_of(fake)
    assert "What you remember in your own notes" in prompt
    assert "## Sources\nPaper one is the best." in prompt
    assert "What the user has saved (read-only; you cannot change these notes):" in prompt
    assert "## Tea\nThe user drinks oolong." in prompt
    assert "of 4,000 chars used" in prompt


async def test_user_memory_none_gives_the_bot_nothing_of_the_users(world):
    cfg, store, memory, tools, chat_id = world
    memory.save("Tea", "The user drinks oolong.")
    memory.bot_scope("researcher").save("Sources", "Paper one.")
    bot = make_bot(memory=BotMemory(own=True, budget=4000, user_memory="none"))
    actor = Actor.for_chat(store.get_chat(chat_id), bot, files_path=cfg.files_path)
    _, fake = await turn(world, actor, [text("Hi")], message="paper")
    prompt = prompt_of(fake)
    assert "Paper one." in prompt
    assert "oolong" not in prompt
    assert "What the user has saved" not in prompt


async def test_a_bot_writes_only_its_own_scope(world):
    cfg, store, memory, tools, chat_id = world
    actor = Actor.for_chat(store.get_chat(chat_id), make_bot(), files_path=cfg.files_path)
    events, fake = await turn(world, actor, [
        tool("memory_add", {"title": "Sources", "body": "Paper one is the best."}),
        text("Saved."),
    ])
    assert any(event["type"] == "tool_result" and event["ok"] for event in events)
    assert memory.bot_scope("researcher").get("sources").body == "Paper one is the best."
    assert memory.get("sources") is None, "the user's notes are never written"


async def test_a_bot_search_tool_never_returns_the_users_or_another_bots_notes(world):
    cfg, store, memory, tools, chat_id = world
    memory.save("Tea", "oolong secret")
    memory.bot_scope("coder").save("Scripts", "script secret")
    memory.bot_scope("researcher").save("Sources", "paper secret")
    actor = Actor.for_chat(store.get_chat(chat_id), make_bot(), files_path=cfg.files_path)
    _, fake = await turn(world, actor, [tool("memory_search", {"query": "secret"}), text("ok")])
    result = [m for m in fake.requests[1]["messages"] if m["role"] == "tool"][-1]["content"]
    assert "paper secret" in result
    assert "oolong" not in result and "script secret" not in result


async def test_without_its_own_memory_a_bot_gets_no_memory_writes(world):
    cfg, store, memory, tools, chat_id = world
    memory.save("Tea", "oolong")
    bot = make_bot(memory=BotMemory(own=False, budget=4000, user_memory="read"))
    actor = Actor.for_chat(store.get_chat(chat_id), bot, files_path=cfg.files_path)
    _, fake = await turn(world, actor, [text("Hi")], message="tea")
    offered = {entry["function"]["name"] for entry in fake.requests[0].get("tools", [])}
    assert "memory_search" in offered
    assert {"memory_add", "memory_patch", "memory_remove", "memory_save"}.isdisjoint(offered)
    assert "oolong" in prompt_of(fake), "it may still read what the user saved"


async def test_the_opted_in_review_writes_into_the_bots_scope(world):
    """The post-turn review belongs to the bot whose turn it reviewed."""
    from kova.review import MemoryReview, ReviewSettings

    cfg, store, memory, tools, chat_id = world
    cfg.memory_review = True
    review = MemoryReview(store, ReviewSettings(review=True, every=1))
    written = []

    async def fake_review(chat, scope, client):
        written.append(scope.root)
        return []

    review.review = fake_review
    fake = FakeModel([text("Hi")])
    agent = Agent(cfg, store, memory, tools, transport=fake.transport)
    agent.memory_review = review
    actor = Actor.for_chat(store.get_chat(chat_id), make_bot(), files_path=cfg.files_path)
    [event async for event in agent.run_turn(chat_id, "remember this", actor=actor)]
    assert written == [memory.bot_scope("researcher").root]


# -------------------------------------------------------------------- routes
@pytest.fixture
def http(tmp_path):
    cfg = load_config(tmp_path / "home")
    cfg.files_path = tmp_path / "Kova Files"
    cfg.files_path.mkdir()
    app = create_app(cfg)
    with TestClient(app) as client:
        headers = {"Authorization": f"Bearer {ensure_token(cfg.home)}"}
        bot = bots.save_bot(cfg.home, bots.validate(
            {"id": "researcher", "name": "Researcher"},
            bot_folder=cfg.home / "bots" / "researcher",
            kova_home=cfg.home, models=[entry.name for entry in cfg.models]))
        yield client, app, cfg, headers, bot


def test_a_bots_notes_have_their_own_memory_routes(http):
    client, app, cfg, headers, bot = http
    created = client.post("/memory", json={"title": "Sources", "body": "Paper one.",
                                           "bot_id": "researcher"}, headers=headers)
    assert created.status_code == 200, created.text
    assert created.json()["slug"] == "sources"
    listed = client.get("/memory", params={"bot_id": "researcher"}, headers=headers)
    assert [note["slug"] for note in listed.json()] == ["sources"]
    assert client.get("/memory", headers=headers).json() == [], "never in the global list"
    # Search inside the scope only.
    found = client.get("/memory", params={"q": "paper", "bot_id": "researcher"}, headers=headers)
    assert [note["slug"] for note in found.json()] == ["sources"]
    assert client.get("/memory", params={"q": "paper"}, headers=headers).json() == []
    # Edit and delete take the same scope parameter.
    edited = client.patch("/memory/sources", json={"body": "Paper two."},
                          params={"bot_id": "researcher"}, headers=headers)
    assert edited.status_code == 200 and edited.json()["body"] == "Paper two."
    assert client.delete("/memory/sources", params={"bot_id": "researcher"},
                         headers=headers).status_code == 200
    assert client.get("/memory", params={"bot_id": "researcher"}, headers=headers).json() == []
    assert (cfg.memory_path / ".kova-bots" / "researcher").is_dir()


def test_a_project_and_a_bot_are_never_mixed_up(http):
    client, app, cfg, headers, bot = http
    project = client.post("/projects", json={"name": "Alpha", "memory_scope": True},
                          headers=headers).json()
    mixed = client.get("/memory", params={"project_id": project["id"], "bot_id": "researcher"},
                       headers=headers)
    assert mixed.status_code == 422 and "not both" in mixed.json()["detail"]
    assert client.get("/memory", params={"bot_id": "missing"}, headers=headers).status_code == 404
    assert client.post("/memory", json={"title": "x", "body": "y", "project_id": project["id"],
                                        "bot_id": "researcher"}, headers=headers).status_code == 422


def test_a_bot_without_its_own_memory_has_no_routes(http):
    client, app, cfg, headers, bot = http
    off = bots.save_bot(cfg.home, bots.validate(
        {"id": "shy", "name": "Shy", "memory": {"own": False}},
        bot_folder=cfg.home / "bots" / "shy", kova_home=cfg.home,
        models=[entry.name for entry in cfg.models]))
    response = client.get("/memory", params={"bot_id": off.id}, headers=headers)
    assert response.status_code == 422 and "no memory of its own" in response.json()["detail"]

