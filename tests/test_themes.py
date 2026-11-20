"""Themes as data: discovery, validation, merging, the routes and the CLI.

A theme file is untrusted input: it arrives from the gallery, a friend, or a folder
the user dropped it into. These tests focus on what has to be *rejected* and on the
one property the engine exists for — a small theme inherits everything it does not
name from the base it extends (#174).
"""

import json
from pathlib import Path

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


EXAMPLES = Path(__file__).resolve().parents[1] / "design" / "themes"


def test_gallery_lists_examples_with_preview_tokens_without_installing(http, home):
    response = http.get("/themes/gallery")
    assert response.status_code == 200, response.text
    entries = {entry["id"]: entry for entry in response.json()}
    assert set(entries) == {path.stem for path in EXAMPLES.glob("*.json")}
    example = themes.load_file(EXAMPLES / "forest.json")
    forest = entries["forest"]
    assert forest["name"] == example["name"]
    assert forest["appearance"] == example["appearance"]
    assert forest["description"] == example["description"]
    assert forest["tokens"]["accent.default"] == example["tokens"]["accent"]["default"]
    assert forest["swatches"] == [forest["tokens"][role] for role in (
        "bg.base", "bg.raised", "fg.default", "accent.default",
    )]
    assert forest["installed"] is False
    assert not themes.theme_dir(home.home).exists()


def test_gallery_installs_the_example_but_never_overwrites_it(http, home):
    example = themes.load_file(EXAMPLES / "forest.json")
    url = "/themes/gallery/forest/install"
    installed = http.post(url)
    assert installed.status_code == 201, installed.text
    path = themes.theme_dir(home.home) / "forest.json"
    assert themes.load_file(path) == example
    assert http.get("/themes/forest").json()["tokens"] == installed.json()["tokens"]
    assert next(row for row in http.get("/themes/gallery").json() if row["id"] == "forest")["installed"]
    edited = {**example, "name": "My Forest"}
    path.write_text(json.dumps(edited), encoding="utf-8")
    assert http.post(url).status_code == 409
    assert themes.load_file(path) == edited


@pytest.mark.parametrize("overrides", [
    {"id": "other-id"}, {"css": "not allowed"}, {"tokens": {"fg": {"default": "#14121c"}}},
])
def test_gallery_revalidates_bundled_documents_before_installing(http, home, monkeypatch, overrides):
    from kova import theme_routes

    example = themes.load_file(EXAMPLES / "forest.json")
    monkeypatch.setattr(theme_routes, "_gallery_documents", lambda: {"forest": {**example, **overrides}})
    assert http.get("/themes/gallery").json() == []
    assert http.post("/themes/gallery/forest/install").status_code == 422
    assert not themes.theme_dir(home.home).exists()


@pytest.mark.parametrize("ident", ["missing", "forest.json", "%2e%2e%2Fforest", "..%5Cforest", "forest%00"])
def test_gallery_only_accepts_exact_bundled_ids(http, home, ident):
    assert http.post(f"/themes/gallery/{ident}/install").status_code == 404
    assert not themes.theme_dir(home.home).exists()


def test_gallery_install_does_not_follow_a_destination_link(http, home, tmp_path):
    directory = themes.theme_dir(home.home)
    directory.mkdir()
    target = tmp_path / "keep.json"
    try:
        (directory / "forest.json").symlink_to(target)
    except OSError:
        pytest.skip("symlinks unavailable")
    assert http.post("/themes/gallery/forest/install").status_code == 409
    assert not target.exists()


def test_gallery_needs_authentication(http):
    http.headers.pop("Authorization")
    assert http.get("/themes/gallery").status_code == 401
    assert http.post("/themes/gallery/forest/install").status_code == 401


def test_the_gallery_does_not_hide_an_existing_theme_named_gallery(http, home, runner):
    write_theme(home.home, id="gallery", name="My Gallery")
    assert isinstance(http.get("/themes/gallery").json(), list)
    assert http.get("/themes/gallery?installed=true").json()["name"] == "My Gallery"
    result = runner.invoke(cli.app, ["themes", "show", "gallery"])
    assert result.exit_code == 0, result.output
    assert "My Gallery" in result.output


def test_the_cli_browses_and_installs_from_the_gallery(runner):
    listing = runner.invoke(cli.app, ["themes", "gallery"])
    assert listing.exit_code == 0, listing.output
    assert "Forest" in listing.output and "forest" in listing.output
    installed = runner.invoke(cli.app, ["themes", "install", "forest"])
    assert installed.exit_code == 0, installed.output
    assert "Installed forest" in installed.output
    duplicate = runner.invoke(cli.app, ["themes", "install", "forest"])
    assert duplicate.exit_code == 1, duplicate.output
    assert "already installed" in duplicate.output
    unsafe_id = runner.invoke(cli.app, ["themes", "install", "../forest"])
    assert unsafe_id.exit_code == 1, unsafe_id.output


@pytest.fixture
def editable_theme():
    return {
        "id": "custom-teal",
        "name": "Custom Teal",
        "version": "1.0.0",
        "appearance": "dark",
        "tokens": {"accent": {"default": "#2dd4bf", "on": "#04201c"}},
    }


