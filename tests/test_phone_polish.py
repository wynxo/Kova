"""Phone polish (#202): safe areas, the keyboard, sheets and gestures.

``mobile.css`` and ``mobile.js`` are separate so app.js stays the conversation's
file. These checks keep the contract the shell and the other modules rely on:
the page loads them in the right order, the offline cache carries them, and the
CSS takes every value from tokens.css. Drag, swipe and visualViewport behaviour
need a browser; the opt-in visual job covers the rendered layout.
"""

from __future__ import annotations

import re
from pathlib import Path

ASSETS = Path(__file__).resolve().parents[1] / "src" / "kova" / "web_assets"
INDEX = ASSETS / "index.html"
MOBILE_CSS = (ASSETS / "mobile.css").read_text(encoding="utf-8")
MOBILE_JS = (ASSETS / "mobile.js").read_text(encoding="utf-8")
SHELL = (ASSETS / "sw.js").read_text(encoding="utf-8")
COLOUR_LITERAL = re.compile(r"#[0-9a-fA-F]{3,8}\b|\brgba?\(|\bhsla?\(")


def without_comments(text: str) -> str:
    """Comments carry issue numbers like #202, which are not colours."""
    return re.sub(r"/\*.*?\*/", "", text, flags=re.S)


def rule(selector: str) -> str:
    """One rule's declarations, without the selector."""
    found = re.search(re.escape(selector) + r"\s*\{([^}]*)\}", without_comments(MOBILE_CSS))
    assert found, f"mobile.css lost its {selector} rule"
    return found.group(1)


def test_shell_loads_both_files_in_order_and_caches_them() -> None:
    page = INDEX.read_text(encoding="utf-8")
    # app.css owns the shell, so only a later sheet can override a popover.
    assert page.index("mobile.css") > page.index("app.css")
    assert page.index("mobile.js") < page.index("app.js")
    assert page.index("mobile.css") > page.index("panes.css")
    assert "'/app/assets/mobile.css'" in SHELL and "'/app/assets/mobile.js'" in SHELL


def test_the_page_paints_under_the_notch_so_safe_areas_can_pad_it_back() -> None:
    page = INDEX.read_text(encoding="utf-8")
    viewport = re.search(r'<meta name="viewport" content="([^"]+)"', page)
    assert viewport and "viewport-fit=cover" in viewport.group(1), \
        "env(safe-area-inset-*) stays 0 without viewport-fit=cover"


def test_the_chrome_pads_itself_out_of_the_notch_and_the_home_bar() -> None:
    for selector in (".topbar", "#sidebar", ".compose-wrap"):
        declarations = rule(selector)
        assert "env(safe-area-inset-" in declarations, \
            f"{selector} does not respect a safe-area inset"


def test_the_composer_follows_the_visual_viewport_and_never_zooms_on_focus() -> None:
    css = without_comments(MOBILE_CSS)
    assert "body { height:var(--app-viewport-height, 100dvh); }" in css
    # 16px is the iOS focus threshold; a token keeps the text-size setting in charge.
    assert "font-size:max(16px, var(--type-size-body-comfortable))" in css
    for needed in ("visualViewport", "'--app-viewport-height'", "'--app-keyboard-inset'"):
        assert needed in MOBILE_JS, f"mobile.js lost {needed}"
    assert "setInterval" not in MOBILE_JS, "the viewport is measured per frame, not polled"


def test_menus_become_bottom_sheets_with_a_handle_and_swipe_down() -> None:
    css = without_comments(MOBILE_CSS)
    for selector in (".compose-menu", ".chat-options", ".chat-menu-list", ".activity-panel", ".popover"):
        assert selector in css, f"mobile.css leaves {selector} off the sheet list"
    assert "@media (max-width:720px)" in css
    assert ".sheet-handle" in css and "sheet-rise" in css
    assert ".sheet-scrim" in css
    for needed in ("SHEETS", "sheet-handle", "pointerdown", "pointermove", "pointerup",
                   "closeSheet", "sheet-closing"):
        assert needed in MOBILE_JS, f"mobile.js lost {needed}"
    # Escape and the backdrop close a sheet too, through the owner's own path.
    assert "'Escape'" in MOBILE_JS
    assert "event.target !== scrim" in MOBILE_JS
    assert "closeSheet(topSheet())" in MOBILE_JS
    assert "'compose-more'" in MOBILE_JS and "'chat-options'" in MOBILE_JS


def test_the_edge_swipe_skips_horizontal_scrollers_and_leaves_escape_alone() -> None:
    for needed in ("touchstart", "touchend", "inHorizontalScroller", "overflowX",
                   "scrollWidth", "#sidebar", "'sheet-open'", "'panel-open'", "'settings-open'"):
        assert needed in MOBILE_JS, f"mobile.js lost {needed}"
    assert "EDGE" in MOBILE_JS and "SWIPE" in MOBILE_JS
    # The drawer is opened through the app's own toggle, so state stays in one place.
    assert "getElementById('menu')?.click()" in MOBILE_JS


def test_touch_targets_are_44px_without_moving_the_chrome() -> None:
    assert "--touch-target:max(44px, calc(var(--space-10) + var(--space-1)))" in without_comments(MOBILE_CSS), \
        "44px is the platform floor; density must not shrink it"
    css = without_comments(MOBILE_CSS)
    assert re.search(r"width:var\(--touch-target\);\s*height:var\(--touch-target\)", css), \
        "the expanded target is a centred square over the control"


def test_the_new_sheet_takes_colour_and_motion_from_tokens() -> None:
    css = without_comments(MOBILE_CSS)
    assert COLOUR_LITERAL.findall(css) == [], "mobile.css hard-codes a colour; use a token"
    assert "var(--motion-base)" in css and "var(--motion-ease-standard)" in css
    assert "[data-motion" not in css, "the one motion switch lives in app.css"
    for token in ("--bg-surface", "--bg-overlay", "--border-strong", "--radius-lg",
                  "--space-3", "--space-4"):
        assert f"var({token})" in css, f"mobile.css should take its look from {token}"


def test_the_script_is_self_contained_and_builds_no_markup_from_strings() -> None:
    text = without_comments(MOBILE_JS)
    for forbidden in ("innerHTML", "outerHTML", "insertAdjacentHTML", "eval(",
                      "new Function", "fetch(", "http://", "https://"):
        assert forbidden not in text, f"mobile.js must not use {forbidden}"
    assert "createElement" in text, "the handle and the scrim are built as elements"
    assert "aria-hidden" in text, "the scrim and the handle are decoration"
