"""200% zoom reflow guard (#201): the page must keep working at double size.

WCAG 1.4.10 (Reflow) requires no loss of content down to 320 CSS px
(400% zoom on a 1280px display); 200% zoom on the same display is 640 CSS
px. The shell already reflows there: the phone layout engages at 720px,
the composer narrows its chip and panel at a 480px container width, and
every dialog and popover is viewport-capped. These static guards lock
those invariants so a new fixed-width panel cannot silently break zoom.

Real Chromium measurements (page overflow and control visibility at 1280,
640 and 320px, with long content and the options sheet open) live in
``tests/visual/test_zoom_reflow.py``.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ASSETS = ROOT / "src" / "kova" / "web_assets"

RULE = re.compile(r"([^{}]+)\{([^{}]*)\}")
WIDTH_PX = re.compile(r"(?<![-\w])width\s*:\s*(\d+)px\b")
# A panel this wide overflows a 320px viewport unless it is fluid or capped.
WIDE_PX = 280


def _css(name: str) -> str:
    return (ASSETS / name).read_text(encoding="utf-8")


def _without_comments(text: str) -> str:
    return re.sub(r"/\*.*?\*/", "", text, flags=re.S)


def _rule_bodies(css: str) -> list[tuple[str, str]]:
    return [(m.group(1).strip(), m.group(2)) for m in RULE.finditer(_without_comments(css))]


def wide_fixed_panels(css: str) -> list[str]:
    """Selectors with a fixed ``width: Npx`` (N >= 280) and no fluid guard.

    A guard is a ``min()``/viewport cap in the same rule; a narrower
    ``@media``/``@container`` override elsewhere covers the small screen.
    """
    problems: list[str] = []
    for selector, body in _rule_bodies(css):
        if selector.startswith("@"):
            continue
        match = WIDTH_PX.search(body)
        if not match or int(match.group(1)) < WIDE_PX:
            continue
        if "min(" in body or "max-width" in body or "100vw" in body or "100%" in body:
            continue
        if _has_narrow_override(css, selector):
            continue
        problems.append(f"{selector}: fixed {match.group(0)} with no fluid guard or narrow override")
    return problems


def _has_narrow_override(css: str, selector: str) -> bool:
    """A later ``@media``/``@container`` block restating this selector fluidly."""
    first = selector.split(",")[0].strip().lstrip(".#")
    if not first:
        return False
    for match in re.finditer(r"@(media|container)[^{]*\{(?:[^{}]*\{[^{}]*\})*[^{}]*\}", _without_comments(css)):
        block = match.group(0)
        if first in block and ("min(" in block or "100vw" in block or "max-width" in block):
            return True
    return False


def test_viewport_allows_200_percent_zoom() -> None:
    for page in ("index.html", "token-sheet.html"):
        html = (ASSETS / page).read_text(encoding="utf-8")
        assert "width=device-width" in html, f"{page} must use the device width"
        assert "user-scalable=no" not in html, f"{page} disables zoom"
        assert "maximum-scale" not in html, f"{page} caps zoom"


def test_phone_layout_engages_by_640px() -> None:
    """200% zoom on a 1280px display is 640 CSS px: the phone rules must apply."""
    app = _css("app.css")
    assert "@media (max-width:720px)" in app
    assert "@container composer (max-width:480px)" in app
    mobile = _css("mobile.css")
    assert "@media (max-width:720px)" in mobile


def test_dialogs_and_popovers_are_viewport_capped() -> None:
    app = _css("app.css")
    assert "dialog {\n  width:min(440px, calc(100vw - 24px))" in app
    assert "max-width:min(340px, calc(100vw - 16px))" in app  # .popover
    assert "width:min(calc(var(--space-10) * 8), calc(100% - var(--space-6)))" in app  # .activity-panel
    assert "width:min(300px, 86vw)" in app  # the sidebar drawer
    assert "width:min(560px, calc(100vw - var(--space-6)))" in _css("palette.css")
    assert "width: min(360px, calc(100vw - var(--space-6)))" in _css("toast.css")


def test_chat_options_panel_cannot_overflow_a_narrow_composer() -> None:
    app = _css("app.css")
    assert ".chat-options { right:0; display:grid; gap:var(--space-1); width:300px;" in app
    # The narrow-composer override and the phone bottom sheet both cap it.
    assert ".chat-options { width:min(300px, calc(100vw - var(--space-6))); }" in app
    mobile = _css("mobile.css")
    assert ".chat-options:not([hidden])" in mobile
    assert "max-width:none" in mobile


def test_long_content_wraps_or_scrolls_instead_of_overflowing() -> None:
    app = _css("app.css")
    assert "main { display:flex; flex-direction:column; min-width:0;" in app
    assert ".messages { display:flex; flex-direction:column;" in app
    assert ".compose-row { display:flex;" in app and "min-width:0" in app
    markdown = _css("markdown.css")
    assert "overflow-x:auto" in markdown, "code blocks must scroll, not push the page wide"
    assert ".file-diff-source { flex: 1; min-width: 0;" in _css("diff.css")
    assert "overflow-wrap: anywhere" in _css("diff.css")


def test_no_wide_fixed_panel_without_a_fluid_guard_or_override() -> None:
    problems: list[str] = []
    for sheet in ("app.css", "settings.css", "panes.css", "palette.css", "toast.css"):
        problems.extend(f"{sheet}: {p}" for p in wide_fixed_panels(_css(sheet)))
    assert not problems, "\n".join(problems)


def test_scanner_catches_a_fixed_panel() -> None:
    """The guard itself works: a fixed 400px panel fails, a capped one passes."""
    assert wide_fixed_panels(".panel { width:400px; }") != []
    assert wide_fixed_panels(".panel { width:min(400px, calc(100vw - 24px)); }") == []
    narrow = ".panel { width:300px; }\n@container composer (max-width:480px) {\n.panel { width:min(300px, calc(100vw - 24px)); }\n}"
    assert wide_fixed_panels(narrow) == []
