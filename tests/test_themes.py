"""Themes as data: discovery, validation, merging, the routes and the CLI.

A theme file is untrusted input: it arrives from the gallery, a friend, or a folder
the user dropped it into. These tests focus on what has to be *rejected* and on the
one property the engine exists for — a small theme inherits everything it does not
name from the base it extends (#174).
"""

import json

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

import kova.cli as cli
from kova import themes
from kova.client import KovaClient
from kova.config import ensure_token, load_config
from kova.server import create_app


def write_theme(home, **overrides):
    """Write one valid theme file into KOVA_HOME/themes and return its path."""
    payload = {
        "id": "midnight-teal",
        "name": "Midnight Teal",
        "version": "1.0.0",
        "appearance": "dark",
        "extends": "kova-dark",
        "tokens": {"accent": {"default": "#2dd4bf", "on": "#04201c"}},
    }
    payload.update(overrides)
    directory = themes.theme_dir(home)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{payload['id']}.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("KOVA_HOME", str(tmp_path))
    return load_config()


@pytest.fixture
def http(home):
    with TestClient(create_app(home)) as client:
        client.headers["Authorization"] = f"Bearer {ensure_token(home.home)}"
        yield client


@pytest.fixture
def runner(home, http, monkeypatch):
    kova = KovaClient("http://127.0.0.1:7700", ensure_token(home.home), http=http)
    monkeypatch.setattr(cli, "get_client", lambda: kova)
    return CliRunner()


def test_both_built_in_themes_resolve_with_a_terminal_palette():
    for theme_id in themes.BUILTIN:
        resolved = themes.get(theme_id, "/nonexistent")

        assert resolved["id"] == theme_id
        assert resolved["source"] == "built-in"
        assert len(resolved["terminal"]) == 16
        assert resolved["tokens"]["bg.base"].startswith("#")


def test_the_generated_token_data_covers_every_built_in_theme():
    data = themes.builtin_data()

    assert set(themes.BUILTIN) <= set(data)
    for theme_id in themes.BUILTIN:
        assert data[theme_id]["appearance"] in themes.APPEARANCES
        assert data[theme_id]["terminal"]


def test_a_user_theme_is_discovered_and_inherits_the_base(home):
    write_theme(home.home, tokens={"accent": {"default": "#2dd4bf"}})

    entries = {entry["id"]: entry for entry in themes.discover(home.home)}

    assert entries["midnight-teal"]["source"].endswith("midnight-teal.json")
    assert entries["midnight-teal"]["error"] is None
    resolved = themes.get("midnight-teal", home.home)
    assert resolved["tokens"]["accent.default"] == "#2dd4bf"
    assert resolved["tokens"]["bg.base"] == themes.get("kova-dark", home.home)["tokens"]["bg.base"]


def test_a_theme_can_ship_its_own_terminal_palette(home):
    write_theme(home.home, terminal=["#000000"] * 15 + ["#ffffff"])

    assert themes.get("midnight-teal", home.home)["terminal"][15] == "#ffffff"


def test_a_dark_theme_without_extends_uses_the_dark_base(home):
    write_theme(home.home, extends=None, tokens={"accent": {"default": "#2dd4bf"}})

    resolved = themes.get("midnight-teal", home.home)

    assert resolved["extends"] == "kova-dark"
    assert resolved["tokens"]["fg.default"] == themes.get("kova-dark", home.home)["tokens"]["fg.default"]


@pytest.mark.parametrize("overrides, needle", [
    ({"id": "Bad_Id"}, "lowercase"),
    ({"version": "one"}, "1.0.0"),
    ({"appearance": "sepia"}, "appearance"),
    ({"extends": "someone-elses"}, "extends"),
    ({"css": "body{display:none}"}, "Unexpected key"),
    ({"tokens": {"primitive": {"orange": {"500": "#ff0000"}}}}, "semantic group"),
    ({"tokens": {"accent": {"glow": "#ff0000"}}}, "not a token"),
    ({"tokens": {"fg": {"default": "url(https://example.com/x.png)"}}}, "colour"),
    ({"tokens": {"fg": {"default": "#abcde"}}}, "colour"),
    ({"tokens": {"fg": {"default": "#abcdef0"}}}, "colour"),
    ({"terminal": ["#000000"] * 15 + ["#abcde"]}, "colour"),
    ({"terminal": ["#000000"] * 15}, "16 colours"),
])
def test_a_broken_theme_is_refused_with_a_sentence(overrides, needle):
    payload = {"id": "midnight-teal", "name": "Midnight Teal", "version": "1.0.0",
               "appearance": "dark"}
    payload.update(overrides)

    with pytest.raises(themes.ThemeError, match=needle):
        themes.validate(payload)


