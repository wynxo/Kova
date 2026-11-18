"""Text size, density, font and content width (#179, #205): one attribute each.

``typography.js`` resolves the Text size / Density / Font / Content width
preferences — shared with the server's UI preferences document — onto
``<html data-text-size data-density data-font data-chat-width>``, and one block
in ``app.css`` swaps the type, space, font and column values for every
component. ``default``, ``inter`` and ``normal`` write no override, so an
untouched install keeps the shipped token values. These checks run in the normal
suite and need no browser.
"""

from __future__ import annotations

import re
from pathlib import Path

ASSETS = Path(__file__).resolve().parents[1] / "src" / "kova" / "web_assets"
INDEX = ASSETS / "index.html"
SCRIPT = ASSETS / "typography.js"
STYLESHEET = ASSETS / "app.css"
COLOUR_LITERAL = re.compile(r"#[0-9a-fA-F]{3,8}\b|\brgba?\(")
TYPE_TOKENS = ("--type-size-label", "--type-size-body", "--type-size-body-comfortable",
               "--type-size-title", "--type-size-heading")


def without_comments(text: str) -> str:
    """Comments carry issue numbers like #179, which are not colours."""
    return re.sub(r"/\*.*?\*/", "", text, flags=re.S)


def rules() -> str:
    """The typography block: from its first attribute selector to the end of the file."""
    text = without_comments(STYLESHEET.read_text(encoding="utf-8"))
    return text[text.index('[data-text-size="small"]'):]


def block(selector: str) -> str:
    """One rule's declarations, without the selector."""
    found = re.search(re.escape(selector) + r"\s*\{([^}]*)\}", rules())
    assert found, f"the typography block lost its {selector} rule"
    return found.group(1)


def test_the_script_ships_with_the_page_before_the_body() -> None:
    page = INDEX.read_text(encoding="utf-8")
    worker = (ASSETS / "sw.js").read_text(encoding="utf-8")
    tag = re.search(r'<script[^>]*src="/app/assets/typography\.js"[^>]*>', page)
    assert tag, "index.html loads typography.js"
    assert "defer" not in tag.group(0) and "async" not in tag.group(0), \
        "typography.js paints <html> before the first frame"
    assert page.index("tokens.css") < tag.start() < page.index("<body")
    assert "/app/assets/typography.js" in worker, \
        "typography.js belongs in the offline shell"


def test_text_size_swaps_the_whole_type_scale() -> None:
    for selector in ('[data-text-size="small"]', '[data-text-size="large"]'):
        declarations = block(selector)
        for token in TYPE_TOKENS:
            assert f"{token}:" in declarations, f"{selector} should scale {token}"


def test_no_text_size_drops_below_the_twelve_pixel_floor() -> None:
    text = rules()[:rules().index('[data-density="compact"]')]
    sizes = [int(value) for value in re.findall(r"--type-size-[a-z-]+:(\d+)px", text)]
    assert sizes, "text size must set a size"
    assert min(sizes) >= 12, "docs/DESIGN.md: never below 12px"


def test_default_writes_no_override() -> None:
    text = rules()
    assert '[data-text-size="default"]' not in text, \
        "default is the shipped scale, so it needs no rule"
    assert '[data-density="default"]' not in text
    assert '[data-font="inter"]' not in text
    assert '[data-chat-width="normal"]' not in text, \
        "normal is the shipped column, so it needs no rule"


def test_content_width_moves_the_shared_column() -> None:
    """Content width (#205): one attribute moves the conversation and composer."""
    narrow = int(re.search(r"--column:(\d+)px", block('[data-chat-width="narrow"]')).group(1))
    wide = int(re.search(r"--column:(\d+)px", block('[data-chat-width="wide"]')).group(1))
    shipped = int(re.search(r"--column:(\d+)px",
                            without_comments(STYLESHEET.read_text(encoding="utf-8"))).group(1))
    assert 0 < narrow < shipped < wide, \
        "narrow must read narrower than the shipped column, and wide broader"


def test_density_swaps_the_space_tokens_the_conversation_uses() -> None:
    for selector in ('[data-density="compact"]', '[data-density="comfortable"]'):
        declarations = block(selector)
        # The message list, sidebar rows and composer all take their padding and
        # gaps from these tokens; one block therefore carries every one of them.
        for token in ("--space-1", "--space-2", "--space-3", "--space-4", "--space-6"):
            assert f"{token}:" in declarations, f"{selector} should change {token}"
    compact = dict(re.findall(r"(--space-[0-9-]+):(\d+)px", block('[data-density="compact"]')))
    comfortable = dict(re.findall(r"(--space-[0-9-]+):(\d+)px", block('[data-density="comfortable"]')))
    for token in ("--space-2", "--space-3", "--space-4"):
        assert int(compact[token]) < int(comfortable[token]), \
            f"{token} should be tighter under compact than comfortable"


def test_font_switches_the_stacks_and_leaves_chat_configurable() -> None:
    system = block('[data-font="system"]')
    assert "--font:" in system
    assert "Inter" not in system, "the system font option must not name the bundled face"
    assert "system-ui" in system
    assert "--font-chat:var(--mono)" in block('[data-font="mono"]')
    css = without_comments(STYLESHEET.read_text(encoding="utf-8"))
    assert ":root {" in css and "--font-chat:var(--font)" in css, \
        "chat text follows the UI font until another is chosen"
    replies = without_comments((ASSETS / "markdown.css").read_text(encoding="utf-8"))
    assert re.search(r"^\.markdown \{ font-family:var\(--font-chat\);", replies, re.MULTILINE), \
        "a reply is set in the chat font (reply styles live in markdown.css)"
    assert "font-family:var(--font-chat)" in re.search(r"^\.message\.user \.message-body \{[^}]*\}",
                                                       css, re.MULTILINE).group(0), \
        "your own message is set in the chat font"


def test_body_text_is_a_token_not_a_literal() -> None:
    css = without_comments(STYLESHEET.read_text(encoding="utf-8"))
    assert re.search(r"^\s*font:14px/", css, re.MULTILINE) is None, \
        "the base font size should come from --type-size-body"
    assert "font:var(--type-size-body)/1.5 var(--font)" in css


def test_the_typography_block_is_token_only() -> None:
    assert COLOUR_LITERAL.findall(rules()) == [], \
        "the typography block hard-codes a colour; use a token"


def test_the_script_reads_the_shared_preferences_and_paints_every_attribute() -> None:
    text = without_comments(SCRIPT.read_text(encoding="utf-8"))
    for needed in ("'data-text-size'", "'data-density'", "'data-font'", "'data-chat-width'",
                   "'small'", "'large'", "'compact'", "'comfortable'",
                   "'inter'", "'system'", "'mono'", "'narrow'", "'wide'",
                   'chatWidth', 'setChatWidth', "'/preferences'", "window.KovaTypography"):
        assert needed in text, f"typography.js lost {needed}"
    assert "localStorage" in text, "the choices must paint before the server answers"
