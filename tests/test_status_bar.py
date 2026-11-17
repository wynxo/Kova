"""First slice of #204: an optional, off-by-default status bar and context meter.

The bar lives in new files (`status-bar.js`/`status-bar.css`) with minimal
hooks elsewhere, stays hidden and quiet until opted in, reuses the data the
server already exposes (chat messages, the model list, the active model) and
the existing Compact action, and never hard-codes a colour.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from kova.config import ensure_token, load_config
from kova.server import create_app

ROOT = Path(__file__).resolve().parent.parent
ASSETS = ROOT / "src" / "kova" / "web_assets"
INDEX = ASSETS / "index.html"
CSS = ASSETS / "status-bar.css"
JS = ASSETS / "status-bar.js"
APP_JS = ASSETS / "app.js"


@pytest.fixture
def page():
    with open(INDEX, encoding="utf-8") as handle:
        return handle.read()


def test_new_files_exist_and_are_served(tmp_path, monkeypatch):
    monkeypatch.setenv("KOVA_HOME", str(tmp_path))
    cfg = load_config()
    app = create_app(cfg)
    with TestClient(app) as client:
        for name in ("status-bar.js", "status-bar.css"):
            response = client.get(f"/app/assets/{name}")
            assert response.status_code == 200, name
            assert response.content, name


def test_stylesheet_loads_after_the_shipped_palette(page):
    assert page.index("/app/assets/tokens.css") < page.index("/app/assets/status-bar.css")


def test_script_loads_before_the_composer_bridge(page):
    # app.js paints through window.KovaStatus on its first controls() pass, so
    # the module defining it must run first.
    assert page.index("/app/assets/status-bar.js") < page.index("/app/assets/app.js")


def test_bar_is_hidden_by_default(page):
    assert re.search(r'<div id="status-bar"[^>]*\bhidden\b', page), \
        "the bar must ship hidden, not rely on JS to hide it"


def test_toggle_lives_in_settings(page):
    assert 'id="status-bar-toggle"' in page


def test_stylesheet_uses_tokens_never_hard_coded_colours():
    text = re.sub(r"/\*.*?\*/", "", CSS.read_text(encoding="utf-8"), flags=re.S)
    assert not re.search(r"#[0-9a-fA-F]{3,8}\b", text), "hex colour in the status bar"
    assert "rgb(" not in text and "hsl(" not in text, "colour function in the status bar"
    declarations = "\n".join(line for line in text.splitlines() if ":" in line)
    assert "--accent" not in declarations, "no accent colour while idle"


def test_module_adds_no_endpoint_and_reuses_compact():
    text = JS.read_text(encoding="utf-8")
    assert "fetch(" not in text, "the bar reads what the client already holds"
    for route in ('"/compact"', '"/models"', '"/health"', '"/usage"', '"/chats"', "'/compact'",
                    "'/models'", "'/health'", "'/usage'", "'/chats'"):
        assert route not in text, f"the bar must not call {route}; app.js passes the data"
    assert 'compact-now' in text, "the nearly-full state reuses the existing Compact action"


def test_module_defaults_to_hidden_and_thresholds_at_80_percent():
    text = JS.read_text(encoding="utf-8")
    assert "getItem" in text and "=== 'shown'" in text, "opt-in, never on by itself"
    assert "0.8" in text, "Compact is offered at 80% of the window"


def test_bridge_passes_server_data_without_new_lookups(page):
    text = APP_JS.read_text(encoding="utf-8")
    assert "KovaStatus" in text and "refreshStatusBar" in text
    # The only element the bridge reads is the composer's own model select.
    assert "$('model')" in text
    used = set(re.findall(r"""\$\s*\(\s*(['"`])([^'"`$]+)\1\s*\)""", text))
    missing = sorted(name for _, name in used if f'id="{name}"' not in page)
    assert missing == [], f"app.js looks up ids missing from index.html: {missing}"
