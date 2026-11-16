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
