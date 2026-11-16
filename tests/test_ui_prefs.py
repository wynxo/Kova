"""UI preferences: the validated store, its routes, and CLI export/import."""

import json

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

import kova.cli as cli
from kova import ui_prefs
from kova.client import KovaClient
from kova.config import ensure_token, load_config
from kova.server import create_app


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("KOVA_HOME", str(tmp_path))
    return load_config()


@pytest.fixture
def http(home):
    with TestClient(create_app(home)) as client:
        client.headers["Authorization"] = f"Bearer {ensure_token(home.home)}"
        yield client


def _output(result) -> str:
    """CLI output on one line, so a narrow terminal's wrapping cannot hide a match."""
    return " ".join(result.output.split())


@pytest.fixture
def runner(home, http, monkeypatch):
    kova = KovaClient("http://127.0.0.1:7700", ensure_token(home.home), http=http)
    monkeypatch.setattr(cli, "get_client", lambda: kova)
    return CliRunner()


def test_defaults_are_served_and_updates_merge(http):
    assert http.get("/preferences").json() == ui_prefs.DEFAULTS

    saved = http.put("/preferences", json={"theme": "light"})

    assert saved.status_code == 200
    assert saved.json() == {**ui_prefs.DEFAULTS, "theme": "light"}
    assert http.get("/preferences").json()["theme"] == "light"


@pytest.mark.parametrize("patch, needle", [
    ({"density": "cozy"}, "Unknown preference"),
    ({"theme": "sepia"}, "must be one of"),
    ({"sidebar": None}, "must be one of"),
])
def test_a_bad_update_is_refused(http, patch, needle):
    response = http.put("/preferences", json=patch)

    assert response.status_code == 422
    assert needle in response.json()["detail"]


def test_export_writes_a_versioned_document(runner, http, tmp_path):
    http.put("/preferences", json={"theme": "light", "sidebar": "collapsed"})
    target = tmp_path / "exported.json"

    result = runner.invoke(cli.app, ["prefs", "export", str(target)])

    assert result.exit_code == 0, result.output
    payload = json.loads(target.read_text(encoding="utf-8"))
    assert payload["format"] == ui_prefs.PREFS_FORMAT
    assert payload["version"] == ui_prefs.PREFS_VERSION
    assert payload["preferences"] == {**ui_prefs.DEFAULTS, "theme": "light", "sidebar": "collapsed"}


def test_export_then_import_restores_preferences(runner, http, tmp_path):
    http.put("/preferences", json={"theme": "light", "notifications": "on"})
    target = tmp_path / "exported.json"
    assert runner.invoke(cli.app, ["prefs", "export", str(target)]).exit_code == 0
    http.put("/preferences", json={"theme": "dark", "notifications": "off", "sidebar": "collapsed"})

    result = runner.invoke(cli.app, ["prefs", "import", str(target)])

    assert result.exit_code == 0, result.output
    assert http.get("/preferences").json() == {
        "theme": "light", "sidebar": "open", "notifications": "on",
    }


def test_import_rejects_invalid_json(runner, tmp_path):
    target = tmp_path / "broken.json"
    target.write_text("{not json", encoding="utf-8")

    result = runner.invoke(cli.app, ["prefs", "import", str(target)])

    assert result.exit_code == 1
    assert "not valid JSON" in _output(result)


def test_import_rejects_another_document(runner, tmp_path):
    target = tmp_path / "other.json"
    target.write_text(json.dumps({"format": "something-else", "version": 1,
                                  "preferences": {}}), encoding="utf-8")

    result = runner.invoke(cli.app, ["prefs", "import", str(target)])

    assert result.exit_code == 1
    assert "Not a Kova UI preferences file" in _output(result)


def test_import_rejects_an_unknown_version(runner, tmp_path):
    target = tmp_path / "future.json"
    target.write_text(json.dumps({"format": ui_prefs.PREFS_FORMAT, "version": 99,
                                  "preferences": {}}), encoding="utf-8")

    result = runner.invoke(cli.app, ["prefs", "import", str(target)])

    assert result.exit_code == 1
    assert "Unsupported preferences version" in _output(result)


def test_import_refuses_a_bad_value_without_changing_anything(runner, http, tmp_path):
    http.put("/preferences", json={"theme": "light"})
    target = tmp_path / "bad-value.json"
    target.write_text(json.dumps({**ui_prefs.document({}), "preferences": {"theme": "sepia"}}),
                      encoding="utf-8")

    result = runner.invoke(cli.app, ["prefs", "import", str(target)])

    assert result.exit_code == 1
    assert "must be one of" in _output(result)
    assert http.get("/preferences").json()["theme"] == "light"


def test_import_refuses_an_unknown_preference(runner, tmp_path):
    target = tmp_path / "unknown.json"
    target.write_text(json.dumps({**ui_prefs.document({}), "preferences": {"font": "large"}}),
                      encoding="utf-8")

    result = runner.invoke(cli.app, ["prefs", "import", str(target)])

    assert result.exit_code == 1
    assert "Unknown preference" in _output(result)


def test_parse_requires_a_preferences_object():
    with pytest.raises(ui_prefs.UiPrefsError, match="no 'preferences' object"):
        ui_prefs.parse({"format": ui_prefs.PREFS_FORMAT, "version": ui_prefs.PREFS_VERSION})


def test_a_corrupt_saved_file_names_itself(home):
    (home.home / "preferences.json").write_text("[]", encoding="utf-8")

    with pytest.raises(ui_prefs.UiPrefsError, match="preferences.json"):
        ui_prefs.Preferences(home.home).read()
