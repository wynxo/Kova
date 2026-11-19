"""Theme backgrounds (#206): schema, the assets route, the preference and CSS fallbacks.

A background is theme data, never a stylesheet: a gradient of semantic colours
or one image file name. The image route is authenticated and confined to
``KOVA_HOME/themes/assets``; the browser keeps the measured panel surfaces
opaque enough that the contrast gate still describes what is on screen.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from kova import themes, ui_prefs
from kova.config import ensure_token, load_config
from kova.server import create_app

ROOT = Path(__file__).resolve().parents[1]
VALIDATOR = ROOT / "scripts" / "validate_theme.py"
ASSETS = ROOT / "src" / "kova" / "web_assets"


def _load_validator():
    spec = importlib.util.spec_from_file_location("kova_validate_theme_backgrounds", VALIDATOR)
    assert spec and spec.loader, f"cannot import {VALIDATOR}"
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


validator = _load_validator()
SCHEMA = validator.load_schema()


def base_theme(**overrides):
    payload = {
        "id": "backdrop",
        "name": "Backdrop",
        "version": "1.0.0",
        "appearance": "dark",
        "extends": "kova-dark",
    }
    payload.update(overrides)
    return payload


def schema_errors(background):
    return validator.validate(base_theme(background=background), SCHEMA, SCHEMA)


@pytest.fixture
def config(tmp_path, monkeypatch):
    monkeypatch.setenv("KOVA_HOME", str(tmp_path))
    return load_config()


@pytest.fixture
def http(config):
    with TestClient(create_app(config), base_url="http://localhost") as client:
        client.headers["Authorization"] = f"Bearer {ensure_token(config.home)}"
        yield client


def write_asset(config, name="backdrop.png", data=b"\x89PNG\r\n\x1a\n"):
    directory = themes.assets_dir(config.home)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_bytes(data)
    return path


# --------------------------------------------------------------------- schema
def test_a_gradient_and_an_image_are_both_valid():
    assert schema_errors({
        "kind": "gradient", "stops": ["#0b1416", "rgb(23, 58, 59)"], "angle": 200,
    }) == []
    assert schema_errors({"kind": "image", "file": "night-sky.png", "dim": 0.55, "blur": 12}) == []


@pytest.mark.parametrize("background", [
    {"kind": "gradient", "stops": ["#0b1416"]},
    {"kind": "gradient", "stops": ["#0b1416"] * 5},
    {"kind": "gradient", "stops": ["#0b1416", "rebeccapurple"]},
    {"kind": "gradient", "stops": ["#0b1416", "#173a3b"], "angle": 361},
    {"kind": "gradient", "stops": ["#0b1416", "#173a3b"], "file": "x.png"},
    {"kind": "image", "file": "../secret.png"},
    {"kind": "image", "file": "https://example.com/x.png"},
    {"kind": "image", "file": "/absolute/x.png"},
    {"kind": "image", "file": "x.gif"},
    {"kind": "image", "file": "x.png", "dim": 0.9},
    {"kind": "image", "file": "x.png", "blur": 25},
    {"kind": "image", "file": "x.png", "stops": ["#000", "#fff"]},
    {"kind": "noise"},
    [],
])
def test_everything_else_is_refused(background):
    assert schema_errors(background), background
    with pytest.raises(themes.ThemeError):
        themes.validate(base_theme(background=background))


def test_the_background_object_is_optional():
    assert validator.validate(base_theme(), SCHEMA, SCHEMA) == []
    assert themes.validate(base_theme())["id"] == "backdrop"


def test_defaults_are_filled_in_once(tmp_path):
    theme = base_theme(background={"kind": "image", "file": "backdrop.webp"})
    directory = themes.theme_dir(tmp_path)
    directory.mkdir(parents=True)
    (directory / "backdrop.json").write_text(json.dumps(theme), encoding="utf-8")

    resolved = themes.get("backdrop", tmp_path)
    assert resolved["background"] == {
        "kind": "image", "file": "backdrop.webp", "dim": 0.35, "blur": 0.0,
    }
    assert themes.get("kova-dark", tmp_path)["background"] is None


# ---------------------------------------------------------------------- route
def test_the_asset_route_is_authenticated_and_typed(config):
    with TestClient(create_app(config), base_url="http://localhost") as client:
        assert client.get("/themes/assets/backdrop.png").status_code == 401
        client.headers["Authorization"] = f"Bearer {ensure_token(config.home)}"
        write_asset(config)
        response = client.get("/themes/assets/backdrop.png")
        assert response.status_code == 200
        assert response.content == b"\x89PNG\r\n\x1a\n"
        assert response.headers["content-type"] == "image/png"
        assert response.headers["x-content-type-options"] == "nosniff"


@pytest.mark.parametrize("name", [
    "missing.png", "backdrop.gif", "Backdrop.png", "..%2Fsecret.png",
    "..%5Csecret.png", "%2E%2E%2Fsecret.png", ".hidden.png", "a" * 70 + ".png",
])
def test_the_route_refuses_anything_but_a_plain_image_name(http, config, name):
    write_asset(config)
    (themes.assets_dir(config.home).parent / "secret.png").write_bytes(b"secret")
    assert http.get(f"/themes/assets/{name}").status_code == 404


def test_the_route_refuses_an_oversized_image(http, config):
    write_asset(config, "huge.png", b"0" * (themes.BACKGROUND_MAX_BYTES + 1))
    response = http.get("/themes/assets/huge.png")
    assert response.status_code == 413


def test_the_route_refuses_a_symlink_out_of_the_assets_folder(http, config):
    write_asset(config)
    outside = Path(config.home) / "secret.png"
    outside.write_bytes(b"secret")
    link = themes.assets_dir(config.home) / "link.png"
    try:
        link.symlink_to(outside)
    except OSError:
        pytest.skip("this filesystem cannot create symlinks")
    assert http.get("/themes/assets/link.png").status_code == 404


# ------------------------------------------------------------------ preference
def test_backgrounds_are_on_by_default_and_shared(http):
    assert ui_prefs.DEFAULTS["backgrounds"] == "on"
    assert http.get("/preferences").json()["backgrounds"] == "on"
    saved = http.put("/preferences", json={"backgrounds": "off"})
    assert saved.status_code == 200
    assert saved.json()["backgrounds"] == "off"
    assert http.get("/preferences").json()["backgrounds"] == "off"
    assert http.put("/preferences", json={"backgrounds": "maybe"}).status_code == 422
    assert http.put("/preferences", json={"backgrounds": "on"}).json()["backgrounds"] == "on"


# ----------------------------------------------------------------------- web
def test_the_browser_paints_a_layer_and_honours_the_fallbacks():
    css = (ASSETS / "app.css").read_text(encoding="utf-8")
    script = (ASSETS / "theme.js").read_text(encoding="utf-8")
    typography = (ASSETS / "typography.js").read_text(encoding="utf-8")

    assert "--theme-background" in script
    assert "linear-gradient" in script
    assert "/themes/assets/" in script
    assert "dataset.background" in script
    assert "'data-backgrounds'" in typography

    assert "--theme-background" in css
    assert "backdrop-filter" in css
    assert "prefers-reduced-transparency" in css
    assert '[data-motion="off"]' in css
    assert '[data-backgrounds="off"]' in css
    # The opaque fallback repeats the measured panel surfaces, not a new colour.
    assert "background:var(--bg-surface)" in css
    assert "background:var(--bg-base)" in css
