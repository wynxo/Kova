"""Optional custom CSS: off by default, bounded, same-origin and recoverable."""

from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from kova import custom_css, ui_prefs
from kova.config import ensure_token, load_config
from kova.server import create_app

SHEET = """
@import url("https://evil.example/theme.css");
:root { --accent-default: #2dd4bf; }
.panel { background: url("https://evil.example/beacon.gif"); }
.mark { content: url("/app/assets/bolt.svg"); }
"""


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("KOVA_HOME", str(tmp_path))
    return load_config()


@pytest.fixture
def http(home):
    app = create_app(home)
    with TestClient(app, base_url="http://127.0.0.1:7700") as client:
        client.headers["Authorization"] = f"Bearer {ensure_token(home.home)}"
        yield client, app


def sign_in(app, client, name: str = "demo") -> None:
    """Same-origin browser session, the credential the sheet route expects."""
    app.state.web_sessions[name] = time.monotonic() + 60
    client.cookies.set("kova_session", name)


def test_custom_css_is_off_by_default(home, http):
    client, app = http
    sign_in(app, client)
    (home.home / custom_css.FILE_NAME).write_text(":root { --radius-md: 2px; }")

    response = client.get("/app/custom.css")

    assert response.status_code == 404
    assert ui_prefs.Preferences(home.home).read()["custom_css"] == "off"


def test_the_sheet_is_served_only_while_the_setting_is_on(home, http):
    client, app = http
    sign_in(app, client)
    (home.home / custom_css.FILE_NAME).write_text(SHEET)
    ui_prefs.Preferences(home.home).update({"custom_css": "on"})

    response = client.get("/app/custom.css")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/css")
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["cache-control"] == "no-store"
    assert "--accent-default" in response.text
    assert "@import" not in response.text
    assert "evil.example" not in response.text
    assert 'url("/app/assets/bolt.svg")' in response.text

    ui_prefs.Preferences(home.home).update({"custom_css": "off"})
    assert client.get("/app/custom.css").status_code == 404


def test_the_sheet_needs_a_browser_session(home):
    app = create_app(home)
    ui_prefs.Preferences(home.home).update({"custom_css": "on"})
    (home.home / custom_css.FILE_NAME).write_text(":root {}")
    with TestClient(app, base_url="http://127.0.0.1:7700") as client:
        assert client.get("/app/custom.css").status_code == 401


def test_the_size_cap_refuses_an_oversized_sheet(home, http):
    client, app = http
    sign_in(app, client)
    (home.home / custom_css.FILE_NAME).write_bytes(b"a" * (custom_css.MAX_BYTES + 1))
    ui_prefs.Preferences(home.home).update({"custom_css": "on"})

    response = client.get("/app/custom.css")

    assert response.status_code == 413
    assert "64 KiB" in response.json()["detail"]


def test_sanitizer_drops_imports_and_foreign_urls() -> None:
    clean = custom_css.sanitize(SHEET)

    assert "@import" not in clean
    assert "https://evil.example" not in clean
    assert "none" in clean
    assert 'url("/app/assets/bolt.svg")' in clean


def test_a_missing_sheet_is_an_empty_one(home) -> None:
    """The switch may be on before the first edit; that is not an error."""
    assert custom_css.load(home.home) == ""


def test_unreadable_encoding_is_refused(home) -> None:
    (home.home / custom_css.FILE_NAME).write_bytes(b"\xff\xfe\x00")
    with pytest.raises(custom_css.CustomCssError, match="UTF-8"):
        custom_css.load(home.home)
