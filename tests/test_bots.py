"""Bot definitions: one folder per bot, validated, atomic, never code (1.1).

Every refusal of the design's section 2.2 has a test here, plus the file
behaviour a person editing a file by hand depends on: writes are 0600 and
atomic, a broken file is listed with one line, and a symlink is never followed.
"""

from __future__ import annotations

import os
import stat

import pytest
import tomli_w

from kova import bots
from kova.bots import (BotAvatar, BotDefinition, BotError, BotModel, BotPermissions,
                       default_folder, delete_bot, list_bots, load_bot, save_bot, validate)

MODELS = ["local", "big"]


def data(**overrides) -> dict:
    """A minimal valid definition, with top-level overrides applied."""
    base = {"id": "researcher", "name": "Researcher"}
    base.update(overrides)
    return base


def write(tmp_path, name, payload: dict | None = None, *, persona: str | None = None) -> "object":
    """Put a hand-written bot folder on disk, the way a person editing files would."""
    folder = tmp_path / "bots" / name
    folder.mkdir(parents=True)
    if payload is not None:
        (folder / "bot.toml").write_text(tomli_w.dumps(payload))
    if persona is not None:
        (folder / "persona.md").write_text(persona)
    return folder


# ------------------------------------------------------------------ defaults
def test_a_minimal_definition_gets_every_documented_default(tmp_path):
    bot = validate(data(), bot_folder=tmp_path / "bots" / "researcher", models=MODELS)
    assert (bot.id, bot.name, bot.handle) == ("researcher", "Researcher", "researcher")
    assert bot.description == ""
    assert bot.schema == bots.SCHEMA
    assert bot.avatar == BotAvatar(colour="info", icon=None, image=None)
    assert bot.model == BotModel(name="", reasoning_effort="", mode="work", web=True)
    assert bot.permissions == BotPermissions(ceiling="manual", tools=bots.DEFAULT_TOOLS,
                                             deny=(), folder=None, inherit_rules=True)
    assert (bot.memory.own, bot.memory.budget, bot.memory.user_memory) == (True, 4_000, "read")
    assert (bot.group.answers, bot.group.max_rounds, bot.group.max_output_tokens) == (
        "normal", 6, 4_000)
    assert bot.persona == "" and bot.warnings == () and bot.path is None


def test_the_default_working_folder_is_under_kova_files():
    assert default_folder("/home/you/Kova Files", "coder").as_posix() == \
        "/home/you/Kova Files/bots/coder"


# ----------------------------------------------------------------- round trip
def test_save_and_load_round_trip_every_field(tmp_path):
    payload = {
        **data(description="Finds sources.", handle="researcher"),
        "avatar": {"colour": "info", "icon": "compass"},
        "model": {"name": "big", "reasoning_effort": "high", "mode": "chat", "web": False},
        "permissions": {"ceiling": "auto", "tools": ["web_*", "read_file"],
                        "deny": ["web_fetch"], "folder": str(tmp_path.parent / "workspace"),
                        "inherit_rules": False},
        "memory": {"own": True, "budget": 8000, "user_memory": "none"},
        "group": {"answers": "always", "max_rounds": 4, "max_output_tokens": 900},
    }
    folder = write(tmp_path, "researcher", payload, persona="You research things.\n")
    loaded = load_bot(folder, models=MODELS, kova_home=tmp_path)
    assert loaded.path == folder
    assert loaded.persona == "You research things.\n"
    assert loaded.avatar == BotAvatar(colour="info", icon="compass", image=None)
    assert loaded.model == BotModel(name="big", reasoning_effort="high", mode="chat", web=False)
    assert loaded.permissions == BotPermissions(ceiling="auto", tools=("web_*", "read_file"),
                                                deny=("web_fetch",),
                                                folder=str(tmp_path.parent / "workspace"),
                                                inherit_rules=False)
    assert loaded.memory == bots.BotMemory(own=True, budget=8000, user_memory="none")
    assert loaded.group == bots.BotGroup(answers="always", max_rounds=4, max_output_tokens=900)

    # Saving the loaded definition writes a canonical file, stable on the next
    # save, and loading it back gives exactly the same definition.
    saved = save_bot(tmp_path, loaded, models=MODELS)
    written = (folder / "bot.toml").read_text()
    save_bot(tmp_path, save_bot(tmp_path, saved, models=MODELS), models=MODELS)
    assert (folder / "bot.toml").read_text() == written
    assert load_bot(folder, models=MODELS, kova_home=tmp_path).to_data() == loaded.to_data()


