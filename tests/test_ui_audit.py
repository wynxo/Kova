"""The accessibility audit must pass on the shipped UI and catch real regressions.

A gate is only worth having if it fails for the reason it claims, so each check is
exercised twice: once on a broken fragment, once on the pattern the app uses.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ASSETS = ROOT / "src" / "kova" / "web_assets"


def _audit():
    path = ROOT / "scripts" / "audit_ui.py"
    spec = importlib.util.spec_from_file_location("kova_audit_ui", path)
    assert spec and spec.loader, f"cannot import {path}"
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


audit_ui = _audit()


def test_shipped_pages_and_stylesheets_pass() -> None:
    problems = audit_ui.audit([ASSETS])
    assert not problems, "\n".join(problems)


def test_missing_lang_is_reported() -> None:
    assert any("lang" in p for p in audit_ui.check_html("<html><head></head></html>", "x.html"))


def test_missing_viewport_is_reported() -> None:
    page = '<html lang="en"><head><title>t</title></head></html>'
    assert any("viewport" in p for p in audit_ui.check_html(page, "x.html"))


def test_inline_handler_is_reported() -> None:
    page = ('<html lang="en"><head><title>t</title>'
            '<meta name="viewport" content="width=device-width"></head>'
            '<body><button onclick="go()">go</button></body></html>')
    assert any("onclick" in p for p in audit_ui.check_html(page, "x.html"))


def test_image_without_alt_is_reported() -> None:
    page = ('<html lang="en"><head><title>t</title>'
            '<meta name="viewport" content="width=device-width"></head>'
            '<body><img src="logo.svg"></body></html>')
    assert any("alt" in p for p in audit_ui.check_html(page, "x.html"))


def test_decorative_image_with_empty_alt_is_allowed() -> None:
    page = ('<html lang="en"><head><title>t</title>'
            '<meta name="viewport" content="width=device-width"></head>'
            '<body><img src="logo.svg" alt=""></body></html>')
    assert audit_ui.check_html(page, "x.html") == []


def test_focus_rule_that_removes_the_outline_is_reported() -> None:
    css = ":focus-visible { outline: 2px solid red; }\nbutton:focus { outline: none; }\n"
    assert any("removes the outline" in p for p in audit_ui.check_css(css, "x.css"))


def test_container_outline_removal_is_inert() -> None:
    # A scroll container is not focusable; clearing its outline changes nothing.
    css = (":focus-visible { outline: 2px solid red; }\n"
           "@media (prefers-reduced-motion: reduce) { * { transition: none; } }\n"
           ".list { outline: 0; }\n")
    assert audit_ui.check_css(css, "x.css") == []


def test_removed_outline_replaced_by_box_shadow_is_allowed() -> None:
    css = (":focus-visible { outline: 2px solid red; }\n"
           "@media (prefers-reduced-motion: reduce) { * { transition: none; } }\n"
           "input:focus-visible { outline: none; box-shadow: 0 0 0 3px var(--ring); }\n")
    assert audit_ui.check_css(css, "x.css") == []


def test_stylesheet_without_focus_ring_is_reported() -> None:
    css = "@media (prefers-reduced-motion: reduce) { * { transition: none; } }\n"
    assert any("focus-visible" in p for p in audit_ui.check_css(css, "x.css"))


def test_stylesheet_without_reduced_motion_is_reported() -> None:
    assert any("reduced-motion" in p for p in audit_ui.check_css(":focus-visible { outline: 1px }", "x.css"))


def test_a_token_only_sheet_needs_no_focus_ring_or_motion_block():
    sheet = ':root, [data-theme="dark"] { --bg: #000; }\n[data-theme="light"] { --bg: #fff; }\n'
    assert audit_ui.check_css(sheet, "tokens.css") == []


def test_a_sheet_that_styles_elements_still_needs_them():
    assert audit_ui.check_css(":root { --a: 1; }\nbutton { color: red; }\n", "x.css")
