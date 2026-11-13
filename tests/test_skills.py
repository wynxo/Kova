"""Skills: a description is always visible, a body only loads when it is used."""

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from kova.agent import Agent
from kova.config import load_config
from kova.memory import Memory
from kova.skills import MAX_DESCRIPTION_CHARS, SkillError, Skills
from kova.store import Store
from kova.tools import ToolContext, ToolError, build_tools
from kova.tools import skills as skills_tools
from tests.fake_model import FakeModel, text, tool


def _now() -> datetime:
    return datetime(2026, 11, 12, 10, 0, tzinfo=timezone.utc)


def _ctx(tmp_path) -> ToolContext:
    from kova.store import Store

    return ToolContext(load_config(tmp_path), Store(tmp_path / "kova.db"),
                       Memory(tmp_path / "memory"), _now)


def _tools(ctx: ToolContext) -> dict:
    return {tool_.name: tool_ for tool_ in skills_tools.tools(ctx)}


def test_save_writes_frontmatter_and_reads_back(tmp_path):
    skills = Skills.for_home(tmp_path)
    skill = skills.save("Deploy the site", "Build and publish the site.", "1. Run tests\n2. Push")

    assert skill.slug == "deploy-the-site"
    assert skill.path == tmp_path / "skills" / "Kova" / "deploy-the-site.md"
    written = skill.path.read_text()
    assert written.startswith("---\n") and "description: Build and publish the site." in written
    assert skills.get("deploy-the-site").body == "1. Run tests\n2. Push"
    assert skills.get("Deploy the site").slug == "deploy-the-site"
    assert [s.name for s in skills.list()] == ["Deploy the site"]


def test_save_updates_the_skill_it_wrote_before(tmp_path):
    skills = Skills.for_home(tmp_path)
    skills.save("Deploy the site", "Old summary.", "old steps")
    skills.save("Deploy the site", "New summary.", "new steps")

    listed = skills.list()
    assert len(listed) == 1
    assert listed[0].description == "New summary."
    assert listed[0].body == "new steps"


@pytest.mark.parametrize("name,description,body", [
    ("", "Summary.", "steps"),
    ("Deploy", "", "steps"),
    ("Deploy", "Summary.", ""),
    ("Deploy", "x" * (MAX_DESCRIPTION_CHARS + 1), "steps"),
    ("x" * 61, "Summary.", "steps"),
])
def test_save_refuses_an_unusable_skill(tmp_path, name, description, body):
    with pytest.raises(SkillError):
        Skills.for_home(tmp_path).save(name, description, body)
    assert Skills.for_home(tmp_path).list() == []


def test_prompt_block_carries_descriptions_but_never_bodies(tmp_path):
    skills = Skills.for_home(tmp_path)
    skills.save("Deploy the site", "Build and publish the site.", "BODY-ONLY-MARKER")

    block = skills.prompt_block()
    assert "- Deploy the site: Build and publish the site." in block
    assert "BODY-ONLY-MARKER" not in block
    assert "skill_load" in block  # the model is told how to get the body


def test_prompt_block_still_explains_itself_with_no_skills(tmp_path):
    block = Skills.for_home(tmp_path).prompt_block()
    assert "(none yet)" in block and "skill_write" in block


def test_prompt_block_stays_bounded_and_says_what_it_dropped(tmp_path):
    skills = Skills.for_home(tmp_path)
    for index in range(40):
        skills.save(f"Skill {index}", f"Summary number {index}.", "body")

    block = skills.prompt_block(limit=200)
    assert "more skills are not shown" in block
    assert len(block) < 1000


def test_a_user_skill_is_read_but_never_rewritten_or_deleted(tmp_path):
    root = tmp_path / "skills"
    root.mkdir(parents=True)
    (root / "tidy.md").write_text("# Tidy the inbox\n\nGrab every unread message.\n")

    skills = Skills.for_home(tmp_path)
    skill = skills.get("Tidy the inbox")
    assert skill is not None
    assert skill.description == "Tidy the inbox"  # the first line stands in for front matter
    assert skill.body.strip() == "Grab every unread message."
    assert skills.remove("Tidy the inbox") is False
    assert (root / "tidy.md").exists()