def test_a_new_bot_is_written_with_private_atomic_files(tmp_path):
    bot = validate(data(), bot_folder=tmp_path / "bots" / "researcher", models=MODELS)
    save_bot(tmp_path, bot, models=MODELS, persona="Be brief.")
    folder = tmp_path / "bots" / "researcher"
    for name in ("bot.toml", "persona.md"):
        mode = stat.S_IMODE((folder / name).stat().st_mode)
        assert mode == 0o600, f"{name} is {oct(mode)}"
    assert (folder / "persona.md").read_text() == "Be brief."
    assert not [path for path in folder.iterdir() if path.name.startswith(".bot-")], \
        "no temporary file may be left behind"
    assert load_bot(folder, models=MODELS, kova_home=tmp_path).persona == "Be brief."


def test_listing_is_sorted_and_carries_each_definition(tmp_path):
    for name, handle in (("coder", "coder"), ("researcher", "researcher")):
        save_bot(tmp_path, validate(data(id=name, name=name.title(), handle=handle),
                                    bot_folder=tmp_path / "bots" / name, models=MODELS),
                 models=MODELS)
    records = list_bots(tmp_path, models=MODELS)
    assert [record.id for record in records] == ["coder", "researcher"]
    assert all(record.ok for record in records)
    assert records[0].bot.summary()["handle"] == "coder"


# ------------------------------------------------------------------ refusals
@pytest.mark.parametrize("bad_id", ["Researcher", "res earcher", "a/b", "../x", "", "x" * 33,
                                    "-leading", "researcher.1"])
def test_an_id_must_be_filename_safe(tmp_path, bad_id):
    with pytest.raises(BotError, match="bot id"):
        validate(data(id=bad_id), models=MODELS)


def test_the_id_must_match_its_folder(tmp_path):
    folder = write(tmp_path, "researcher", data(id="other"))
    with pytest.raises(BotError, match="match its folder"):
        load_bot(folder, models=MODELS)


def test_kova_is_reserved(tmp_path):
    with pytest.raises(BotError, match="reserved"):
        validate(data(id="kova"), models=MODELS)
    with pytest.raises(BotError, match="reserved"):
        validate(data(handle="kova"), models=MODELS)


@pytest.mark.parametrize("handle", ["all", "everyone", "here", "you"])
def test_a_handle_may_not_address_everybody(handle):
    with pytest.raises(BotError, match="reserved"):
        validate(data(handle=handle), models=MODELS)


def test_a_duplicate_handle_is_refused():
    with pytest.raises(BotError, match="already used"):
        validate(data(handle="coder"), handles=["coder"], models=MODELS)


def test_a_saved_bot_cannot_take_another_bots_handle(tmp_path):
    save_bot(tmp_path, validate(data(id="coder", name="Coder", handle="coder"),
                                bot_folder=tmp_path / "bots" / "coder", models=MODELS),
             models=MODELS)
    clash = validate(data(id="researcher", name="Researcher", handle="coder"),
                     bot_folder=tmp_path / "bots" / "researcher", models=MODELS)
    with pytest.raises(BotError, match="already used"):
        save_bot(tmp_path, clash, models=MODELS)
    assert not (tmp_path / "bots" / "researcher").exists()


def test_a_removed_model_is_a_warning_not_an_error(tmp_path):
    bot = validate(data(model={"name": "ghost"}), models=["local"], bot_folder=None)
    assert bot.model.name == "ghost"
    assert bot.warnings and "not configured" in bot.warnings[0]


@pytest.mark.parametrize("payload, message", [
    ({**data(), "extra": 1}, "Unknown key extra"),
    ({**data(), "model": {"temperature": 1}}, "Unknown key model.temperature"),
    ({**data(), "permissions": {"shell": True}}, "Unknown key permissions.shell"),
    ({**data(), "memory": {"unlimited": True}}, "Unknown key memory.unlimited"),
    ({**data(), "group": {"preset": "debate"}}, "Unknown key group.preset"),
])
def test_an_unknown_key_fails_loudly(payload, message):
    with pytest.raises(BotError, match=message.replace(".", r"\.")):
        validate(payload, models=MODELS)


