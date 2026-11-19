"""The theme choice (#174): a shared preference, the manifest colours and the page wiring.

The browser behaviour of ``theme.js`` is exercised in ``tests/web_theme.mjs``; these
tests pin the server half and the contract between the files.
"""

import json
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from kova import themes, ui_prefs
from kova.config import ensure_token, load_config
from kova.server import create_app

ASSETS = Path(__file__).resolve().parents[1] / "src" / "kova" / "web_assets"


def write_theme(home, theme_id="midnight-teal", appearance="dark", **tokens):
    directory = themes.theme_dir(home)
    directory.mkdir(parents=True, exist_ok=True)
    payload = {"id": theme_id, "name": theme_id.replace("-", " ").title(), "version": "1.0.0",
               "appearance": appearance, "tokens": tokens or {"bg": {"base": "#0b1416"}}}
    (directory / f"{theme_id}.json").write_text(json.dumps(payload), encoding="utf-8")


@pytest.fixture
def config(tmp_path, monkeypatch):
    monkeypatch.setenv("KOVA_HOME", str(tmp_path))
    return load_config()


@pytest.fixture
def http(config):
    with TestClient(create_app(config), base_url="http://localhost") as client:
        client.headers["Authorization"] = f"Bearer {ensure_token(config.home)}"
        yield client


# ------------------------------------------------------------------ the preference
def test_system_is_the_default_and_every_fixed_choice_is_accepted(http):
    assert ui_prefs.DEFAULTS["theme"] == "system"
    assert http.get("/preferences").json()["theme"] == "system"
    for choice in ("light", "dark", "system"):
        assert http.put("/preferences", json={"theme": choice}).json()["theme"] == choice


def test_an_installed_theme_id_is_a_valid_choice(http, config):
    write_theme(config.home)
    saved = http.put("/preferences", json={"theme": "midnight-teal"})
    assert saved.status_code == 200
    assert http.get("/preferences").json()["theme"] == "midnight-teal"


def test_a_theme_id_need_not_be_installed_yet():
    # Preferences move between machines (`kova prefs import`, a paired phone):
    # the theme file can arrive later, and the client falls back meanwhile.
    assert ui_prefs.parse(ui_prefs.document({"theme": "not-here-yet"}))["theme"] == "not-here-yet"


@pytest.mark.parametrize("value", ["Midnight", "../etc", "a", "-lead", "x" * 41, 7, None, ""])
def test_a_malformed_theme_id_is_refused(http, value):
    response = http.put("/preferences", json={"theme": value})
    assert response.status_code == 422
    assert "installed theme id" in response.json()["detail"]


def test_open_ids_are_only_for_the_theme(http):
    response = http.put("/preferences", json={"sidebar": "midnight-teal"})
    assert response.status_code == 422
    assert "installed theme id" not in response.json()["detail"]


# --------------------------------------------------------------- the browser chrome
def builtin_base(theme_id):
    return themes.get(theme_id, "unused")["tokens"]["bg.base"]


@pytest.mark.parametrize("choice, base", [
    (None, "kova-dark"), ("system", "kova-dark"), ("dark", "kova-dark"),
    ("light", "kova-light"), ("kova-light", "kova-light"), ("missing-theme", "kova-dark"),
])
def test_chrome_colours_are_the_chosen_bg_base(tmp_path, choice, base):
    expected = builtin_base(base)
    assert themes.chrome_colours(choice, tmp_path) == {
        "theme_color": expected, "background_color": expected}


def test_chrome_colours_follow_an_installed_theme(tmp_path):
    write_theme(tmp_path, bg={"base": "#0b1416"})
    assert themes.chrome_colours("midnight-teal", tmp_path)["theme_color"] == "#0b1416"


def test_a_broken_theme_file_falls_back_to_the_dark_base(tmp_path):
    directory = themes.theme_dir(tmp_path)
    directory.mkdir()
    (directory / "broken.json").write_text("{not json", encoding="utf-8")
    assert themes.chrome_colours("broken", tmp_path)["background_color"] == builtin_base("kova-dark")


def test_the_manifest_follows_the_theme(http, config):
    def manifest():
        return http.get("/app/manifest.webmanifest").json()

    assert manifest()["theme_color"] == builtin_base("kova-dark")
    http.put("/preferences", json={"theme": "light"})
    assert manifest()["theme_color"] == builtin_base("kova-light")
    assert manifest()["background_color"] == builtin_base("kova-light")
    write_theme(config.home, theme_id="paper", appearance="light", bg={"base": "#fbfaf7"})
    http.put("/preferences", json={"theme": "paper"})
    assert manifest()["theme_color"] == "#fbfaf7"


def test_a_broken_preferences_file_does_not_break_the_manifest(http, config):
    (Path(config.home) / "preferences.json").write_text("{nope", encoding="utf-8")
    response = http.get("/app/manifest.webmanifest")
    assert response.status_code == 200
    assert response.json()["theme_color"] == builtin_base("kova-dark")


# -------------------------------------------------------------------- the page wiring
def test_theme_js_runs_before_the_body_and_after_the_tokens():
    page = (ASSETS / "index.html").read_text(encoding="utf-8")
    tag = re.search(r'<script[^>]*src="/app/assets/theme\.js"[^>]*>', page)
    assert tag, "index.html loads theme.js"
    assert "defer" not in tag.group(0) and "async" not in tag.group(0), \
        "theme.js paints <html> before the first frame"
    assert page.index("tokens.css") < tag.start() < page.index("<body"), \
        "it reads tokens.css back, so it comes after it and before the body"
    assert page.index('id="theme-color"') < tag.start(), "the meta tag exists when it runs"


def test_theme_js_is_cached_for_offline_use():
    assert "'/app/assets/theme.js'" in (ASSETS / "sw.js").read_text(encoding="utf-8")


def test_no_script_writes_a_colour_for_the_theme():
    # Colours come from tokens.css or the server's resolved theme, never a literal.
    for name in ("theme.js", "app.js", "settings.js"):
        source = (ASSETS / name).read_text(encoding="utf-8")
        assert not re.search(r"theme-color'\)\.setAttribute\('content',\s*[^)]*#[0-9a-fA-F]{3,8}", source), name
    assert not re.search(r"['\"`]#[0-9a-fA-F]{3,8}['\"`]", (ASSETS / "theme.js").read_text(encoding="utf-8"))


def test_the_client_and_server_agree_on_the_id_rule():
    source = (ASSETS / "theme.js").read_text(encoding="utf-8")
    assert "/^[a-z0-9][a-z0-9-]{1,39}$/" in source
    assert ui_prefs.PATTERNS["theme"].pattern == "^[a-z0-9][a-z0-9-]{1,39}$"


def test_the_client_and_server_agree_on_semantic_token_values():
    source = (ASSETS / "theme.js").read_text(encoding="utf-8")
    colour_groups = re.search(r"const TOKEN = /\^\((.*?)\)", source).group(1).split("|")
    assert set(colour_groups) == set(themes.GROUPS) - {"chat"}
    for name, choices in themes.CHAT_TOKENS.items():
        rule = re.search(rf"'chat\.{re.escape(name)}': \[([^\]]+)\]", source)
        assert rule, f"missing chat token: {name}"
        assert tuple(re.findall(r"'([^']+)'", rule.group(1))) == choices
