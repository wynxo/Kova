"""Static checks for the Settings view (issue #184, first slice).

The view is plain files with no bundler or type checker, so these tests pin the
things a browser would only reveal late: the page loads both assets, every
control the view links to exists, the sections are the agreed eight (every row that left the sidebar), and the
stylesheet keeps to tokens instead of inventing colours.
"""

from __future__ import annotations

import re
from html.parser import HTMLParser
from pathlib import Path

ASSETS = Path(__file__).resolve().parents[1] / "src" / "kova" / "web_assets"
INDEX = ASSETS / "index.html"
SETTINGS_JS = ASSETS / "settings.js"
SETTINGS_CSS = ASSETS / "settings.css"

SECTIONS = ["General", "Models and providers", "Memory", "Reminders", "Extensions and MCP",
            "Privacy", "Phone", "Install and notifications"]

# Colour syntax a stylesheet may not carry when the semantic layer owns colour.
COLOUR_LITERAL = re.compile(
    r"#[0-9a-fA-F]{3,8}\b|\brgba?\s*\(|\bhsla?\s*\(|"
    r"\b(?:black|white|red|green|blue|gray|grey|orange|yellow|purple|pink|"
    r"brown|cyan|magenta|lime|navy|teal|olive|maroon|silver|gold)\b(?!-)",
    re.IGNORECASE,
)


class Page(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.ids: set[str] = set()
        self.references: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        for name, value in attrs:
            if name == "id" and value:
                self.ids.add(value)
            if name in {"src", "href"} and value:
                self.references.append(value)

    handle_startendtag = handle_starttag


def page() -> Page:
    parser = Page()
    parser.feed(INDEX.read_text(encoding="utf-8"))
    return parser


def strip_comments(css: str) -> str:
    return re.sub(r"/\*.*?\*/", "", css, flags=re.S)


def test_index_loads_the_settings_assets_after_app():
    references = page().references
    assert "/app/assets/settings.css" in references
    assert "/app/assets/settings.js" in references
    assert references.index("/app/assets/app.js") < references.index("/app/assets/settings.js")
    assert references.index("/app/assets/tokens.css") < references.index("/app/assets/settings.css")
    assert SETTINGS_JS.is_file() and SETTINGS_CSS.is_file()


def test_sidebar_has_the_settings_launcher():
    ids = page().ids
    assert "open-settings" in ids
    source = INDEX.read_text(encoding="utf-8")
    assert 'id="open-settings"' in source


def test_every_linked_control_exists_in_the_page():
    ids = page().ids
    source = SETTINGS_JS.read_text(encoding="utf-8")
    actions = set(re.findall(r"action:\s*'([a-z-]+)'", source))
    assert actions, "no linked controls found; did the settings data move?"
    missing = sorted(actions - ids)
    assert missing == [], f"settings.js links to controls missing from index.html: {missing}"
    # Literal byId lookups are the sidebar launcher and the theme control.
    for literal in re.findall(r"byId\('([a-z-]+)'\)", source):
        assert literal in ids, f"settings.js looks up missing id {literal!r}"


def test_sections_are_the_agreed_eight_in_order():
    source = SETTINGS_JS.read_text(encoding="utf-8")
    labels = re.findall(r"id: '[a-z]+', label: '([^']+)'", source)
    assert labels == SECTIONS


def test_settings_view_is_labelled_and_keyboard_operable():
    source = SETTINGS_JS.read_text(encoding="utf-8")
    for marker in ("'aria-labelledby', 'settings-title'", "role', 'tablist'", "role', 'tabpanel'",
                   "aria-selected", "aria-controls", "aria-live", "'Search settings'",
                   "ArrowDown", "ArrowUp", "Home", "End", "Escape", "'Tab'"):
        assert marker in source, f"settings.js is missing {marker!r}"
    assert "opener?.focus" in source, "closing the view must return focus to its opener"


def test_settings_css_uses_no_colour_literals():
    css = strip_comments(SETTINGS_CSS.read_text(encoding="utf-8"))
    found = COLOUR_LITERAL.findall(css)
    assert found == [], f"settings.css hard-codes colour values: {sorted(set(found))}"


def test_settings_css_uses_semantic_tokens():
    css = strip_comments(SETTINGS_CSS.read_text(encoding="utf-8"))
    for token in ("--bg-surface", "--bg-base", "--bg-raised", "--fg-default", "--fg-muted",
                  "--border-subtle", "--border-strong", "--accent-default", "--accent-soft"):
        assert f"var({token})" in css, f"settings.css should use the semantic token {token}"


def test_settings_css_honours_focus_and_reduced_motion():
    css = strip_comments(SETTINGS_CSS.read_text(encoding="utf-8"))
    assert ":focus-visible" in css
    assert "@media (prefers-reduced-motion: reduce)" in css
    assert "animation: none" in css and "transition: none" in css


def test_every_sidebar_panel_moved_into_settings():
    """The rows removed from the sidebar each have a Settings row."""
    source = SETTINGS_JS.read_text(encoding="utf-8")
    actions = set(re.findall(r"action:\s*'([a-z-]+)'", source))
    for moved in ("open-memory", "open-reminders", "add-provider", "open-extensions", "open-privacy",
                  "open-mcp", "open-sounds", "install-settings", "connect-phone", "disconnect",
                  "open-shortcuts", "open-model-settings"):
        assert moved in actions, f"{moved} has no Settings row"
    assert "host: 'settings'" in source, "startup is hosted in Settings"
    assert "control: 'theme'" in source, "theme is a Settings control"