def test_colours_and_icons_are_token_tables():
    validate(data(avatar={"colour": "danger", "icon": "compass"}), models=MODELS)
    with pytest.raises(BotError, match="unknown chat colour"):
        validate(data(avatar={"colour": "#ff0000"}), models=MODELS)
    with pytest.raises(BotError, match="unknown chat icon"):
        validate(data(avatar={"icon": "rocket"}), models=MODELS)


def test_the_avatar_image_is_a_plain_name_inside_the_bot_folder():
    validate(data(avatar={"image": "avatar.png"}), models=MODELS)
    for bad in ("../avatar.png", "sub/avatar.png", ".avatar.png", "C:\\x.png"):
        with pytest.raises(BotError, match="plain file name"):
            validate(data(avatar={"image": bad}), models=MODELS)


def test_the_working_folder_must_stay_clear_of_the_kova_home(tmp_path):
    good = tmp_path.parent / "Kova Files" / "bots" / "researcher"
    assert validate(data(permissions={"folder": str(good)}), kova_home=tmp_path,
                    models=MODELS).permissions.folder == str(good)
    for bad in (tmp_path, tmp_path / "inside", tmp_path.parent):
        with pytest.raises(BotError, match="outside the Kova home"):
            validate(data(permissions={"folder": str(bad)}), kova_home=tmp_path, models=MODELS)
    with pytest.raises(BotError, match="absolute"):
        validate(data(permissions={"folder": "relative/folder"}), kova_home=tmp_path,
                 models=MODELS)


def test_a_symlinked_working_folder_is_refused(tmp_path):
    real = tmp_path.parent / "real-folder"
    real.mkdir(exist_ok=True)
    link = tmp_path.parent / "linked-folder"
    if link.is_symlink():
        link.unlink()
    try:
        os.symlink(real, link)
    except (OSError, NotImplementedError):  # pragma: no cover - platform without links
        pytest.skip("this platform cannot create symlinks")
    with pytest.raises(BotError, match="link"):
        validate(data(permissions={"folder": str(link)}), kova_home=tmp_path, models=MODELS)


def test_a_symlinked_bot_folder_is_refused(tmp_path):
    real = tmp_path.parent / "real-bot"
    real.mkdir(exist_ok=True)
    (real / "bot.toml").write_text(tomli_w.dumps(data()))
    link = tmp_path / "bots" / "researcher"
    link.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.symlink(real, link)
    except (OSError, NotImplementedError):  # pragma: no cover - platform without links
        pytest.skip("this platform cannot create symlinks")
    assert list_bots(tmp_path, models=MODELS) == [
        bots.BotRecord(id="researcher", path=link, error="The bot folder must not be a link.")
    ]
    with pytest.raises(BotError, match="link"):
        load_bot(link, models=MODELS)
    assert not delete_bot(tmp_path, "researcher"), "a link is never followed or removed"
    assert link.is_symlink()


def test_a_value_that_looks_like_a_secret_is_refused(tmp_path):
    key = "sk-proj-abcdefghijklmnopqrstuvwxyz0123456789"
    with pytest.raises(BotError, match="looks like a secret"):
        validate(data(description=key), models=MODELS)
    with pytest.raises(BotError, match="looks like a secret"):
        validate(data(model={"name": key}), models=MODELS)
    folder = write(tmp_path, "researcher", data(), persona=f"Use {key} to answer.")
    with pytest.raises(BotError, match="looks like a secret"):
        load_bot(folder, models=MODELS)


