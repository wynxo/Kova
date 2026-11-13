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


def test_app_js_only_looks_up_ids_that_exist_in_index(page: Page) -> None:
    used = js_dollar_ids(APP_JS.read_text(encoding="utf-8"))
    assert used, "no literal $('id') lookups found; the app.js helper may have been renamed"
    missing = sorted(used - set(page.ids))
    assert missing == [], f"app.js looks up ids missing from index.html: {missing}"
