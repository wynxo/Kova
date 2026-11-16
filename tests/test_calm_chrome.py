"""Guard-rails for the app shell's chrome (epic #214, docs/DESIGN.md).

A healthy state is the normal case and says nothing: no permanent "connected"
pill, no "your chats stay yours" badge, no slogan under the message box. Each of
these crept in once; these checks keep them from creeping back.
"""

from __future__ import annotations

import re
from html.parser import HTMLParser
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
INDEX = ROOT / "src" / "kova" / "web_assets" / "index.html"


class _Shell(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.by_id: dict[str, dict[str, str | None]] = {}
        self.inline_styles: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        if values.get("id"):
            self.by_id[str(values["id"])] = values
        if "style" in values:
            self.inline_styles.append(f"<{tag} id={values.get('id')!r}>")


def _shell() -> tuple[_Shell, str]:
    html = INDEX.read_text(encoding="utf-8")
    parser = _Shell()
    parser.feed(html)
    return parser, html


def test_connection_indicator_starts_hidden() -> None:
    """The indicator only appears when app.js reports a problem."""
    parser, _ = _shell()
    assert "hidden" in parser.by_id["connection"]


def test_no_permanent_reassurance_badges() -> None:
    _, html = _shell()
    for noise in ("local-note", "compose-hint", "Your chats stay yours", "All yours"):
        assert noise not in html, f"{noise!r} is back in the app shell"


def test_no_inline_styles_in_the_shell() -> None:
    """Every visual value is a token in a stylesheet, never a style attribute."""
    parser, _ = _shell()
    assert parser.inline_styles == []


def test_header_actions_use_an_icon_not_text_glyphs() -> None:
    _, html = _shell()
    summary = re.search(r'<details class="chat-menu"><summary[^>]*>(.*?)</summary>', html)
    assert summary, "conversation actions menu is missing"
    assert "•" not in summary.group(1)
    assert "i-ellipsis" in summary.group(1)


# ---------------------------------------------------------------- conversation

APP_CSS = ROOT / "src" / "kova" / "web_assets" / "app.css"


def _section(css: str, start: str, end: str) -> str:
    return css[css.index(start):css.index(end)]


def test_start_screen_has_no_decorative_accent_or_tinted_starters() -> None:
    """The accent means activity; an idle start screen shows none of it."""
    _, html = _shell()
    welcome = re.search(r'<div id="welcome".*?</div>', html, re.S).group(0)
    assert "<span>" not in welcome, "no accent-coloured fragment in the greeting"
    for tint in ("suggestion-icon", "mint", "peach", "welcome-foot"):
        assert tint not in html, f"{tint!r} is back on the start screen"


def test_conversation_type_comes_from_the_scale() -> None:
    """Messages, reasoning, tools and cards use the 12/14/16/18/24 type tokens only."""
    css = APP_CSS.read_text(encoding="utf-8")
    section = _section(css, "/* ---------- Messages", "/* ---------- Dialogs")
    raw = re.findall(r"font(?:-size)?:[^;}]*?\b\d+(?:\.\d+)?px", section)
    assert raw == [], f"raw pixel font sizes in the conversation: {raw}"


def test_conversation_cards_share_one_radius() -> None:
    """Tool, plan, change, approval and error cards all use --radius-md."""
    css = APP_CSS.read_text(encoding="utf-8")
    for selector in (".tool {", ".plan-view {", ".changes-card {", ".approval-card {", ".turn-error {"):
        rule = css[css.index(selector):css.index("}", css.index(selector))]
        assert "border-radius:var(--radius-md)" in rule, f"{selector} uses another radius"


def test_status_words_are_not_filled_pills() -> None:
    css = APP_CSS.read_text(encoding="utf-8")
    for selector in (".status-badge {", ".risk-badge {"):
        rule = css[css.index(selector):css.index("}", css.index(selector))]
        assert "background" not in rule and "uppercase" not in rule, selector


# ------------------------------------------------------------ sidebar, composer


def test_sidebar_and_composer_type_comes_from_the_scale() -> None:
    css = APP_CSS.read_text(encoding="utf-8")
    for start, end in (("/* ---------- Sidebar", "/* ---------- Main column"),
                       ("/* ---------- Composer", "/* ---------- Popover")):
        raw = re.findall(r"font(?:-size)?:[^;}]*?\b\d+(?:\.\d+)?px", _section(css, start, end))
        assert raw == [], f"raw pixel font sizes after {start!r}: {raw}"


def test_no_emoji_or_text_glyphs_standing_in_for_icons() -> None:
    _, html = _shell()
    for button in ("attach", "commands"):
        tag = re.search(rf'<button id="{button}"[^>]*>(.*?)</button>', html).group(1)
        assert 'class="icon i-' in tag, f"#{button} should use an icon from the set"
    assert "📎" not in html


def test_every_sidebar_nav_row_has_an_icon() -> None:
    """Rows without an icon break the column the others line up on."""
    _, html = _shell()
    rows = re.findall(r'<(?:button|summary)[^>]*class="nav-row"[^>]*>(.*?)</(?:button|summary)>', html)
    sidebar = [row for row in rows if "Compact now" not in row]
    assert sidebar, "expected sidebar nav rows"
    for row in sidebar:
        assert 'class="icon i-' in row, f"nav row without an icon: {row!r}"


def test_idle_composer_has_no_accent() -> None:
    """Send, toggles and focus are monochrome; the accent is reserved for activity."""
    css = APP_CSS.read_text(encoding="utf-8")
    composer = _section(css, "/* ---------- Composer", "/* ---------- Popover")
    assert "var(--accent" not in composer
    assert "infinite" not in composer, "no looping animation on composer controls"
    # Only the floating jump-to-latest button may cast a shadow.
    without_jump = re.sub(r"\.jump \{.*?\}", "", composer, flags=re.S)
    assert "var(--shadow" not in without_jump, "the composer sits in the layout; it needs no shadow"