@pytest.mark.parametrize("payload, message", [
    (data(name=""), "Name must be"),
    (data(name="x" * 41), "Name must be"),
    (data(name="two\nlines"), "Name must be"),
    (data(description="x" * 121), "Description"),
    (data(description="one\ntwo"), "Description"),
    (data(schema=2), "version 2"),
    (data(model={"mode": "code"}), "model.mode"),
    (data(model={"reasoning_effort": "max"}), "reasoning_effort"),
    (data(permissions={"ceiling": "root"}), "permissions.ceiling"),
    (data(permissions={"tools": "web_*"}), "permissions.tools"),
    (data(group={"answers": "sometimes"}), "group.answers"),
    (data(group={"max_rounds": 0}), "group.max_rounds"),
    (data(group={"max_rounds": 12}), "group.max_rounds"),
    (data(group={"max_output_tokens": 99}), "group.max_output_tokens"),
    (data(memory={"user_memory": "write"}), "memory.user_memory"),
    (data(memory={"budget": -1}), "memory.budget"),
])
def test_every_documented_range_is_enforced(payload, message):
    with pytest.raises(BotError, match=message.replace(".", r"\.")):
        validate(payload, models=MODELS)


def test_a_persona_is_capped_and_plain_markdown(tmp_path):
    assert validate(data(), persona="# Role\n\nBe brief.", models=MODELS).persona
    with pytest.raises(BotError, match="Persona"):
        validate(data(), persona="x" * (bots.MAX_PERSONA + 1), models=MODELS)
    folder = write(tmp_path, "researcher", data(), persona="x" * (bots.MAX_PERSONA + 1))
    with pytest.raises(BotError, match="Persona"):
        load_bot(folder, models=MODELS)


# ------------------------------------------------------------ broken on disk
def test_a_broken_file_is_listed_with_one_line_and_no_bot(tmp_path):
    write(tmp_path, "broken", None)
    folder = write(tmp_path, "bad-toml")
    (folder / "bot.toml").write_text("id = [unclosed")
    records = list_bots(tmp_path, models=MODELS)
    errors = {record.id: record.error for record in records}
    assert records and all(not record.ok for record in records)
    assert "missing" in errors["broken"]
    assert "not valid TOML" in errors["bad-toml"]
    assert all("\n" not in (record.error or "") for record in records)
    assert bots.get_bot(tmp_path, "broken") is None


def test_two_bots_with_one_handle_are_both_listed_as_broken(tmp_path):
    for name in ("coder", "researcher"):
        (tmp_path / "bots" / name).mkdir(parents=True)
        (tmp_path / "bots" / name / "bot.toml").write_text(
            tomli_w.dumps(data(id=name, name=name.title(), handle="same")))
    records = list_bots(tmp_path, models=MODELS)
    assert [record.id for record in records] == ["coder", "researcher"]
    assert all(not record.ok and "@same" in record.error for record in records)


def test_hand_editing_keeps_the_chosen_folder_name_as_id(tmp_path):
    folder = write(tmp_path, "researcher", {**data(), "description": "hand edited"})
    bot = load_bot(folder, models=MODELS)
    assert bot.id == "researcher" and bot.description == "hand edited"


# ------------------------------------------------------------------- delete
def test_deleting_one_bot_leaves_the_others_and_never_follows_links(tmp_path):
    for name in ("coder", "researcher"):
        save_bot(tmp_path, validate(data(id=name, name=name.title(), handle=name),
                                    bot_folder=tmp_path / "bots" / name, models=MODELS),
                 models=MODELS)
    outside = tmp_path.parent / "not-a-bot"
    outside.mkdir(exist_ok=True)
    (outside / "keep.txt").write_text("keep")
    try:
        os.symlink(outside, tmp_path / "bots" / "coder" / "link")
    except (OSError, NotImplementedError):  # pragma: no cover - platform without links
        pass
    assert delete_bot(tmp_path, "coder")
    assert not (tmp_path / "bots" / "coder").exists()
    assert (tmp_path / "bots" / "researcher" / "bot.toml").is_file()
    assert (outside / "keep.txt").read_text() == "keep"
    assert not delete_bot(tmp_path, "missing")
    assert not delete_bot(tmp_path, "../escape")


def test_a_bot_summary_never_contains_the_persona(tmp_path):
    bot = validate(data(), persona="private role text", models=MODELS)
    summary = bot.summary()
    assert "persona" not in summary
    assert summary["id"] == "researcher"


def test_a_definition_is_frozen():
    bot = BotDefinition(id="researcher", name="Researcher", handle="researcher")
    with pytest.raises(Exception):
        bot.name = "Other"  # type: ignore[misc]
