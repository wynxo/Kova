"""The group and member settings schema, presets and resolution (task 2.1).

The schema is the one place every default lives, so these tests pin the
defaults, every range and its ceiling, that storage holds only changed keys,
that ``None`` is a reset, and that presets are named sets of knobs which never
touch approvals, folders, memory or the limits they do not list.
"""

from __future__ import annotations

import copy

import pytest
from fastapi.testclient import TestClient

from kova import group_settings as gs
from kova.bots import BotDefinition, BotGroup
from kova.config import ensure_token, load_config
from kova.group_settings import SettingsError
from kova.server import create_app


def nested(key: str, value) -> dict:
    if "." not in key:
        return {key: value}
    table, leaf = key.split(".", 1)
    return {table: {leaf: value}}


def flatten(partial: dict, prefix: str = "") -> set[str]:
    keys: set[str] = set()
    for key, value in partial.items():
        path = f"{prefix}{key}"
        if isinstance(value, dict):
            keys |= flatten(value, f"{path}.")
        else:
            keys.add(path)
    return keys


def settings_by_key(scope: str = "group") -> dict:
    source = gs.SETTINGS if scope == "group" else gs.MEMBER_SETTINGS
    return {setting.key: setting for setting in source}


# ---------------------------------------------------------------- defaults
def test_defaults_are_the_designs_defaults():
    assert gs.resolve() == {
        "answer": {"mode": "lead", "lead": None, "bots": [], "count": 2,
                   "auto_max": 2, "mentions": "replace"},
        "replies": {"mode": "mentioned", "max_hops": 3, "ping_pong": 2,
                    "allow_pass": True},
        "limits": {"max_bot_turns": 8, "max_tokens": 40_000,
                   "max_cost_usd": None, "max_seconds": 600},
        "approvals": "ask",
        "folder": "shared",
        "folder_path": None,
        "share_work": "replies",
        "see_earlier_replies": True,
        "instructions": "",
    }


def test_defaults_returns_a_fresh_mapping():
    first = gs.defaults()
    first["answer"]["bots"].append("coder")
    first["answer"]["mode"] = "everyone"
    assert gs.defaults()["answer"] == gs.resolve()["answer"]


def test_every_setting_has_copy_and_a_type_its_default_satisfies():
    keys = [setting.key for setting in gs.SETTINGS]
    assert len(keys) == len(set(keys))
    for setting in gs.SETTINGS:
        assert setting.label and setting.help and setting.section in gs.SECTIONS
        assert setting.scope == "group"
        assert setting.type in {"enum", "int", "money", "bool", "text", "path",
                                "bot", "bots"}
        if setting.default is not None and setting.type != "bots":
            assert gs.validate(nested(setting.key, setting.default)) == \
                nested(setting.key, setting.default)
        if setting.type == "bots":
            assert gs.validate(nested(setting.key, list(setting.default))) == \
                nested(setting.key, list(setting.default))


def test_simple_view_is_the_four_controls_the_design_names():
    simple = {setting.key for setting in gs.SETTINGS if setting.simple}
    assert simple == {"answer.mode", "answer.lead", "answer.bots",
                      "answer.count", "replies.mode", "replies.max_hops",
                      "approvals", "folder"}


def test_member_simple_view_is_the_three_controls_the_design_names():
    assert {setting.key for setting in gs.MEMBER_SETTINGS if setting.simple} == {
        "approvals", "answers", "muted"}


def test_retry_and_memory_are_absent_until_their_tasks_land():
    # Design 9: a setting appears only when it works (2.10, 2.12).
    resolved = gs.resolve()
    assert "retry" not in resolved and "memory" not in resolved
    keys = {setting.key for setting in gs.SETTINGS}
    assert "retry" not in keys and "memory" not in keys


# -------------------------------------------------------- ranges and ceilings
RANGES = {
    "answer.count": (1, 8),
    "answer.auto_max": (1, 8),
    "replies.max_hops": (0, 8),
    "replies.ping_pong": (1, 8),
    "limits.max_bot_turns": (1, 16),
    "limits.max_tokens": (1_000, 400_000),
    "limits.max_seconds": (30, 3_600),
}
MEMBER_RANGES = {
    "max_rounds": (1, 11),
    "max_output_tokens": (100, 32_000),
}
CEILINGS = {
    "answer.mode": gs.ANSWER_MODES,
    "answer.bots": 8,
    "answer.count": 8,
    "answer.auto_max": 8,
    "answer.mentions": gs.MENTION_MODES,
    "replies.mode": gs.REPLY_MODES,
    "replies.max_hops": 8,
    "replies.ping_pong": 8,
    "limits.max_bot_turns": 16,
    "limits.max_tokens": 400_000,
    "limits.max_cost_usd": None,
    "limits.max_seconds": 3_600,
    "approvals": gs.APPROVALS,
    "folder": gs.FOLDER_MODES,
    "folder_path": None,
    "share_work": gs.SHARE_WORK,
    "see_earlier_replies": None,
    "replies.allow_pass": None,
    "instructions": 2_000,
}
MEMBER_CEILINGS = {
    "approvals": gs.MEMBER_APPROVALS,
    "answers": gs.MEMBER_ANSWERS,
    "muted": None,
    "max_rounds": 11,
    "max_output_tokens": 32_000,
}


