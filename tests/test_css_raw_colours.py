"""No served stylesheet may carry a colour of its own (#173).

`src/kova/web_assets/tokens.css` is the generated palette and the only file
allowed to spell a colour out. Every component reads a semantic role from it —
`--bg-*`, `--fg-*`, `--border-*`, `--accent-*`, `--status-*` — so a theme only
has to move the semantic layer (docs/DESIGN.md, "One colour layer").

These guards run in the normal `uv run pytest -q`, no browser needed. They fail
on a hex colour anywhere in a component stylesheet, and on a component
stylesheet that scopes itself to a theme instead of the semantic roles.
Functional and named colours (`rgb()`, `oklch()`, `rebeccapurple`) are already
guarded declaration by declaration in tests/test_semantic_css.py.
"""

from __future__ import annotations

import re
from pathlib import Path

ASSETS = Path(__file__).resolve().parents[1] / "src" / "kova" / "web_assets"
PALETTE = "tokens.css"

# Nothing is exempt today. If a value ever has to be, add it here with the
# file name and a comment saying why it cannot be a token.
ALLOWED: dict[str, set[str]] = {}

# `#rgb`, `#rrggbb` and the four-channel spellings, but never a fragment in a
# data URL (`%23…`) or the tail of an identifier.
HEX = re.compile(r"(?<![\w&\\.-])#[0-9a-fA-F]{3,8}(?![\w-])")
THEME_SELECTOR = re.compile(r"\[data-theme[^\]]*\]")


def stylesheets() -> list[Path]:
    """Component stylesheets: everything served except the generated palette."""
    return [
        path
        for path in sorted(ASSETS.rglob("*.css"))
        if path.name != PALETTE and "vendor" not in path.relative_to(ASSETS).parts
    ]


def without_comments(css: str) -> str:
    """Drop comments: issue references like `(#198)` are not colours."""
    return re.sub(r"/\*.*?\*/", "", css, flags=re.S)


def test_component_stylesheets_have_no_raw_colours() -> None:
    offenders = []
    for path in stylesheets():
        css = without_comments(path.read_text(encoding="utf-8"))
        for match in HEX.finditer(css):
            if match.group(0) in ALLOWED.get(path.name, set()):
                continue
            line = css.count("\n", 0, match.start()) + 1
            offenders.append(f"{path.name}:{line}: {match.group(0)}")
    assert not offenders, (
        "colours belong in design/tokens.json, not in a component stylesheet:\n"
        + "\n".join(offenders)
    )


def test_no_component_scopes_itself_to_a_theme() -> None:
    """tokens.css owns `[data-theme]`; components only know semantic roles."""
    for path in stylesheets():
        css = without_comments(path.read_text(encoding="utf-8"))
        assert not THEME_SELECTOR.search(css), (
            f"{path.name} scopes a rule to a theme; move the value into tokens.css"
        )


def test_the_hex_guard_catches_colours_and_ignores_lookalikes() -> None:
    for colour in ("#fff", "#ffffff", "#ffffff80", "border:1px solid #ABC;"):
        assert HEX.search(colour), colour
    for not_a_colour in (
        "url(\"data:image/svg+xml,%3Csvg stroke='%23000'/%3E\")",
        "#sidebar",
        "#provider-details",
        "var(--fg-default)",
        "color-mix(in srgb, var(--status-danger) 11%, transparent)",
    ):
        assert not HEX.search(not_a_colour), not_a_colour
