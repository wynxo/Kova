"""Guard-rails for the app shell's chrome (epic #214, docs/DESIGN.md).

A healthy state is the normal case and says nothing: no permanent "connected"
pill, no "your chats stay yours" badge, no slogan under the message box. The
shell is minimal on purpose: the sidebar is New chat, one search, the list and
one Settings row; the message box is +, the text, one model chip and Send.
Everything else lives in Settings or the command palette. Each of these crept
in once; these checks keep them from creeping back.
"""

from __future__ import annotations

import re
from html.parser import HTMLParser
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
INDEX = ROOT / "src" / "kova" / "web_assets" / "index.html"


VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source", "track", "wbr"}


class _Shell(HTMLParser):
    """Element ids, plus which ids sit inside which (to check what is on screen)."""

    def __init__(self) -> None:
        super().__init__()
        self.by_id: dict[str, dict[str, str | None]] = {}
        self.ancestors: dict[str, list[str]] = {}
        self.inline_styles: list[str] = []
        self.inside: dict[str, list[tuple[str, dict[str, str | None]]]] = {}
        self._stack: list[tuple[str, str | None]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        open_ids = [i for _, i in self._stack if i]
        for owner in open_ids:
            self.inside.setdefault(owner, []).append((tag, values))
        if values.get("id"):
            self.by_id[str(values["id"])] = values
            self.ancestors[str(values["id"])] = open_ids
        if "style" in values:
            self.inline_styles.append(f"<{tag} id={values.get('id')!r}>")
        if tag not in VOID:
            self._stack.append((tag, values.get("id")))

    def handle_endtag(self, tag: str) -> None:
        for at in range(len(self._stack) - 1, -1, -1):
            if self._stack[at][0] == tag:
                del self._stack[at:]
                break


def _shell() -> tuple[_Shell, str]:
    html = INDEX.read_text(encoding="utf-8")
    parser = _Shell()
    parser.feed(html)
    return parser, html


def _ids_inside(parser: _Shell, owner: str, tag: str | None = None) -> list[str]:
    return [str(values["id"]) for name, values in parser.inside.get(owner, [])
            if values.get("id") and (tag is None or name == tag)]


def _hidden_by(parser: _Shell, element: str) -> bool:
    """True when the element or any ancestor carries ``hidden``."""
    return any("hidden" in parser.by_id[i] for i in [element, *parser.ancestors[element]] if i in parser.by_id)


# ------------------------------------------------------------------- header


def test_header_is_toggle_title_and_actions_only() -> None:
    """No connection dot, no Retry button, no banner: a healthy link says nothing."""
    parser, html = _shell()
    header = re.search(r'<header class="topbar">(.*?)</header>', html, re.S).group(1)
    ids = re.findall(r'\bid="([^"]+)"', header)
    assert ids == ["menu", "chat-title", "chat-menu", "compact-now"], ids
    assert "hidden" in parser.by_id["chat-menu"], "chat actions appear only once a chat is open"
    for gone in ("connection", "banner", "local-only-status-header"):
        assert gone not in parser.by_id, f"#{gone} is back"
    for noise in ("Disconnected", "Retry connection", "Model connected"):
        assert noise not in html, f"{noise!r} is back in the shell"


def test_failure_notice_is_hidden_until_an_action_fails() -> None:
    """One quiet line above the message box, with Retry, and nothing until then."""
    parser, _ = _shell()
    assert "hidden" in parser.by_id["notice"]
    assert "notice-retry" in _ids_inside(parser, "notice")
    assert parser.by_id["notice"].get("role") == "alert"
    _, html = _shell()
    assert not {"sidebar", "composer"} & set(parser.ancestors["notice"])
    assert html.index('id="notice"') < html.index('id="composer"'), "the notice sits just above the box"


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
    summary = re.search(r'<details id="chat-menu" class="chat-menu"[^>]*><summary[^>]*>(.*?)</summary>', html)
    assert summary, "chat actions menu is missing"
    assert "•" not in summary.group(1)
    assert "i-ellipsis" in summary.group(1)


# ------------------------------------------------------------------- sidebar

MOVED_TO_SETTINGS = ("open-memory", "open-reminders", "add-provider", "open-extensions", "open-privacy",
                     "open-mcp", "open-sounds", "install-settings", "connect-phone", "settings",
                     "theme", "disconnect", "open-shortcuts")


def test_sidebar_is_new_chat_search_list_and_settings() -> None:
    parser, html = _shell()
    sidebar = re.search(r'<aside id="sidebar".*?</aside>', html, re.S).group(0)
    buttons = _ids_inside(parser, "sidebar", "button")
    assert buttons == ["sidebar-close", "new-chat", "open-settings"], buttons
    assert _ids_inside(parser, "sidebar", "input") == ["search"], "one search field"
    assert "<kbd" not in sidebar, "no shortcut badges in the sidebar"
    for gone in ("open-palette", "chat-count", "Search or run a command", "Incognito chat"):
        assert gone not in sidebar, f"{gone!r} is back in the sidebar"
    footer = re.search(r'<div class="sidebar-footer">(.*?)</div>', sidebar, re.S).group(1)
    assert footer.count("<button") == 1 and "Settings" in footer and "i-settings" in footer


def test_moved_rows_live_in_settings_not_the_sidebar() -> None:
    """Every panel that left the sidebar still has its trigger, out of sight."""
    parser, _ = _shell()
    for element in MOVED_TO_SETTINGS:
        assert element in parser.by_id, f"#{element} lost its trigger"
        assert "sidebar" not in parser.ancestors[element], f"#{element} is back in the sidebar"
        assert _hidden_by(parser, element), f"#{element} should only be reached from Settings or the palette"


# ------------------------------------------------------------------ composer


def test_message_box_shows_plus_text_model_chip_and_send() -> None:
    """The only controls on the surface; Queue, Steer and Stop appear mid-reply."""
    parser, _ = _shell()
    controls = [str(values["id"]) for tag, values in parser.inside["composer"]
                if tag in ("button", "textarea", "input", "select") and values.get("id")
                and not _hidden_by(parser, str(values["id"]))]
    assert controls == ["compose-more", "prompt", "chat-options", "send"], controls


def test_options_and_extras_sit_behind_the_plus_and_the_chip() -> None:
    parser, _ = _shell()
    for element in ("attach", "commands", "new-incognito"):
        assert "compose-menu" in parser.ancestors[element], f"#{element} belongs in the + menu"
    for element in ("model-slot", "reasoning-effort-slot", "mode-switch", "model-make-default",
                    "permission-slot", "cwd", "web"):
        assert "chat-options-panel" in parser.ancestors[element], f"#{element} belongs behind the chip"
    assert "hidden" in parser.by_id["compose-menu"] and "hidden" in parser.by_id["chat-options-panel"]
    assert parser.by_id["chat-options"].get("aria-controls") == "chat-options-panel"
    assert parser.by_id["compose-more"].get("aria-controls") == "compose-menu"
    for gone in ("options", "options-label", "compose-options"):
        assert gone not in parser.by_id, f"#{gone} is back"


def test_start_screen_is_mark_greeting_question_and_message_box() -> None:
    """The idle screen is the mark, one greeting line, the question and quiet
    suggestions (#194). Choosing a suggestion only fills the message box."""
    parser, html = _shell()
    welcome = re.search(r'<div id="welcome".*?</div>', html, re.S).group(0)
    assert "What are you working on?" in welcome and "welcome-mark" in welcome
    assert "welcome-greeting" in parser.by_id, "the greeting carries the assistant's name"
    assert "starters" in parser.by_id, "suggestions sit under the question"
    assert "hidden" in parser.by_id["starters"], "nothing shows until the client has the list"
    assert "starters-edit" in _ids_inside(parser, "welcome", "button")
    assert "data-prompt" not in html, "suggestion text comes from the preference, not markup"


# ---------------------------------------------------------------- conversation

APP_CSS = ROOT / "src" / "kova" / "web_assets" / "app.css"


def _section(css: str, start: str, end: str) -> str:
    return css[css.index(start):css.index(end)]


def test_start_screen_has_no_decorative_accent() -> None:
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
    for button in ("attach", "commands", "compose-more", "new-incognito"):
        tag = re.search(rf'<button id="{button}"[^>]*>(.*?)</button>', html).group(1)
        assert 'class="icon i-' in tag, f"#{button} should use an icon from the set"
    assert "📎" not in html


def test_every_nav_row_has_an_icon() -> None:
    """Rows without an icon break the column the others line up on."""
    _, html = _shell()
    rows = re.findall(r'<(?:button|summary)[^>]*class="nav-row"[^>]*>(.*?)</(?:button|summary)>', html)
    sidebar = [row for row in rows if "Compact now" not in row]
    assert sidebar, "expected the Settings row"
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
