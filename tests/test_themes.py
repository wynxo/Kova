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
    editable_theme["tokens"]["fg"] = {"default": "#17191c"}
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


# -- gallery: search, update and remove (#177) --------------------------------------


def _bundled(monkeypatch, **versions):
    """Pretend the bundled gallery carries these versions of forest."""
    from kova import theme_routes

    example = themes.load_file(EXAMPLES / "forest.json")
    monkeypatch.setattr(theme_routes, "_gallery_documents",
                        lambda: {"forest": {**example, **versions}})
    return example


def test_gallery_entries_report_version_author_and_updates(http, home, monkeypatch):
    example = _bundled(monkeypatch, version="1.2.0", author="Ada")
    forest = http.get("/themes/gallery").json()[0]
    assert forest["version"] == "1.2.0" and forest["author"] == "Ada"
    assert forest["installed"] is False and forest["update"] is False
    write_theme(home.home, **{**example, "version": "1.1.0"})
    forest = http.get("/themes/gallery").json()[0]
    assert forest["installed_version"] == "1.1.0" and forest["update"] is True
    write_theme(home.home, **{**example, "version": "1.2.0"})
    assert http.get("/themes/gallery").json()[0]["update"] is False
    write_theme(home.home, **{**example, "version": "1.10.0"})  # numbers, not text order
    assert http.get("/themes/gallery").json()[0]["update"] is False


def test_update_replaces_an_older_install_with_the_bundled_copy(http, home, monkeypatch):
    example = _bundled(monkeypatch, version="1.1.0", description="Better greens")
    path = write_theme(home.home, **{**example, "version": "1.0.0", "description": "Old"})
    response = http.post("/themes/gallery/forest/update")
    assert response.status_code == 200, response.text
    assert themes.load_file(path)["description"] == "Better greens"
    assert themes.load_file(path)["version"] == "1.1.0"
    assert list(path.parent.glob(".theme-*")) == [], "no staging file is left behind"
    again = http.post("/themes/gallery/forest/update")
    assert again.status_code == 409 and "up to date" in again.json()["detail"]


def test_update_needs_an_installed_theme_and_a_gallery_id(http, home, monkeypatch):
    _bundled(monkeypatch, version="1.1.0")
    assert http.post("/themes/gallery/forest/update").status_code == 409
    assert http.post("/themes/gallery/nope/update").status_code == 404
    assert not themes.theme_dir(home.home).exists()


def test_update_repairs_a_broken_file_and_will_not_follow_a_link(http, home, monkeypatch, tmp_path):
    _bundled(monkeypatch)
    directory = themes.theme_dir(home.home)
    directory.mkdir()
    (directory / "forest.json").write_text("{not json", encoding="utf-8")
    assert http.post("/themes/gallery/forest/update").status_code == 200
    assert themes.load_file(directory / "forest.json")["id"] == "forest"

    target = tmp_path / "keep.json"
    target.write_text("keep", encoding="utf-8")
    (directory / "forest.json").unlink()
    try:
        (directory / "forest.json").symlink_to(target)
    except OSError:
        pytest.skip("symlinks unavailable")
    assert http.post("/themes/gallery/forest/update").status_code == 409
    assert target.read_text(encoding="utf-8") == "keep"


def test_remove_deletes_only_the_theme_file(http, home):
    path = write_theme(home.home, id="midnight-teal")
    assets = themes.assets_dir(home.home)
    assets.mkdir(parents=True)
    (assets / "sky.png").write_bytes(b"png")
    assert http.delete("/themes/midnight-teal").json() == {"id": "midnight-teal", "removed": True}
    assert not path.exists() and (assets / "sky.png").exists()
    assert all(row["id"] != "midnight-teal" for row in http.get("/themes").json())
    assert http.delete("/themes/midnight-teal").status_code == 404