def test_skill_tools_write_load_list_and_remove(tmp_path):
    tools = _tools(_ctx(tmp_path))

    saved = tools["skill_write"].run({"name": "Weekly review", "description": "Review the week.",
                                      "body": "STEP-ONE"})
    assert "Saved skill 'Weekly review'" in saved
    listing = json.loads(tools["skill_list"].run({}))
    assert listing["total"] == 1 and listing["skills"][0]["name"] == "Weekly review"
    assert tools["skill_load"].run({"name": "weekly-review"}) == (
        "# Weekly review\nReview the week.\n\nSTEP-ONE")
    assert "Removed" in tools["skill_remove"].run({"name": "Weekly review"})
    assert Skills.for_home(tmp_path).list() == []


def test_skill_load_and_remove_report_what_exists(tmp_path):
    tools = _tools(_ctx(tmp_path))
    with pytest.raises(ToolError, match="no skills yet"):
        tools["skill_load"].run({"name": "nothing"})

    tools["skill_write"].run({"name": "Tidy notes", "description": "Tidy the notes.", "body": "steps"})
    with pytest.raises(ToolError, match="Tidy notes"):
        tools["skill_load"].run({"name": "missing"})
    with pytest.raises(ToolError, match="Tidy notes"):
        tools["skill_remove"].run({"name": "missing"})


async def _run_turn(agent: Agent, store: Store, tmp_path, text_in: str) -> None:
    chat = store.create_chat(title="t", cwd=str(tmp_path))
    async for _event in agent.run_turn(chat["id"], text_in):
        pass


async def test_the_prompt_shows_the_description_and_not_the_body(tmp_path):
    skills = Skills.for_home(tmp_path)
    skills.save("Deploy the site", "Build and publish the site.", "BODY-ONLY-MARKER")
    cfg = load_config(tmp_path)
    store = Store(tmp_path / "kova.db", default_cwd=str(tmp_path))
    memory = Memory(tmp_path / "memory")
    fake = FakeModel([text("ok")])
    agent = Agent(cfg, store, memory, build_tools(_ctx(tmp_path)), transport=fake.transport, now=_now)

    await _run_turn(agent, store, tmp_path, "hello")

    prompt = fake.requests[0]["messages"][0]["content"]
    assert "- Deploy the site: Build and publish the site." in prompt
    assert "BODY-ONLY-MARKER" not in prompt


async def test_a_skill_written_in_one_turn_is_reusable_in_the_next(tmp_path):
    cfg = load_config(tmp_path)
    store = Store(tmp_path / "kova.db", default_cwd=str(tmp_path))
    memory = Memory(tmp_path / "memory")
    fake = FakeModel([
        tool("skill_write", {"name": "Tidy notes", "description": "Tidy the notes folder.",
                             "body": "MOVE-EVERYTHING-TO-ARCHIVE"}),
        text("Saved it."),
        text("Done."),
    ])
    agent = Agent(cfg, store, memory, build_tools(_ctx(tmp_path)), transport=fake.transport, now=_now)

    await _run_turn(agent, store, tmp_path, "work out how to tidy my notes")
    await _run_turn(agent, store, tmp_path, "tidy my notes again")

    later = fake.requests[-1]["messages"][0]["content"]
    assert "- Tidy notes: Tidy the notes folder." in later
    assert "MOVE-EVERYTHING-TO-ARCHIVE" not in later
    # …and the body is one skill_load away.
    assert "MOVE-EVERYTHING-TO-ARCHIVE" in _tools(_ctx(tmp_path))["skill_load"].run({"name": "Tidy notes"})


async def test_chat_mode_has_no_skills_block_or_tools(tmp_path):
    Skills.for_home(tmp_path).save("Deploy the site", "Build and publish the site.", "BODY-ONLY-MARKER")
    cfg = load_config(tmp_path)
    store = Store(tmp_path / "kova.db", default_cwd=str(tmp_path))
    memory = Memory(tmp_path / "memory")
    fake = FakeModel([text("hi")])
    agent = Agent(cfg, store, memory, build_tools(_ctx(tmp_path)), transport=fake.transport, now=_now)
    chat = store.create_chat(mode="chat", cwd=str(tmp_path))

    async for _event in agent.run_turn(chat["id"], "hello"):
        pass

    body = fake.requests[0]
    prompt = body["messages"][0]["content"]
    assert "Build and publish the site." not in prompt
    assert not any("skill" in schema["function"]["name"] for schema in body.get("tools") or [])