def test_every_group_range_is_the_documented_one():
    for key, (low, high) in RANGES.items():
        setting = settings_by_key()[key]
        assert (setting.minimum, setting.maximum) == (low, high), key


def test_every_member_range_is_the_documented_one():
    for key, (low, high) in MEMBER_RANGES.items():
        setting = settings_by_key("member")[key]
        assert (setting.minimum, setting.maximum) == (low, high), key


def test_every_setting_has_its_hard_ceiling():
    for key, ceiling in CEILINGS.items():
        setting = settings_by_key()[key]
        assert setting.ceiling == ceiling, key
        if setting.type == "int":
            assert setting.ceiling == setting.maximum, key
    for key, ceiling in MEMBER_CEILINGS.items():
        assert settings_by_key("member")[key].ceiling == ceiling, key


@pytest.mark.parametrize("key", RANGES)
def test_both_ends_of_a_range_are_accepted(key):
    low, high = RANGES[key]
    assert gs.validate(nested(key, low)) == nested(key, low)
    assert gs.validate(nested(key, high)) == nested(key, high)


@pytest.mark.parametrize("key", RANGES)
def test_a_value_outside_the_range_is_refused_in_one_line(key):
    low, high = RANGES[key]
    for value in (low - 1, high + 1):
        with pytest.raises(SettingsError) as refused:
            gs.validate(nested(key, value))
        assert str(refused.value) == f"{key} must be {low:,}–{high:,}"
        assert "\n" not in str(refused.value)


@pytest.mark.parametrize("key", MEMBER_RANGES)
def test_the_member_ranges_are_enforced(key):
    low, high = MEMBER_RANGES[key]
    assert gs.member_validate({key: low}) == {key: low}
    assert gs.member_validate({key: high}) == {key: high}
    for value in (low - 1, high + 1):
        with pytest.raises(SettingsError):
            gs.member_validate({key: value})


def test_instruction_length_is_capped():
    longest = "x" * 2_000
    assert gs.validate({"instructions": longest}) == {"instructions": longest}
    with pytest.raises(SettingsError) as refused:
        gs.validate({"instructions": longest + "x"})
    assert str(refused.value) == "instructions must be at most 2,000 characters"


def test_a_cost_is_positive_and_finite():
    assert gs.validate({"limits": {"max_cost_usd": 0.25}}) == {
        "limits": {"max_cost_usd": 0.25}}
    assert gs.validate({"limits": {"max_cost_usd": 0}}) == {
        "limits": {"max_cost_usd": 0.0}}
    for bad in (-1, float("inf"), float("nan"), "1", True):
        with pytest.raises(SettingsError):
            gs.validate({"limits": {"max_cost_usd": bad}})


def test_bot_ids_and_lists_are_checked():
    assert gs.validate({"answer": {"lead": "coder", "bots": ["critic", "coder"]}})
    with pytest.raises(SettingsError):
        gs.validate({"answer": {"lead": "Not A Bot"}})
    with pytest.raises(SettingsError):
        gs.validate({"answer": {"bots": ["coder", "Not A Bot"]}})
    with pytest.raises(SettingsError):
        gs.validate({"answer": {"bots": "coder"}})
    with pytest.raises(SettingsError):
        gs.validate({"answer": {"bots": [f"bot{index}" for index in range(9)]}})
    # A repeated bot names one member, not two.
    assert gs.validate({"answer": {"bots": ["coder", "coder"]}}) == {
        "answer": {"bots": ["coder"]}}


def test_an_empty_path_is_refused():
    with pytest.raises(SettingsError):
        gs.validate({"folder_path": "  "})
    assert gs.validate({"folder_path": "~/Kova Files/groups/crew"}) == {
        "folder_path": "~/Kova Files/groups/crew"}


