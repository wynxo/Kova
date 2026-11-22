"""Actors: the resolved per-turn view of the default assistant or one bot (1.2).

The rules that keep a bot from exceeding what its chat allows live in
``Actor.for_chat`` and nowhere else, so this file tests that one function hard:
the stricter permission, the tool allowlist and the bot's own limits.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from kova.actors import DEFAULT_ACTOR_ID, Actor, stricter
from kova.bots import BotDefinition, BotGroup, BotMemory, BotModel, BotPermissions


def bot(**overrides) -> BotDefinition:
    base = dict(id="researcher", name="Researcher", handle="researcher",
                persona="You research sources.", model=BotModel(name="big"),
                permissions=BotPermissions(ceiling="manual", tools=("web_*", "read_file")),
                memory=BotMemory(own=True, budget=4000),
                group=BotGroup(max_rounds=4, max_output_tokens=1200))
    base.update(overrides)
    return BotDefinition(**base)


# --------------------------------------------------------------- stricter of
@pytest.mark.parametrize("a, b, expected", [
    ("manual", "full", "manual"),
    ("auto", "manual", "manual"),
    ("auto", "full", "auto"),
    ("full", "full", "full"),
    ("auto", "auto", "auto"),
    ("bogus", "auto", "auto"),
    ("bogus", "bogus", "manual"),
])
def test_stricter_permission(a, b, expected):
    assert stricter(a, b) == expected


def test_a_bot_never_exceeds_its_own_ceiling():
    chat = {"permission": "full"}
    assert Actor.for_chat(chat, bot()).permission == "manual"
    assert Actor.for_chat(chat, bot()).ceiling == "manual"


def test_a_bot_never_exceeds_the_chats_ceiling():
    chat = {"permission": "manual"}
    actor = Actor.for_chat(chat, bot(permissions=BotPermissions(ceiling="full")))
    assert actor.permission == "manual"
    assert actor.ceiling == "full", "the bot still declares what it may have elsewhere"


def test_the_stricter_of_auto_and_auto_is_auto():
    chat = {"permission": "auto"}
    actor = Actor.for_chat(chat, bot(permissions=BotPermissions(ceiling="auto")))
    assert actor.permission == "auto"


def test_a_chat_without_a_permission_is_manual():
    assert Actor.for_chat({}, bot()).permission == "manual"
    assert Actor.for_chat({}, None).permission == "manual"


# ----------------------------------------------------------------- the default
def test_the_default_assistant_is_todays_behaviour():
    actor = Actor.for_chat({"permission": "auto"})
    assert actor.id == DEFAULT_ACTOR_ID and actor.is_default
    assert actor.permission == "auto"
    assert actor.folder is None and actor.tools is None
    assert actor.allows("run_command") and actor.allows("anything__at_all")
    assert actor.max_rounds == 11 and actor.max_output_tokens is None


# ------------------------------------------------------------------ allowlist
def test_the_allowlist_uses_globs_and_deny_wins():
    actor = Actor.for_chat({}, bot(permissions=BotPermissions(
        ceiling="manual", tools=("web_*", "memory_*", "read_file"), deny=("memory_remove",))))
    assert actor.allows("web_search") and actor.allows("memory_add") and actor.allows("read_file")
    assert not actor.allows("memory_remove"), "deny is applied after tools"
    assert not actor.allows("write_file") and not actor.allows("run_command")
    assert not actor.allows("github__create_issue")


def test_an_empty_allowlist_offers_nothing_but_none_offers_everything():
    none = Actor.for_chat({}, bot(permissions=BotPermissions(tools=())))
    assert not none.allows("web_search")
    everything = Actor.for_chat({}, bot(permissions=BotPermissions(tools=None)))
    assert everything.allows("run_command")


# ------------------------------------------------------------------ the bot
def test_a_bot_carries_its_name_role_model_and_limits():
    actor = Actor.for_chat({"permission": "auto"}, bot())
    assert (actor.id, actor.name) == ("researcher", "Researcher")
    assert actor.role == "You research sources."
    assert actor.model == "big"
    assert actor.max_rounds == 4 and actor.max_output_tokens == 1200
    assert (actor.memory_own, actor.memory_budget, actor.user_memory) == (True, 4000, "read")


def test_missing_model_and_effort_mean_the_chats_will_be_used_again():
    actor = Actor.for_chat({}, bot(model=BotModel(name="", reasoning_effort="")))
    assert actor.model is None and actor.reasoning_effort is None


def test_the_round_limit_can_never_exceed_the_agents_own_cap():
    actor = Actor.for_chat({}, bot(group=BotGroup(max_rounds=99)))
    assert actor.max_rounds == 11


def test_a_bot_folder_is_resolved_explicitly_or_under_kova_files(tmp_path):
    explicit = tmp_path / "work"
    configured = Actor.for_chat({}, bot(permissions=BotPermissions(folder=str(explicit))))
    assert configured.folder == explicit
    default = Actor.for_chat({}, bot(), files_path=tmp_path / "Kova Files")
    assert default.folder == tmp_path / "Kova Files" / "bots" / "researcher"
    assert Actor.for_chat({}, bot()).folder is None, "no files_path, no guessed folder"


def test_a_home_relative_bot_folder_is_expanded():
    actor = Actor.for_chat({}, bot(permissions=BotPermissions(folder="~/somewhere")))
    assert actor.folder == Path("~/somewhere").expanduser()


def test_with_permission_only_narrows():
    actor = Actor.for_chat({"permission": "full"}, bot(permissions=BotPermissions(ceiling="auto")))
    assert actor.with_permission("manual").permission == "manual"
    assert actor.with_permission("full").permission == "auto", "the bot's ceiling still wins"
