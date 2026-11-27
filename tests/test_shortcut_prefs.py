"""The shared shortcut map (#200): known actions, sane chords, bounded size."""

import json
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from kova import ui_prefs
from kova.config import ensure_token, load_config
from kova.server import create_app

SHORTCUTS_JS = Path(__file__).resolve().parents[1] / "src" / "kova" / "web_assets" / "shortcuts.js"


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("KOVA_HOME", str(tmp_path))
    return load_config()


@pytest.fixture
def http(home):
    with TestClient(create_app(home)) as client:
        client.headers["Authorization"] = f"Bearer {ensure_token(home.home)}"
        yield client


def test_shortcuts_start_empty_and_round_trip(http):
    assert ui_prefs.DEFAULTS["shortcuts"] == {}
    assert http.get("/preferences").json()["shortcuts"] == {}

    saved = http.put("/preferences", json={"shortcuts": {
        "palette": "Mod+Shift+P",
        "toggle-sidebar": "alt+j",
        "stop-reply": "escape",
    }})

    assert saved.status_code == 200
    assert saved.json()["shortcuts"] == {
        "palette": "mod+shift+p",       # lowercased, modifiers in a fixed order
        "stop-reply": "escape",
        "toggle-sidebar": "alt+j",      # saved sorted, so the file is stable
    }
    assert http.get("/preferences").json()["shortcuts"]["palette"] == "mod+shift+p"


def test_the_whole_map_can_be_replaced_and_cleared(http):
    http.put("/preferences", json={"shortcuts": {"palette": "mod+shift+p"}})
    replaced = http.put("/preferences", json={"shortcuts": {"toggle-theme": "mod+alt+l"}})

    assert replaced.json()["shortcuts"] == {"toggle-theme": "mod+alt+l"}
    assert http.put("/preferences", json={"shortcuts": {}}).json()["shortcuts"] == {}


def test_every_known_action_is_accepted(http):
    every = {action: "mod+alt+shift+k" for action in sorted(ui_prefs.SHORTCUT_ACTIONS)}
    saved = http.put("/preferences", json={"shortcuts": every})

    assert saved.status_code == 200
    assert saved.json()["shortcuts"] == every


def test_the_client_registry_and_the_server_list_are_the_same_actions(http):
    """shortcuts.js is what the reader can rebind; this list is what saves.

    Every action the registry offers must be accepted here, and nothing else:
    an action missing from the list is recorded in the browser and then refused
    by the server, so the key silently never reaches the other devices.
    """
    source = SHORTCUTS_JS.read_text(encoding="utf-8")
    registry = source[source.index("const REGISTRY"):]
    registry = registry[:registry.index("]);")]
    offered = set(re.findall(r"\{id: '([a-z0-9-]+)'", registry))

    assert offered, "shortcuts.js has no registry entries"
    assert offered == set(ui_prefs.SHORTCUT_ACTIONS)
    for action in sorted(offered):
        assert http.put("/preferences", json={"shortcuts": {action: "mod+alt+k"}}).status_code == 200, action


@pytest.mark.parametrize("binding", [
    "hyper+k",          # an unknown modifier
    "mod+",             # no key
    "+k",               # no modifier before a key
    "mod+k+k",          # two keys
    "mod+mod+k",        # a repeated modifier
    "shift+mod+k",      # not the canonical order
    "notakey",          # an unknown name
    "k",                # a bare letter would steal typing
    "shift+k",          # Shift alone is still typing
    " ",                # whitespace is not a key
    "",                 # empty
    "mod+" + "k" * 40,  # longer than a chord can be
])
def test_unsane_bindings_are_refused_through_the_route(http, binding):
    response = http.put("/preferences", json={"shortcuts": {"palette": binding}})

    assert response.status_code == 422
    assert "shortcut" in response.json()["detail"].lower()


def test_unknown_actions_and_wrong_shapes_are_refused(http):
    unknown = http.put("/preferences", json={"shortcuts": {"launch-rockets": "mod+r"}})
    assert unknown.status_code == 422
    assert "launch-rockets" in unknown.json()["detail"]

    assert http.put("/preferences", json={"shortcuts": ["mod+k"]}).status_code == 422
    assert http.put("/preferences", json={"shortcuts": {"palette": 42}}).status_code == 422
    assert http.put("/preferences", json={"shortcuts": None}).status_code == 422


def test_the_map_is_size_bounded(http):
    too_many = {f"action-{index}": "mod+alt+k"
                for index in range(len(ui_prefs.SHORTCUT_ACTIONS) + 1)}

    response = http.put("/preferences", json={"shortcuts": too_many})

    assert response.status_code == 422
    assert "at most" in response.json()["detail"]


def test_a_chord_is_length_limited():
    with pytest.raises(ui_prefs.UiPrefsError):
        ui_prefs._check_binding("mod+" + "f" * ui_prefs.MAX_BINDING_LENGTH)
    assert ui_prefs._check_binding("ctrl+alt+delete") == "ctrl+alt+delete"


def test_an_older_file_without_shortcuts_still_reads(tmp_path):
    path = tmp_path / "preferences.json"
    path.write_text(json.dumps({
        "format": ui_prefs.PREFS_FORMAT,
        "version": ui_prefs.PREFS_VERSION,
        "preferences": {"theme": "light"},
    }))

    values = ui_prefs.Preferences(tmp_path).read()

    assert values["theme"] == "light"
    assert values["shortcuts"] == {}
    assert values["shortcuts"] is not ui_prefs.DEFAULTS["shortcuts"], \
        "a caller cannot mutate the defaults"


def test_export_and_import_carry_the_shortcuts(tmp_path):
    values = {**ui_prefs.DEFAULTS, "shortcuts": {"palette": "mod+shift+p"}}
    path = ui_prefs.write_file(values, tmp_path / "prefs.json")

    assert ui_prefs.read_file(path)["shortcuts"] == {"palette": "mod+shift+p"}

    payload = ui_prefs.document(values)
    assert ui_prefs.parse(payload)["shortcuts"] == {"palette": "mod+shift+p"}

    payload["preferences"]["shortcuts"] = {"launch-rockets": "mod+r"}
    with pytest.raises(ui_prefs.UiPrefsError):
        ui_prefs.parse(payload)