@pytest.mark.parametrize("payload", [
    {"answer": {"mode": 3}},
    {"answer": {"mode": "nope"}},
    {"answer": {"count": True}},
    {"answer": {"count": 2.5}},
    {"answer": {"count": "2"}},
    {"answer": {"bots": 3}},
    {"replies": {"mode": "sometimes"}},
    {"replies": {"allow_pass": "yes"}},
    {"limits": {"max_seconds": 1.5}},
    {"approvals": "always"},
    {"folder": "everywhere"},
    {"share_work": "mind"},
    {"see_earlier_replies": 1},
])
def test_types_and_values_are_refused_with_a_one_line_reason(payload):
    with pytest.raises(SettingsError) as refused:
        gs.validate(payload)
    assert str(refused.value) and "\n" not in str(refused.value)


# ------------------------------------------------------------ storage shape
def test_only_the_changed_keys_are_stored():
    assert gs.validate({"answer": {"mode": "everyone"}}) == {
        "answer": {"mode": "everyone"}}
    assert gs.validate({"answer": {"mode": "everyone", "count": 3},
                        "approvals": "once"}) == {
        "answer": {"mode": "everyone", "count": 3}, "approvals": "once"}
    assert gs.validate({}) == {}
    assert gs.validate(None) == {}


def test_flat_dotted_keys_are_the_same_settings_as_nested_ones():
    assert gs.validate({"answer.count": 3}) == {"answer": {"count": 3}}
    assert gs.validate({"answer.mode": "everyone", "approvals": "deny"}) == {
        "answer": {"mode": "everyone"}, "approvals": "deny"}


def test_an_unknown_key_is_refused_so_a_typo_never_silently_does_nothing():
    for payload in ({"answer": {"mdoe": "lead"}}, {"nonsense": 1},
                    {"answer.mode.extra": "x"}, {"answer": "everyone"}):
        with pytest.raises(SettingsError):
            gs.validate(payload)


def test_null_resets_a_key_and_leaves_its_siblings_alone():
    assert gs.validate({"answer": {"mode": None}}) == {}
    assert gs.validate({"answer": {"mode": None, "count": 3}}) == {
        "answer": {"count": 3}}
    assert gs.validate({"approvals": None, "folder": "own"}) == {
        "folder": "own"}
    # Resetting a key resolves back to the default.
    assert gs.resolve({"answer": {"mode": "everyone", "count": None}}) == \
        gs.resolve({"answer": {"mode": "everyone"}})


def test_resolve_fills_defaults_and_keeps_the_stored_values():
    resolved = gs.resolve({"answer": {"mode": "subset", "bots": ["coder"]},
                           "replies": {"max_hops": 1}})
    assert resolved["answer"]["mode"] == "subset"
    assert resolved["answer"]["bots"] == ["coder"]
    assert resolved["answer"]["count"] == 2
    assert resolved["replies"]["max_hops"] == 1
    assert resolved["replies"]["mode"] == "mentioned"
    assert resolved["limits"]["max_seconds"] == 600


def test_a_resolved_mapping_round_trips_through_storage():
    for payload in ({"answer": {"mode": "random"}},
                    {"answer": {"mode": "everyone"}, "approvals": "deny"},
                    {"replies": {"mode": "turns", "max_hops": 7},
                     "limits": {"max_tokens": 1_000}}):
        first = gs.resolve(payload)
        assert gs.resolve(first) == first
        assert gs.resolve(gs.changed(first)) == first


def test_changed_drops_values_that_are_the_default():
    assert gs.changed(gs.resolve()) == {}
    assert gs.changed({"answer": {"mode": "lead"}}) == {}
    assert gs.changed({"answer": {"mode": "everyone", "count": 2}}) == {
        "answer": {"mode": "everyone"}}
    assert gs.changed({"answer": {"mode": "random", "count": 1}}) == {
        "answer": {"mode": "random"}}


def test_random_count_defaults_to_one_and_count_to_two():
    assert gs.resolve({"answer": {"mode": "random"}})["answer"]["count"] == 1
    assert gs.resolve({"answer": {"mode": "count"}})["answer"]["count"] == 2
    assert gs.resolve({"answer": {"mode": "random", "count": 3}})[
        "answer"]["count"] == 3


# ------------------------------------------------------------------ presets
def test_the_four_presets_are_data_in_the_designs_order():
    assert [preset.id for preset in gs.PRESETS] == [
        "ask-one", "panel", "team", "debate"]
    assert [preset.name for preset in gs.PRESETS] == [
        "Ask one", "Panel", "Team with a lead", "Debate"]
    for preset in gs.PRESETS:
        assert preset.help and flatten(preset.values) <= set(gs.PRESET_MANAGED)


