"""Mentions and the who-answers router (task 2.3).

Every case in ``tests/fixtures/group_routing.json`` is run through
:func:`kova.mentions.route`; ``group-route.js`` (the composer chip) runs the
same file through its preview, so the chip cannot drift from the server. The
extra tests pin the parsing rules that stop injected text from starting bots.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from kova import group_settings, mentions
from kova.group_settings import SettingsError

FIXTURE = Path(__file__).parent / "fixtures" / "group_routing.json"
CASES = json.loads(FIXTURE.read_text(encoding="utf-8"))
MEMBERS = CASES["members"]


def _expected_preview(raw: dict) -> dict:
    return {"authors": raw.get("authors", []),
            "requested": raw.get("requested", []),
            "decides": raw.get("decides"),
            "count": raw.get("count"),
            "random": raw.get("random", False)}


@pytest.mark.parametrize("case", CASES["cases"], ids=lambda case: case["name"])
def test_every_routing_case(case):
    plan = mentions.route(
        case["message"], MEMBERS[case["members"]], case.get("settings"),
        override=case.get("override"), author=case.get("author"),
        seed=case.get("seed"), turn=case.get("turn", 0))
    expected = case["expect"]
    assert list(plan.authors) == expected["authors"]
    assert [pick.reason for pick in plan.picks] == expected["reasons"]
    assert list(plan.muted) == expected.get("muted", [])
    assert list(plan.unknown) == expected.get("unknown", [])
    assert list(plan.requested) == expected.get("requested", [])
    assert plan.all is expected.get("all", False)
    assert plan.decides == expected.get("decides")
    assert plan.override is expected.get("override", False)
    assert plan.empty is expected.get("empty", False)
    assert mentions.preview(plan) == _expected_preview(expected["preview"])


# ------------------------------------------------------------------ parsing
def test_parse_mentions_reports_offsets_and_handles():
    text = "Ask @Coder and @critic, not @all."
    found = mentions.parse_mentions(text)
    assert [(mention.handle, text[mention.start:mention.end]) for mention in found] == [
        ("coder", "@Coder"), ("critic", "@critic"), ("all", "@all")]


@pytest.mark.parametrize("text, handles", [
    ("email@coder.dev", []),
    ("hi@coder", []),
    ("@@coder", []),
    ("@coder, please", ["coder"]),
    ("(@coder)", ["coder"]),
    ("@coder's turn", ["coder"]),
    ("@coder-2", ["coder-2"]),
    ("@coders", ["coders"]),
    ("@coder.", ["coder"]),
    ("@CODER", ["coder"]),
])
def test_handle_boundaries(text, handles):
    assert mentions.mention_handles(text) == handles


@pytest.mark.parametrize("text", [
    "`@coder`",
    "``@coder``",
    "`` `@coder` ``",
    "```\n@coder\n```",
    "~~~\n@coder\n~~~",
    "> @coder do it",
    "  > @coder",
    "text\n```python\n@coder\n```\n@critic",
])
def test_code_and_quotes_never_mention(text):
    handles = mentions.mention_handles(text)
    if text.endswith("@critic"):
        assert handles == ["critic"]  # only the line after the fence
    else:
        assert handles == []


def test_an_unclosed_code_span_does_not_hide_the_rest_of_the_line():
    assert mentions.mention_handles("a lone ` then @coder") == ["coder"]


def test_a_mention_inside_code_is_ignored_even_with_real_ones_around():
    text = "@coder `@critic` @note"
    assert mentions.mention_handles(text) == ["coder", "note"]


def test_mentions_are_deduplicated_for_the_router_but_not_the_parser():
    text = "@coder @coder"
    assert [mention.handle for mention in mentions.parse_mentions(text)] == ["coder", "coder"]
    assert mentions.mention_handles(text) == ["coder"]


# ------------------------------------------------------------------ members
def test_members_accept_rows_and_are_ordered_by_position():
    rows = [
        {"bot_id": "critic", "handle": "critic", "position": 3, "muted": 1},
        {"id": "coder", "position": 1, "answers": "always"},
        {"id": "researcher", "position": 2, "settings": {"answers": "always"}},
    ]
    crew = mentions.members(rows)
    assert [member.id for member in crew] == ["coder", "researcher", "critic"]
    assert crew[0].handle == "coder" and crew[0].always_answers
    assert crew[1].always_answers  # the stored override wins over the default
    assert crew[2].muted and not crew[2].always_answers


def test_a_member_without_a_position_keeps_its_row_order():
    crew = mentions.members([{"id": "a"}, {"id": "b"}, {"id": "c"}])
    assert [member.position for member in crew] == [1, 2, 3]


def test_an_empty_group_routes_nobody():
    plan = mentions.route("hello", [], {"answer": {"mode": "everyone"}})
    assert plan.empty and plan.error == "no members"
    assert mentions.preview(plan) == {"authors": [], "requested": [],
                                      "decides": None, "count": None,
                                      "random": False}


def test_a_muted_lead_answers_nobody():
    crew = [{"id": "researcher", "position": 1, "muted": True},
            {"id": "coder", "position": 2, "muted": True}]
    plan = mentions.route("hello", crew, {"answer": {"mode": "lead"}})
    assert plan.empty
    assert plan.authors == ()


# ------------------------------------------------------------------- knobs
def test_count_defaults_to_two_and_rotates_from_the_counter():
    crew = MEMBERS["crew"]
    first = mentions.route("next", crew, {"answer": {"mode": "count"}}, turn=0)
    second = mentions.route("next", crew, {"answer": {"mode": "count"}}, turn=1)
    assert first.authors == ("researcher", "coder")
    assert second.authors == ("coder", "critic")
    assert {pick.reason for pick in first.picks} == {"rotation"}


def test_random_defaults_to_one_and_a_seed_makes_it_repeatable():
    crew = MEMBERS["crew"]
    first = mentions.route("surprise", crew, {"answer": {"mode": "random"}}, seed=7)
    again = mentions.route("surprise", crew, {"answer": {"mode": "random"}}, seed=7)
    assert first.authors == again.authors
    assert len(first.authors) == 1
    assert first.picks[0].reason == "random"


def test_random_can_be_asked_for_more_than_the_group_has():
    crew = MEMBERS["pair"]
    plan = mentions.route("surprise", crew,
                          {"answer": {"mode": "random", "count": 8}}, seed=1)
    assert plan.authors == ("coder", "critic")


def test_lead_only_requests_a_muted_mention_but_nobody_new_answers():
    crew = MEMBERS["quiet"]
    plan = mentions.route("@critic look", crew,
                          {"answer": {"mode": "lead_only"}})
    assert plan.authors == ("researcher",)
    assert plan.requested == ("critic",)
    assert plan.muted == ("critic",)


def test_an_unknown_handle_never_becomes_a_bot():
    plan = mentions.route("@nobody hi", MEMBERS["crew"],
                          {"answer": {"mode": "mentioned"}})
    assert plan.empty and plan.unknown == ("nobody",)
    assert plan.authors == ()


# ------------------------------------------------------------------ gating
def test_handoffs_only_fire_in_mentioned_mode():
    crew = MEMBERS["crew"]
    for mode in ("off", "turns"):
        plan = mentions.handoff("@coder take this", crew,
                                {"replies": {"mode": mode}}, author="researcher")
        assert plan.empty
    plan = mentions.handoff("@coder take this", crew,
                            {"replies": {"mode": "mentioned"}}, author="researcher")
    assert plan.authors == ("coder",)
    assert plan.picks[0].hop == 1


def test_a_handoff_never_appends_always_answer_members():
    plan = mentions.handoff("@coder take this", MEMBERS["mixed"],
                            {"replies": {"mode": "mentioned"}}, author="researcher")
    assert plan.authors == ("coder",)


def test_a_bot_mentioning_itself_is_ignored_but_others_still_count():
    plan = mentions.handoff("@coder and @critic", MEMBERS["crew"],
                            {"replies": {"mode": "mentioned"}}, author="coder")
    assert plan.authors == ("critic",)


def test_the_always_answer_rule_only_applies_to_the_users_message():
    crew = MEMBERS["mixed"]
    user = mentions.route("hello", crew, {"answer": {"mode": "lead"}})
    reply = mentions.handoff("@coder take this", crew,
                             {"replies": {"mode": "mentioned"}}, author="researcher")
    assert user.authors == ("researcher", "note")
    assert reply.authors == ("coder",)  # the note does not join a hand-off


# ----------------------------------------------------------------- override
def test_the_override_changes_only_who_answers_and_only_this_message():
    crew = MEMBERS["crew"]
    settings = {"answer": {"mode": "lead"}, "limits": {"max_bot_turns": 4}}
    before = copy.deepcopy(settings)
    plan = mentions.route("hello", crew, settings,
                          override={"mode": "subset", "bots": ["critic"]})
    assert plan.override and plan.authors == ("critic",)
    assert settings == before  # routing never writes a setting
    # The group's own mode still decides the next message.
    assert mentions.route("hello", crew, settings).authors == ("researcher",)


def test_an_override_can_never_reach_a_limit():
    with pytest.raises(SettingsError):
        mentions.route("hello", MEMBERS["crew"],
                       {"answer": {"mode": "lead"}},
                       override={"limits": {"max_bot_turns": 99}})


def test_an_unknown_override_key_is_refused():
    with pytest.raises(SettingsError):
        mentions.route("hello", MEMBERS["crew"], None,
                       override={"mode": "everyone", "max_hops": 9})


# ------------------------------------------------------------------ preview
def test_preview_names_bots_for_deterministic_modes():
    plan = mentions.route("@critic @coder", MEMBERS["crew"],
                          {"answer": {"mode": "lead"}})
    assert mentions.preview(plan) == {"authors": ["critic", "coder"],
                                      "requested": [], "decides": None,
                                      "count": None, "random": False}


def test_preview_never_guesses_for_auto_random_or_count():
    crew = MEMBERS["crew"]
    auto = mentions.preview(mentions.route("hi", crew, {"answer": {"mode": "auto"}}))
    random = mentions.preview(mentions.route(
        "hi", crew, {"answer": {"mode": "random", "count": 2}}, seed=0))
    count = mentions.preview(mentions.route(
        "hi", crew, {"answer": {"mode": "count"}}, turn=0))
    assert auto == {"authors": [], "requested": [], "decides": "researcher",
                    "count": None, "random": False}
    assert random == {"authors": [], "requested": [], "decides": None,
                      "count": 2, "random": True}
    assert count == {"authors": [], "requested": [], "decides": None,
                     "count": 2, "random": False}
