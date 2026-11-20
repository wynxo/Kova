"""Static safety net for the packaged browser UI.

The web UI is plain files served from ``kova/web_assets``; nothing bundles or
type-checks it. These tests catch the two breakages that are otherwise only
visible in a browser:

* ``index.html`` pointing at a file that was renamed, deleted, or moved to a
  CDN (the app is deliberately offline and self-contained).
* ``app.js`` looking up an element id that no longer exists in the markup.
"""

from __future__ import annotations

import re
from html.parser import HTMLParser
from pathlib import Path

import pytest

ASSETS = Path(__file__).resolve().parents[1] / "src" / "kova" / "web_assets"
INDEX = ASSETS / "index.html"
APP_JS = ASSETS / "app.js"
SOUNDS_JS = ASSETS / "sounds.js"
# Formats the app deliberately does not ship: cues are synthesized at play time.
AUDIO_SUFFIXES = {".mp3", ".wav", ".ogg", ".oga", ".m4a", ".aac", ".flac", ".opus"}
PALETTE_JS = ASSETS / "palette.js"
PALETTE_CSS = ASSETS / "palette.css"
TOAST_JS = ASSETS / "toast.js"
TOAST_CSS = ASSETS / "toast.css"
REASONING_JS = ASSETS / "reasoning.js"
REASONING_CSS = ASSETS / "reasoning.css"

# Any literal colour in a component stylesheet: themes own those values.
COLOUR_LITERAL = re.compile(r"#[0-9a-fA-F]{3,8}\b|\brgba?\(|\bhsla?\(")


def without_comments(text: str) -> str:
    """Comments carry issue numbers like #183, which are not colours."""
    return re.sub(r"/\*.*?\*/", "", text, flags=re.S)


def test_markdown_module_ships_with_the_shell() -> None:
    """markdown-view.js owns reply Markdown and defines window.KovaMarkdown, so
    it must run before app.js; its stylesheet wins over the transcript shell and
    both files are part of the offline shell."""
    html = INDEX.read_text(encoding="utf-8")
    worker = (ASSETS / "sw.js").read_text(encoding="utf-8")
    assert html.index("markdown.css") > html.index("app.css"), \
        "markdown.css must win the cascade over app.css"
    assert html.index("markdown-view.js") < html.index("app.js"), \
        "markdown-view.js must define window.KovaMarkdown before app.js"
    assert "/app/assets/markdown.css" in worker and "/app/assets/markdown-view.js" in worker
    css = (ASSETS / "markdown.css").read_text(encoding="utf-8")
    assert COLOUR_LITERAL.findall(without_comments(css)) == [], \
        "markdown.css hard-codes a colour; use a token"
    for rule in ("position:sticky", ":nth-child(even)", "overflow:auto", "li.task-item"):
        assert rule in css, f"markdown.css lost {rule}"
    script = (ASSETS / "markdown-view.js").read_text(encoding="utf-8")
    assert "window.KovaMarkdown" in script
    assert "hasOpenFence" in script, "the streaming fence check moved or vanished"
    app = APP_JS.read_text(encoding="utf-8")
    assert "window.KovaMarkdown?.render" in app, "app.js does not delegate Markdown rendering"


def test_rich_markdown_assets_are_available_offline_but_not_eager_scripts() -> None:
    html = INDEX.read_text(encoding="utf-8")
    worker = (ASSETS / "sw.js").read_text(encoding="utf-8")
    for asset in ("vendor/katex.js", "vendor/mermaid.js", "diagram-frame.js"):
        assert (ASSETS / asset).is_file()
        assert f"/app/assets/{asset}" in worker
        assert f"/app/assets/{asset}" not in html


def test_popover_search_has_a_visible_keyboard_focus_ring() -> None:
    css = (ASSETS / "app.css").read_text(encoding="utf-8")
    focus = re.search(r"\.popover-search:focus-visible\s*\{([^}]+)\}", css)
    assert focus is not None, "the popover search must show keyboard focus"
    declarations = dict(re.findall(r"([\w-]+)\s*:\s*([^;]+);", focus[1]))
    assert declarations.get("outline") == "2px solid var(--accent-default)"
    assert declarations.get("outline-offset") == "1px"

# Attributes that make the browser fetch something.
FETCH_ATTRIBUTES = {"src", "href", "poster", "data"}
# A same-page anchor or an inline resource never touches the network.
INLINE_PREFIXES = ("#", "data:", "blob:", "mailto:", "javascript:")