def test_each_preset_round_trips_and_is_recognised():
    for preset in gs.PRESETS:
        stored = gs.preset_values(preset.id)
        assert gs.validate(stored) == stored
        assert gs.matching_preset(stored) == preset.id
        assert gs.matching_preset(gs.resolve(stored)) == preset.id


def test_a_group_with_nothing_changed_is_ask_one():
    assert gs.matching_preset({}) == "ask-one"
    assert gs.matching_preset(gs.defaults()) == "ask-one"


@pytest.mark.parametrize("preset_id", [preset.id for preset in gs.PRESETS])
def test_custom_after_any_preset_knob_changes(preset_id):
    base = gs.preset_values(preset_id)
    changes = {
        "answer.mode": "everyone" if preset_id != "panel" else "mentioned",
        "answer.auto_max": 3,
        "replies.mode": "turns" if preset_id != "debate" else "off",
        "replies.max_hops": 1,
        "see_earlier_replies": not gs.resolve(base)["see_earlier_replies"],
        "instructions": "custom instructions",
    }
    for key, value in changes.items():
        changed = copy.deepcopy(base)
        if "." in key:
            table, leaf = key.split(".", 1)
            changed.setdefault(table, {})[leaf] = value
        else:
            changed[key] = value
        if gs.resolve(changed) == gs.resolve(base):
            continue  # the change happened to match the preset; skip it
        assert gs.matching_preset(changed) == gs.CUSTOM, (preset_id, key)


@pytest.mark.parametrize("preset_id", [preset.id for preset in gs.PRESETS])
def test_other_settings_do_not_hide_the_preset(preset_id):
    stored = gs.apply_preset({}, preset_id)
    stored = gs.validate({
        **stored,
        "approvals": "deny",
        "folder": "own",
        "folder_path": "~/Kova Files/groups/crew",
        "share_work": "tools",
        "limits": {"max_tokens": 5_000, "max_seconds": 120},
        "answer": {**stored.get("answer", {}), "lead": "coder", "count": 3},
    })
    assert gs.matching_preset(stored) == preset_id


def test_presets_never_touch_approvals_folders_limits_or_memory():
    untouched = {"approvals": "deny", "folder": "own",
                 "folder_path": "~/Kova Files/groups/crew",
                 "share_work": "tools",
                 "limits": {"max_bot_turns": 4, "max_tokens": 9_000,
                            "max_cost_usd": 0.5, "max_seconds": 120}}
    for preset in gs.PRESETS:
        values = gs.preset_values(preset.id)
        assert "approvals" not in values and "folder" not in values
        assert "folder_path" not in values and "share_work" not in values
        assert "limits" not in values
        assert "memory" not in values and "retry" not in values
        applied = gs.apply_preset(untouched, preset.id)
        assert gs.resolve(applied)["approvals"] == "deny"
        assert gs.resolve(applied)["folder"] == "own"
        assert gs.resolve(applied)["limits"]["max_tokens"] == 9_000


def test_switching_presets_returns_their_own_keys_to_default():
    debate = gs.preset_values("debate")
    assert gs.resolve(debate)["instructions"] == gs.DEBATE_INSTRUCTIONS
    panel = gs.apply_preset(debate, "panel")
    assert gs.resolve(panel)["instructions"] == ""
    assert "instructions" not in panel
    assert gs.matching_preset(panel) == "panel"
    # The panel preset sets these; Ask one leaves them at their defaults.
    assert gs.resolve(panel)["see_earlier_replies"] is False
    ask_one = gs.apply_preset(panel, "ask-one")
    assert gs.resolve(ask_one)["see_earlier_replies"] is True
    assert gs.matching_preset(ask_one) == "ask-one"


def test_the_presets_set_what_the_design_table_says():
    ask_one = gs.resolve(gs.preset_values("ask-one"))
    assert ask_one["answer"]["mode"] == "lead"
    assert ask_one["replies"] == {"mode": "mentioned", "max_hops": 3,
                                  "ping_pong": 2, "allow_pass": True}

    panel = gs.resolve(gs.preset_values("panel"))
    assert panel["answer"]["mode"] == "everyone"
    assert panel["replies"]["mode"] == "off"
    assert panel["see_earlier_replies"] is False

    team = gs.resolve(gs.preset_values("team"))
    assert team["answer"]["mode"] == "auto"
    assert team["answer"]["auto_max"] == 2
    assert team["replies"]["mode"] == "mentioned"
    assert team["replies"]["max_hops"] == 3

    debate = gs.resolve(gs.preset_values("debate"))
    assert debate["answer"]["mode"] == "everyone"
    assert debate["replies"]["mode"] == "turns"
    assert debate["replies"]["max_hops"] == 4
    assert debate["instructions"].startswith(
        "Answer the other participants' points directly.")