@pytest.mark.parametrize("ident", ["kova-dark", "kova-light", "system", "dark", "light"])
def test_built_in_themes_cannot_be_removed(http, ident):
    assert http.delete(f"/themes/{ident}").status_code == 409


@pytest.mark.parametrize("ident", ["..%2Fx", "%2e%2e", "UPPER", "a", "x.json", "x%00"])
def test_remove_refuses_names_that_are_not_theme_ids(http, home, ident):
    assert http.delete(f"/themes/{ident}").status_code in (404, 405)


def test_remove_deletes_a_link_not_its_target(http, home, tmp_path):
    directory = themes.theme_dir(home.home)
    directory.mkdir()
    target = tmp_path / "keep.json"
    target.write_text("keep", encoding="utf-8")
    try:
        (directory / "linked.json").symlink_to(target)
    except OSError:
        pytest.skip("symlinks unavailable")
    assert http.delete("/themes/linked").status_code == 200
    assert target.read_text(encoding="utf-8") == "keep"


def test_update_and_remove_need_authentication(http):
    http.headers.pop("Authorization")
    assert http.post("/themes/gallery/forest/update").status_code == 401
    assert http.delete("/themes/forest").status_code == 401


def test_the_cli_searches_the_gallery(runner):
    everything = runner.invoke(cli.app, ["themes", "gallery"])
    assert "Forest" in everything.output and "Paper" in everything.output
    dark = runner.invoke(cli.app, ["themes", "gallery", "forest", "dark"])
    assert "Forest" in dark.output and "Paper" not in dark.output
    light_forest = runner.invoke(cli.app, ["themes", "gallery", "forest", "light"])
    assert light_forest.exit_code == 0 and "No gallery theme matches" in light_forest.output
    assert "Version" in everything.output and "Available" in everything.output


def test_the_cli_updates_and_removes_installed_themes(runner, home, monkeypatch):
    example = _bundled(monkeypatch, version="1.1.0")
    nothing = runner.invoke(cli.app, ["themes", "update"])
    assert nothing.exit_code == 0 and "up to date" in nothing.output
    write_theme(home.home, **{**example, "version": "1.0.0"})
    assert "Update to 1.1.0" in runner.invoke(cli.app, ["themes", "gallery"]).output
    updated = runner.invoke(cli.app, ["themes", "update"])
    assert updated.exit_code == 0 and "Updated forest" in updated.output
    assert themes.load_file(themes.theme_dir(home.home) / "forest.json")["version"] == "1.1.0"
    again = runner.invoke(cli.app, ["themes", "update", "forest"])
    assert again.exit_code == 1 and "up to date" in again.output

    removed = runner.invoke(cli.app, ["themes", "remove", "forest"])
    assert removed.exit_code == 0 and "Removed forest" in removed.output
    assert not (themes.theme_dir(home.home) / "forest.json").exists()
    missing = runner.invoke(cli.app, ["themes", "remove", "forest"])
    assert missing.exit_code == 1
    builtin = runner.invoke(cli.app, ["themes", "remove", "kova-dark"])
    assert builtin.exit_code == 1 and "Built-in" in builtin.output
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


def test_configured_catalog_theme_lifecycle(home, runner):
    import hashlib

    index = home.home / "catalog.json"
    source = home.home / "community.json"
    home.catalog_url = str(index)

    def publish(version, **extra):
        document = dict(id="community", name="Community", appearance="dark",
                        version=version, tokens={}, **extra)
        payload = json.dumps(document).encode()
        source.write_bytes(payload)
        entry = {"kind": "theme", "name": "community", "version": version,
                 "source": {"url": source.name, "sha256": hashlib.sha256(payload).hexdigest()}}
        index.write_text(json.dumps({"extensions": [entry]}))

    def run(*args):
        return runner.invoke(cli.app, ["themes", *args])

    publish("1.0.0")
    assert "Community" in run("gallery", "community").output
    assert "Community" in run("show", "community", "--gallery").output
    assert not (themes.theme_dir(home.home) / "community.json").exists()
    assert run("install", "community").exit_code == 0

    publish("1.1.0", css="not theme data")
    assert run("update", "community").exit_code != 0
    publish("1.1.0")
    source.write_text("{}")
    assert run("update", "community").exit_code != 0
    assert "1.0.0" in run("show", "community").output
    publish("1.1.0")
    assert run("update", "community").exit_code == 0
    assert "1.1.0" in run("show", "community").output

    index.unlink()
    listing = run("gallery")
    assert listing.exit_code == 0 and "Forest" in listing.output
    assert run("remove", "community").exit_code == 0