@pytest.mark.parametrize("value", ["#abc", "#abcd", "#abcdef", "#abcdef01"])
def test_valid_css_hex_lengths_are_accepted(value):
    payload = {"id": "midnight-teal", "name": "Midnight Teal", "version": "1.0.0",
               "appearance": "dark", "tokens": {"fg": {"default": value}},
               "terminal": [value] * 16}
    assert themes.validate(payload) is payload


def test_a_theme_that_is_not_an_object_is_refused():
    with pytest.raises(themes.ThemeError, match="one JSON object"):
        themes.validate([])


def test_the_file_name_must_be_the_id(home):
    path = write_theme(home.home)
    path.rename(path.with_name("other-name.json"))

    entries = {entry["id"]: entry for entry in themes.discover(home.home)}

    assert "must match the file name" in entries["other-name"]["error"]


def test_a_file_that_is_not_json_is_listed_not_hidden(home):
    directory = themes.theme_dir(home.home)
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "half.json").write_text("{oops", encoding="utf-8")

    entries = {entry["id"]: entry for entry in themes.discover(home.home)}

    assert "not valid JSON" in entries["half"]["error"]


def test_an_unknown_theme_names_where_themes_live(home):
    with pytest.raises(themes.ThemeError, match="kova themes list"):
        themes.get("does-not-exist", home.home)


def test_a_theme_id_cannot_escape_the_theme_folder(home):
    with pytest.raises(themes.ThemeError, match="not a theme id"):
        themes.get("../../etc/passwd", home.home)


def test_the_route_lists_built_ins_then_user_files(http, home):
    write_theme(home.home)

    rows = http.get("/themes").json()

    assert [row["id"] for row in rows] == ["kova-dark", "kova-light", "midnight-teal"]
    assert rows[0]["source"] == "built-in"
    assert rows[2]["error"] is None


def test_the_route_resolves_a_theme_and_404s_for_an_unknown_one(http, home):
    write_theme(home.home, tokens={"accent": {"default": "#2dd4bf"}})

    resolved = http.get("/themes/midnight-teal")
    assert resolved.status_code == 200
    assert resolved.json()["tokens"]["accent.default"] == "#2dd4bf"
    assert http.get("/themes/nope").status_code == 404


def test_the_route_reports_a_broken_file_without_hiding_it(http, home):
    directory = themes.theme_dir(home.home)
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "half.json").write_text("{oops", encoding="utf-8")

    row = [entry for entry in http.get("/themes").json() if entry["id"] == "half"][0]

    assert row["error"] and "not valid JSON" in row["error"]
    assert http.get("/themes/half").status_code == 404


def test_the_cli_lists_themes_and_shows_one(runner, home):
    write_theme(home.home)

    listing = runner.invoke(cli.app, ["themes"])
    assert listing.exit_code == 0, listing.output
    assert "kova-dark" in listing.output
    assert "midnight-teal" in listing.output

    shown = runner.invoke(cli.app, ["themes", "show", "midnight-teal"])
    assert shown.exit_code == 0, shown.output
    assert "accent.default" in shown.output


def test_the_cli_says_what_is_wrong_with_a_broken_file(runner, home):
    directory = themes.theme_dir(home.home)
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "half.json").write_text("{oops", encoding="utf-8")

    result = runner.invoke(cli.app, ["themes", "list"])

    assert result.exit_code == 0, result.output
    assert "not valid JSON" in result.output


def test_the_cli_checks_a_file_before_it_is_installed(runner, home, tmp_path):
    source = tmp_path / "midnight-teal.json"
    source.write_text(json.dumps({
        "id": "midnight-teal", "name": "Midnight Teal", "version": "1.0.0",
        "appearance": "dark", "tokens": {"accent": {"default": "#2dd4bf"}},
    }), encoding="utf-8")

    result = runner.invoke(cli.app, ["themes", "check", str(source)])

    assert result.exit_code == 0, result.output
    assert "is valid" in result.output
    assert "accent.default" in result.output


def test_the_cli_says_why_a_file_is_refused(runner, tmp_path):
    broken = tmp_path / "broken.json"
    broken.write_text(json.dumps({"id": "broken"}), encoding="utf-8")

    result = runner.invoke(cli.app, ["themes", "check", str(broken)])

    assert result.exit_code == 1
    assert "Missing required key" in result.output