def test_an_unknown_preset_is_refused():
    with pytest.raises(SettingsError) as refused:
        gs.preset_values("party")
    assert "ask-one" in str(refused.value)


# ------------------------------------------------------------------ members
def test_member_defaults_follow_the_group_and_the_bot():
    assert gs.member_resolve() == {
        "approvals": "ask", "answers": "normal", "muted": False,
        "max_rounds": 6, "max_output_tokens": 4_000}
    group = {"approvals": "once"}
    bot = BotDefinition(id="coder", name="Coder", handle="coder",
                        group=BotGroup(answers="always", max_rounds=4,
                                       max_output_tokens=8_000))
    assert gs.member_resolve(group, {}, bot) == {
        "approvals": "once", "answers": "always", "muted": False,
        "max_rounds": 4, "max_output_tokens": 8_000}


def test_member_overrides_win_over_the_group_and_the_bot():
    bot = {"group": {"answers": "always", "max_rounds": 4,
                     "max_output_tokens": 8_000}}
    member = {"approvals": "once", "answers": "normal", "muted": True,
              "max_rounds": 3, "max_output_tokens": 1_000}
    assert gs.member_resolve({"approvals": "deny"}, member, bot) == {
        "approvals": "once", "answers": "normal", "muted": True,
        "max_rounds": 3, "max_output_tokens": 1_000}


def test_member_same_follows_the_group_and_the_bot_again():
    bot = {"group": {"answers": "always", "max_rounds": 4,
                     "max_output_tokens": 8_000}}
    resolved = gs.member_resolve({"approvals": "allow"},
                                 {"approvals": "same", "answers": "same"}, bot)
    assert resolved["approvals"] == "allow"
    assert resolved["answers"] == "always"


def test_member_resolve_clamps_a_hand_edited_bot():
    bot = {"group": {"max_rounds": 99, "max_output_tokens": 999_999,
                     "answers": "sometimes"}}
    resolved = gs.member_resolve(None, None, bot)
    assert resolved["max_rounds"] == 11
    assert resolved["max_output_tokens"] == 32_000
    assert resolved["answers"] == "normal"


def test_member_null_resets_an_override():
    assert gs.member_validate({"approvals": None}) == {}
    assert gs.member_validate({"muted": True, "max_rounds": None}) == {
        "muted": True}
    assert gs.member_validate(None) == {}


def test_member_unknown_keys_and_types_are_refused():
    for payload in ({"mdoe": 1}, {"approvals": "nope"}, {"answers": "nope"},
                    {"muted": "no"}, {"max_rounds": 0},
                    {"max_output_tokens": 99}):
        with pytest.raises(SettingsError):
            gs.member_validate(payload)


# ------------------------------------------------------------------- schema
def test_the_schema_payload_describes_every_knob_and_preset():
    payload = gs.schema()
    assert payload["version"] == 1
    assert payload["custom"] == gs.CUSTOM
    assert payload["sections"] == list(gs.SECTIONS)
    assert set(payload["member_sections"]) == set(gs.MEMBER_SECTIONS)
    assert [entry["key"] for entry in payload["settings"]] == [
        setting.key for setting in gs.SETTINGS]
    assert [entry["key"] for entry in payload["member_settings"]] == [
        setting.key for setting in gs.MEMBER_SETTINGS]
    for entry in payload["settings"]:
        assert {"key", "label", "help", "type", "default", "section", "scope",
                "simple", "ceiling"} <= set(entry)
        assert entry["scope"] == "group"
    for entry in payload["member_settings"]:
        assert entry["scope"] == "member"
    assert [preset["id"] for preset in payload["presets"]] == [
        preset.id for preset in gs.PRESETS]
    for entry in payload["presets"]:
        assert entry["values"] == gs.preset_values(entry["id"])


def test_get_group_settings_serves_the_schema(tmp_path):
    config = load_config(tmp_path / "home")
    config.files_path = tmp_path / "Kova Files"
    config.files_path.mkdir()
    app = create_app(config)
    with TestClient(app) as client:
        headers = {"Authorization": f"Bearer {ensure_token(config.home)}"}
        response = client.get("/group-settings", headers=headers)
        assert response.status_code == 200
        payload = response.json()
        assert [entry["key"] for entry in payload["settings"]] == [
            setting.key for setting in gs.SETTINGS]
        assert payload["presets"][0]["id"] == "ask-one"