def test_validate_and_save_a_theme(http, home, editable_theme):
    path = themes.theme_dir(home.home) / "custom-teal.json"
    preview = http.post("/themes?validate_only=true", json=editable_theme)
    assert preview.status_code == 200, preview.text
    assert preview.json()["tokens"]["accent.default"] == "#2dd4bf"
    assert not path.exists()

    saved = http.post("/themes", json=editable_theme)
    assert saved.status_code == 201, saved.text
    assert json.loads(path.read_text(encoding="utf-8")) == editable_theme
    assert http.get("/themes/custom-teal").json()["tokens"] == preview.json()["tokens"]
    assert any(row["id"] == "custom-teal" for row in http.get("/themes").json())

    duplicate = http.post("/themes", json={**editable_theme, "name": "Other"})
    assert duplicate.status_code == 409
    assert json.loads(path.read_text(encoding="utf-8")) == editable_theme


@pytest.mark.parametrize("overrides", [
    {"id": "../escaped"},
    {"id": "sub/file"},
    {"id": "sub\\file"},
    {"id": "custom-teal\n"},
    {"id": "kova-dark"},
    {"id": "system"},
    {"id": "con"},
    {"extends": []},
    {"tokens": {"bg": {"unknown": "#fff"}}},
    {"tokens": {"bg": {"overlay": "rgb(1,2,3,4,5)"}}},
    {"tokens": {"fg": {"default": "rgb(0,0,0,0,9)"}}},
    {"tokens": {"fg": {"default": "rgb(255255 0 0 / ..%)"}}},
])
def test_save_refuses_invalid_documents_without_writing(http, home, editable_theme, overrides):
    response = http.post("/themes", json={**editable_theme, **overrides})
    assert response.status_code == 422, response.text
    assert not themes.theme_dir(home.home).exists()
    assert not (home.home / "escaped.json").exists()


def test_contrast_failure_names_the_pair_on_preview_and_save(http, home, editable_theme):
    editable_theme["tokens"]["fg"] = {"default": "#14121c"}
    for suffix in ("?validate_only=true", ""):
        response = http.post(f"/themes{suffix}", json=editable_theme)
        assert response.status_code == 422
        assert any("fg.default on bg.base = 1.00:1, below 4.5:1" in line
                   for line in response.json()["detail"])
    assert not themes.theme_dir(home.home).exists()


def test_save_refuses_bad_json_and_limits_streamed_bytes(http, home):
    assert http.post("/themes", content=b"{]").status_code == 422
    assert http.post("/themes", content=b"\xff").status_code == 422
    chunks = (b" " * 4096 for _ in range(32))
    assert http.post("/themes", content=chunks).status_code == 413
    assert not themes.theme_dir(home.home).exists()


def test_save_cannot_write_through_a_symlink(http, home, editable_theme, tmp_path):
    directory = themes.theme_dir(home.home)
    outside = tmp_path / "other"
    outside.mkdir()
    try:
        directory.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("symlinks unavailable")
    assert http.post("/themes", json=editable_theme).status_code == 409
    assert not list(outside.iterdir())

    directory.unlink()
    directory.mkdir()
    target = outside / "keep.json"
    (directory / "custom-teal.json").symlink_to(target)
    assert http.post("/themes", json=editable_theme).status_code == 409
    assert not target.exists()


def test_save_requires_authentication(http, editable_theme):
    http.headers.pop("Authorization")
    assert http.post("/themes", json=editable_theme).status_code == 401


def test_theme_import_preserves_opaque_hex_and_modern_rgb(http, home, editable_theme):
    editable_theme["tokens"].update({
        "fg": {"default": "#ffff"},
        "bg": {"overlay": "rgb(13 12 19 / 72%)"},
    })
    editable_theme["description"] = "Palette from a different device ☕"
    assert http.post("/themes", json=editable_theme).status_code == 201
    installed = themes.theme_dir(home.home) / "custom-teal.json"
    assert json.loads(installed.read_text(encoding="utf-8")) == editable_theme


def test_theme_at_the_upload_limit_can_be_reimported(http, home, editable_theme):
    editable_theme["$schema"] = ""
    size = len(json.dumps(editable_theme, separators=(",", ":")))
    editable_theme["$schema"] = "x" * (32 * 1024 - size)
    document = json.dumps(editable_theme, separators=(",", ":")).encode()
    assert len(document) == 32 * 1024
    assert http.post("/themes", content=document).status_code == 201
    installed = (themes.theme_dir(home.home) / "custom-teal.json").read_bytes()
    assert http.post("/themes?validate_only=true", content=installed).status_code == 200


def test_chat_view_defaults_are_validated_semantic_theme_tokens(home):
    write_theme(home.home, tokens={"chat": {
        "width": "wide", "message-style": "bubbles", "avatars": "off",
        "zen": "on", "bubble-shape": "sharp",
    }})
    tokens = themes.get("midnight-teal", home.home)["tokens"]
    assert tokens["chat.width"] == "wide"
    assert tokens["chat.message-style"] == "bubbles"
    assert tokens["chat.avatars"] == "off"
    assert tokens["chat.zen"] == "on"
    assert tokens["chat.bubble-shape"] == "sharp"
    path = write_theme(home.home, tokens={"chat": {"width": "unknown"}})
    with pytest.raises(themes.ThemeError, match="chat.width"):
        themes.load_file(path)