@pytest.mark.parametrize("keep", [False, True])
def test_cli_try_only_installs_and_selects_after_keep(home, runner, monkeypatch, keep):
    from kova.tui import theme_trial

    config = home.home / "config.toml"
    before = config.read_bytes()
    target = themes.theme_dir(home.home) / "forest.json"

    def preview(data):
        assert data["id"] == "forest"
        assert not target.exists()
        assert config.read_bytes() == before
        return keep

    monkeypatch.setattr(type(cli.console), "is_terminal", property(lambda self: True))
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(theme_trial, "keep_preview", preview)
    cli.themes_try("forest")
    assert target.exists() is keep
    if keep:
        assert load_config().tui_theme == "forest"
    else:
        assert config.read_bytes() == before


@pytest.mark.parametrize(("keys", "keep"), [("k", True), ("r", False), ("", False)])
def test_terminal_preview_keeps_reverts_and_times_out(monkeypatch, keys, keep):
    from prompt_toolkit import Application
    from prompt_toolkit.input import create_pipe_input
    from prompt_toolkit.output import DummyOutput
    from kova.tui import theme_trial

    with create_pipe_input() as pipe:
        pipe.send_text(keys)
        monkeypatch.setattr(theme_trial, "Application",
                            lambda **kwargs: Application(input=pipe, output=DummyOutput(), **kwargs))
        # A key press must beat the timeout even on slow CI runners; only the no-key case waits it out.
        monkeypatch.setattr(theme_trial, "TRY_SECONDS", 10 if keys else 0.2)
        assert theme_trial.keep_preview(themes.get("kova-dark", ".")) is keep


def test_bundled_preview_and_noninteractive_try_do_not_open_catalog(home, http, runner, monkeypatch):
    from kova import catalog

    def unexpected(*args, **kwargs):
        pytest.fail("Catalog must not be opened")

    home.catalog_url = "https://example.com/catalog.json"
    monkeypatch.setattr(catalog, "load", unexpected)
    result = runner.invoke(cli.app, ["themes", "show", "forest", "--gallery"])
    assert result.exit_code == 0 and "Forest" in result.output
    monkeypatch.setattr(cli, "get_client", unexpected)
    result = runner.invoke(cli.app, ["themes", "try", "forest"])
    assert result.exit_code != 0 and "interactive terminal" in result.output


def test_gallery_preview_fetches_only_the_opted_in_theme(home, http, monkeypatch):
    from types import SimpleNamespace
    from kova import catalog

    requested = []
    entries = [catalog.Entry(name=name, kind="theme", version="1.0.0")
               for name in ("unrelated", "community")]

    def load(url, **kwargs):
        requested.append(url)
        return SimpleNamespace(entries=entries)

    def read(entry, index, *, limit):
        requested.append(entry.name)
        return json.dumps(dict(id=entry.name, name="Community", appearance="dark",
                               version="1.0.0", tokens={})).encode()

    monkeypatch.setattr(catalog, "load", load)
    monkeypatch.setattr(catalog, "read_theme", read)
    route = "/themes/gallery/community/preview"
    assert http.get(route).status_code == 404
    assert requested == []

    home.catalog_url = "https://example.com/catalog.json"
    response = http.get(route)
    assert response.status_code == 200
    assert response.json()["document"]["id"] == "community"
    assert requested == [home.catalog_url, "community"]
