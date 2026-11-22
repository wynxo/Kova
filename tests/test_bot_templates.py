"""Starter bots: every template is a valid bot with the tools it claims.

A template is data, so these tests are the compiler: each definition must pass
:func:`kova.bots.validate` (the same check the server runs), and every literal
tool pattern must name a tool the current registry really offers. A renamed
tool then fails here instead of quietly vanishing from a starter bot.
"""

import pytest
from fastapi.testclient import TestClient

from kova import bot_templates
from kova.bots import MAX_DESCRIPTION, MAX_PERSONA, bot_root, validate
from kova.config import load_config
from kova.server import create_app
from tests.fake_model import FakeModel


@pytest.fixture(scope="module")
def registry(tmp_path_factory):
    home = tmp_path_factory.mktemp("tools")
    cfg = load_config(home)
    app = create_app(cfg, transport=FakeModel([]).transport)
    with TestClient(app):
        return set(app.state.agent.tools)


def test_the_four_starters_ship():
    assert [template.id for template in bot_templates.list_templates()] == \
        ["researcher", "coder", "writer", "buddy"]
    for template in bot_templates.list_templates():
        assert template.handle == template.id == template.definition()["id"]
        assert template.name and template.name != template.id
        assert 0 < len(template.description) <= MAX_DESCRIPTION
        assert template.description.isprintable() and "\n" not in template.description


def test_every_starter_defines_a_real_bot(tmp_path):
    handles: list[str] = []
    for template in bot_templates.list_templates():
        definition = template.definition()
        folder = bot_root(tmp_path) / template.id
        bot = validate(definition, bot_folder=folder, models=["local"], handles=handles,
                       kova_home=tmp_path, persona=template.persona)
        handles.append(bot.handle)
        assert bot.id == template.id
        assert bot.name == template.name
        assert bot.persona == template.persona
        assert bot.permissions.tools == template.tools
        assert bot.permissions.ceiling == template.ceiling
        assert bot.memory.budget == template.budget
        assert bot.group.max_rounds == template.max_rounds
        assert bot.group.max_output_tokens == template.max_output_tokens
        assert not bot.warnings, bot.warnings


def test_starter_personas_are_real_prompts():
    for template in bot_templates.list_templates():
        persona = template.persona
        assert 300 <= len(persona) <= MAX_PERSONA, template.id
        assert persona.strip() == persona, template.id  # no stray blank edge
        assert "\n" in persona and not persona.startswith("# "), template.id
        # A persona is the role, not a second copy of the name line.
        assert f"You are the {template.name}" not in persona, template.id
        assert "{{" not in persona and "}}" not in persona, template.id


def test_starter_models_default_to_the_active_one():
    for template in bot_templates.list_templates():
        assert template.definition()["model"]["name"] == ""


def test_literal_tool_patterns_name_real_tools(registry):
    """A pattern with no glob must exist; a glob must match at least one tool."""
    for template in bot_templates.list_templates():
        for pattern in template.tools:
            matching = [name for name in registry if _matches(name, pattern)]
            assert matching, f"{template.id} names no tool matching {pattern!r}"
            if not any(char in pattern for char in "*?["):
                assert pattern in registry, f"{template.id} names unknown tool {pattern!r}"


def _matches(name: str, pattern: str) -> bool:
    from fnmatch import fnmatchcase

    return fnmatchcase(name, pattern)


def test_read_only_starters_cannot_touch_the_computer():
    """Buddy is company, not an operator: memory and reading, never writing."""
    buddy = bot_templates.get_template("buddy")
    assert buddy.tools == ("memory_*",)
    assert buddy.ceiling == "manual"
    writer = bot_templates.get_template("writer")
    assert "run_command" not in writer.tools
    assert "write_file" in writer.tools
    # The Coder is the one that runs commands, so it is the one that must ask.
    coder = bot_templates.get_template("coder")
    assert "run_command" in coder.tools
    assert coder.ceiling == "auto"


def test_templates_are_looked_up_by_id_and_alias():
    assert bot_templates.get_template("researcher").id == "researcher"
    assert bot_templates.get_template("Research").id == "researcher"
    assert bot_templates.get_template("coding").id == "coder"
    assert bot_templates.get_template("friend").id == "buddy"
    assert bot_templates.get_template("KoVa") is None


def test_each_call_returns_a_fresh_definition():
    """The CLI edits a template's definition before sending it, so it must not be shared."""
    first = bot_templates.RESEARCHER.definition()
    first["permissions"]["tools"].append("run_command")
    first["model"]["name"] = "lab"
    second = bot_templates.RESEARCHER.definition()
    assert "run_command" not in second["permissions"]["tools"]
    assert second["model"]["name"] == ""