class Page(HTMLParser):
    """Collect element ids and every fetched resource reference."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.ids: list[str] = []
        self.references: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        for name, value in attrs:
            if name == "id" and value:
                self.ids.append(value)
            if name in FETCH_ATTRIBUTES and value:
                self.references.append(value)

    handle_startendtag = handle_starttag


@pytest.fixture(scope="module")
def page() -> Page:
    parser = Page()
    parser.feed(INDEX.read_text(encoding="utf-8"))
    return parser


def local_path(reference: str) -> Path | None:
    """Map a page reference to a packaged file, or ``None`` when it is not local."""
    if reference.startswith(INLINE_PREFIXES):
        return None
    if reference.startswith(("http://", "https://", "//")):
        raise AssertionError(f"external URL in index.html: {reference}")
    route = reference.split("?", 1)[0].split("#", 1)[0]
    if route == "/app/manifest.webmanifest":
        return ASSETS / "manifest.webmanifest"
    if route in {"", "/app", "/app/"}:
        return INDEX
    if route.startswith("/app/assets/"):
        return ASSETS / route[len("/app/assets/"):]
    if route.startswith("/"):
        raise AssertionError(f"unexpected absolute reference in index.html: {reference}")
    return ASSETS / route


def test_index_has_no_external_resources(page: Page) -> None:
    for reference in page.references:
        local_path(reference)  # raises on CDN / external URLs


def test_index_references_only_existing_local_files(page: Page) -> None:
    missing = [reference for reference in page.references
               if (path := local_path(reference)) is not None and not path.is_file()]
    assert missing == [], f"index.html references missing files: {missing}"


def test_index_element_ids_are_unique(page: Page) -> None:
    duplicates = sorted({element for element in page.ids if page.ids.count(element) > 1})
    assert duplicates == [], f"duplicate element ids in index.html: {duplicates}"


def js_dollar_ids(source: str) -> set[str]:
    """Literal ids looked up through ``app.js``'s ``$`` helper.

    Dynamic lookups such as ``$(id)`` or ``$(`${name}`)`` cannot be resolved
    statically, so they are skipped instead of guessed at.
    """
    pattern = re.compile(
        r"""\$\s*\(\s*(?P<quote>['"`])(?P<id>[^'"`$]+)(?P=quote)\s*\)"""
    )
    return {match.group("id") for match in pattern.finditer(source)}


def test_sounds_load_before_the_app_and_ship_no_audio_files(page: Page) -> None:
    """The optional cues are code, not media: no audio file, no runtime fetch."""
    scripts = [reference for reference in page.references if reference.endswith(".js")]
    assert "/app/assets/sounds.js" in scripts
    assert scripts.index("/app/assets/sounds.js") < scripts.index("/app/assets/app.js"), \
        "sounds.js defines window.KovaSounds, so it must run before app.js"
    source = SOUNDS_JS.read_text(encoding="utf-8")
    assert "AudioContext" in source and "fetch(" not in source
    shipped = sorted(path.name for path in ASSETS.rglob("*")
                     if path.suffix.lower() in AUDIO_SUFFIXES)
    assert shipped == [], f"the web app ships no audio files: {shipped}"


def test_app_js_only_looks_up_ids_that_exist_in_index(page: Page) -> None:
    used = js_dollar_ids(APP_JS.read_text(encoding="utf-8"))
    assert used, "no literal $('id') lookups found; the app.js helper may have been renamed"
    missing = sorted(used - set(page.ids))
    assert missing == [], f"app.js looks up ids missing from index.html: {missing}"


def test_index_loads_the_palette_before_the_app() -> None:
    """The palette module must be defined before app.js constructs it,
    and its stylesheet must win over the shared dialog rules."""
    html = INDEX.read_text(encoding="utf-8")
    assert html.index("palette.css") > html.index("app.css")
    assert html.index("palette.js") < html.index("app.js")


def test_palette_stylesheet_has_no_literal_colour() -> None:
    text = without_comments(PALETTE_CSS.read_text(encoding="utf-8"))
    assert COLOUR_LITERAL.findall(text) == [], "palette.css hard-codes a colour; use a token"
    assert text.count("var(--") >= 20, "palette.css should take its look from tokens"


def test_palette_script_is_self_contained_and_builds_text_only() -> None:
    text = without_comments(PALETTE_JS.read_text(encoding="utf-8"))
    for forbidden in ("innerHTML", "outerHTML", "insertAdjacentHTML", "eval(",
                      "new Function", "fetch(", "http://", "https://"):
        assert forbidden not in text, f"palette.js must not use {forbidden}"
    for needed in ("'aria-activedescendant'", "'Escape'", "'Tab'", "'role', 'listbox'",
                   "textContent"):
        assert needed in text, f"palette.js lost {needed}"


def test_app_js_wires_the_palette_registry() -> None:
    text = APP_JS.read_text(encoding="utf-8")
    assert "KovaPalette?.create" in text, "app.js does not create the palette"
    assert "safe(openPalette)" in text, "the palette has no keyboard or button entry point"
    assert "paletteItems" in text, "the command registry is missing"


def test_toast_module_ships_with_the_shell() -> None:
    """toast.js defines window.KovaToast, so it must run before app.js; its
    stylesheet must win over the removed ad-hoc rules in app.css."""
    html = INDEX.read_text(encoding="utf-8")
    worker = (ASSETS / "sw.js").read_text(encoding="utf-8")
    assert html.index("toast.css") > html.index("app.css")
    assert html.index("toast.js") < html.index("app.js")
    assert "/app/assets/toast.js" in worker and "/app/assets/toast.css" in worker


def test_toast_stylesheet_uses_tokens_and_no_glow() -> None:
    text = without_comments(TOAST_CSS.read_text(encoding="utf-8"))
    assert COLOUR_LITERAL.findall(text) == [], "toast.css hard-codes a colour; use a token"
    assert not re.search(r"gradient|drop-shadow|text-shadow", text, re.I)
    for token in ("--bg-surface", "--fg-default", "--border-strong",
                  "--status-success", "--status-warning", "--status-danger",
                  "--space-3", "--radius-md", "--motion-base"):
        assert f"var({token})" in text, f"toast.css should take its look from {token}"
    assert "prefers-reduced-motion" in text, "toast.css must honour reduced motion"
    assert ":focus-visible" in text, "toast.css must keep keyboard focus visible"


def test_toast_script_is_self_contained_and_accessible() -> None:
    text = without_comments(TOAST_JS.read_text(encoding="utf-8"))
    for forbidden in ("innerHTML", "outerHTML", "insertAdjacentHTML", "eval(",
                      "new Function", "fetch(", "http://", "https://"):
        assert forbidden not in text, f"toast.js must not use {forbidden}"
    for needed in ("'aria-live'", "'aria-label'", "'role'", "'Escape'",
                   "'pointerenter'", "'focusin'", "textContent"):
        assert needed in text, f"toast.js lost {needed}"


def test_reasoning_module_ships_with_the_shell(page: Page) -> None:
    """reasoning.js defines window.KovaReasoning, so it must run before app.js;
    both it and its stylesheet are part of the offline shell."""
    html = INDEX.read_text(encoding="utf-8")
    worker = (ASSETS / "sw.js").read_text(encoding="utf-8")
    scripts = [reference for reference in page.references if reference.endswith(".js")]
    assert "/app/assets/reasoning.js" in scripts
    assert scripts.index("/app/assets/reasoning.js") < scripts.index("/app/assets/app.js")
    assert html.index("reasoning.css") > html.index("app.css")
    assert "/app/assets/reasoning.js" in worker and "/app/assets/reasoning.css" in worker


def test_reasoning_stylesheet_uses_tokens_and_reduced_motion() -> None:
    text = without_comments(REASONING_CSS.read_text(encoding="utf-8"))
    assert COLOUR_LITERAL.findall(text) == [], "reasoning.css hard-codes a colour; use a token"
    for token in ("--fg-muted", "--fg-faint", "--border-strong", "--bg-raised",
                  "--accent-default", "--space-1", "--space-2", "--radius-sm",
                  "--type-size-label", "--motion-fast"):
        assert f"var({token})" in text, f"reasoning.css should take its look from {token}"
    assert "prefers-reduced-motion" in text, "reasoning.css must honour reduced motion"
    assert ":focus-visible" in text, "reasoning.css must keep keyboard focus visible"
    assert "[data-reasoning=\"hidden\"] .reasoning" in text, \
        "the setting must hide rows already on screen"


def test_reasoning_script_folds_text_and_never_builds_markup() -> None:
    text = without_comments(REASONING_JS.read_text(encoding="utf-8"))
    for forbidden in ("innerHTML", "outerHTML", "insertAdjacentHTML", "eval(",
                      "new Function"):
        assert forbidden not in text, f"reasoning.js must not use {forbidden}"
    for needed in ("textContent", "'collapsed'", "'expanded'", "'hidden'",
                   "'/preferences'", "Thought for", "window.KovaReasoning"):
        assert needed in text, f"reasoning.js lost {needed}"
    assert "localStorage" in text, "the setting must paint before the server answers"
    assert "LEGACY" in text, "the first shown/hidden release must keep reading as collapsed"


def test_app_js_delegates_reasoning_rows_to_the_module() -> None:
    text = APP_JS.read_text(encoding="utf-8")
    assert "window.KovaReasoning?.row" in text, \
        "app.js must ask the module for the folded reasoning row"
    assert "activityText.textContent = 'Thinking…'" in text, \
        "the quiet thinking row must stay while reasoning streams"
    assert "details', 'thinking'" not in text, \
        "app.js must not build reasoning markup itself"


def test_app_js_posts_messages_through_the_toast_component() -> None:
    text = APP_JS.read_text(encoding="utf-8")
    assert "globalThis.KovaToast" in text, "app.js never reaches the toast component"
    assert "toastError(" in text and "toastSuccess(" in text, "severities are unwired"
    assert "severity: 'error'" in text, "errors ask the component for the sticky severity"
    assert "label: 'Undo'" in text, "the action button has no real user"
    assert "sticky: true" in text, "reminders must not expire"
