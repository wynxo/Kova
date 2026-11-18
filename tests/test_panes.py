"""Static checks for the resizable three-pane layout (#182).

The behaviour (dragging, keys, the phone sheet) needs a browser; these tests
keep the contract that other code and the offline shell depend on.
"""

from __future__ import annotations

import re
from pathlib import Path

ASSETS = Path(__file__).resolve().parents[1] / "src" / "kova" / "web_assets"
PANES_JS = (ASSETS / "panes.js").read_text(encoding="utf-8")
PANES_CSS = (ASSETS / "panes.css").read_text(encoding="utf-8")


def test_panes_load_before_app_js_and_are_cached_offline() -> None:
    index = (ASSETS / "index.html").read_text(encoding="utf-8")
    assert "/app/assets/panes.css" in index
    # Code headers ask for window.KovaPanel while app.js renders history.
    assert index.index("/app/assets/panes.js") < index.index("/app/assets/app.js")
    shell = (ASSETS / "sw.js").read_text(encoding="utf-8")
    assert "'/app/assets/panes.js'" in shell and "'/app/assets/panes.css'" in shell


def test_splitters_are_keyboard_operable_separators() -> None:
    for attribute in ("role: 'separator'", "tabindex: '0'", "aria-valuenow", "aria-valuemin", "aria-valuemax"):
        assert attribute in PANES_JS
    for key in ("ArrowLeft", "ArrowRight", "Home", "End"):
        assert f"'{key}'" in PANES_JS
    assert "'dblclick'" in PANES_JS


def test_saved_widths_never_break_the_page_when_storage_is_blocked() -> None:
    code = re.sub(r"//[^\n]*", "", PANES_JS)  # prose in comments mentions it too
    uses = len(re.findall(r"localStorage\.", code))
    guarded = re.findall(r"try\s*\{[^}]*localStorage\.[^}]*\}\s*catch", code)
    assert uses and sum(block.count("localStorage.") for block in guarded) == uses


def test_panel_api_and_phone_sheet() -> None:
    assert re.search(r"window\.KovaPanel\s*=\s*\{[^}]*open[^}]*close", PANES_JS, re.S)
    assert "'Escape'" in PANES_JS
    phone = PANES_CSS[PANES_CSS.index("@media (max-width:720px)"):]
    assert ".pane-splitter { display:none; }" in phone
    assert "inset:0" in phone


def test_panel_motion_follows_the_setting_and_the_system() -> None:
    assert '[data-motion="reduced"] .side-panel' in PANES_CSS
    assert "prefers-reduced-motion:reduce" in PANES_CSS
