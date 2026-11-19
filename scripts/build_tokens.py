#!/usr/bin/env python3
"""Generate Kova's CSS custom properties and check theme contrast and freshness.

One source of truth (`design/tokens.json`) produces the semantic custom properties
that the web app consumes, plus the TUI palette that `kova.tui.style` needs.
No third-party dependencies: this reads JSON, resolves `{...}` aliases, does WCAG
maths and writes files.

Usage:
    python scripts/build_tokens.py                 # write tokens.css and tokens.json
    python scripts/build_tokens.py --check         # exit 1 if contrast, shape or freshness fails
    python scripts/build_tokens.py --stdout        # print the CSS instead of writing

Why not Style Dictionary or Tokens Studio: this repository generates several outputs
(CSS, the server's token data, the TUI palettes and docs), the alias resolver is a dozen
lines, and there is no package manager here to run them with. See
docs/research/design-ui-1-tokens-and-themes.md.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from itertools import zip_longest
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
# Keep this runnable with plain Python from a source checkout.
sys.path.insert(0, str(ROOT / "src"))
from kova.theme_contrast import PAIRS, contrast_ratio

TOKENS_PATH = ROOT / "design" / "tokens.json"
# src/kova/web_assets is mounted at /app/assets by src/kova/web.py; a stylesheet at the
# repository root would never be served.
CSS_PATH = ROOT / "src" / "kova" / "web_assets" / "tokens.css"
# The server needs the same values as data, so a theme file can be merged over its base
# (kova.themes). It sits next to the module so it ships in the package.
TOKENS_JSON_PATH = ROOT / "src" / "kova" / "tokens.json"
EXAMPLE_THEMES = ROOT / "design" / "themes"
GALLERY_JSON_PATH = ROOT / "src" / "kova" / "theme_gallery.json"

THEMES = ("kova-dark", "kova-light")

# The web app switches themes with `document.documentElement.dataset.theme`, so the
# generated selectors must speak the values it actually sets ("dark" / "light"), not the
# theme ids used in the JSON. Without this the stylesheet would define nothing.
THEME_SELECTORS = {
    "kova-dark": ':root, [data-theme="dark"]',
    "kova-light": '[data-theme="light"]',
}
THEME_SCHEMES = {"kova-dark": "dark", "kova-light": "light"}

# Theme-independent groups in tokens.json, emitted once on :root as
# --space-*, --radius-*, --motion-* and --type-*.
SCALES = ("space", "radius", "motion", "type")

ALIAS = re.compile(r"^\{([a-z0-9_.]+)\}$", re.IGNORECASE)

# ---------------------------------------------------------------------------- tokens


def load_tokens(path: Path = TOKENS_PATH) -> dict[str, Any]:
    with path.open(encoding="utf-8") as handle:
        return json.load(handle)


def resolve(value: Any, data: dict[str, Any], _seen: frozenset[str] = frozenset()) -> Any:
    """Follow `{group.token}` aliases. Raises on cycles and unknown targets."""
    if not isinstance(value, str):
        return value
    match = ALIAS.match(value.strip())
    if not match:
        return value
    target = match.group(1)
    if target in _seen:
        raise ValueError(f"alias cycle at {target!r}")
    node: Any = data
    for part in target.split("."):
        if not isinstance(node, dict) or part not in node:
            raise KeyError(f"unknown token alias {target!r}")
        node = node[part]
    if isinstance(node, dict) and "$value" in node:
        node = node["$value"]
    return resolve(node, data, _seen | {target})


def flatten(group: dict[str, Any], data: dict[str, Any], prefix: str = "") -> dict[str, Any]:
    """Flatten a token group to `{"fg.default": "#e8e6f0"}` with aliases resolved."""
    out: dict[str, Any] = {}
    for key, node in group.items():
        if key.startswith("$"):
            continue
        name = f"{prefix}.{key}" if prefix else key
        if isinstance(node, dict) and "$value" in node:
            out[name] = resolve(node["$value"], data)
        elif isinstance(node, dict):
            out.update(flatten(node, data, name))
    return out


def theme_tokens(theme: str, data: dict[str, Any] | None = None) -> dict[str, Any]:
    data = data if data is not None else load_tokens()
    return flatten(data["semantic"][theme], data)


# ------------------------------------------------------------------------------- css


def _css_name(token: str) -> str:
    """`fg.default` -> `--fg-default`.

    Deliberately theme-free: every theme block assigns the same custom property names,
    so a component says `var(--fg-default)` and never learns which theme is active.
    """
    return "--" + token.replace(".", "-").replace("_", "-")


def _css_value(value: Any) -> str:
    """A token value as CSS: cubic-bezier lists become `cubic-bezier(...)`."""
    if isinstance(value, list):
        return "cubic-bezier(" + ", ".join(f"{number:g}" for number in value) + ")"
    return str(value)


def scale_tokens(data: dict[str, Any] | None = None) -> dict[str, Any]:
    """The theme-independent scales (space, radius, motion, type), flattened.

    These are the same in every theme, so they are emitted once on `:root`. A theme
    file cannot override them; density and type settings will swap them later.
    """
    data = data if data is not None else load_tokens()
    out: dict[str, Any] = {}
    for group in SCALES:
        out.update(flatten(data.get(group, {}), data, group))
    return out


def render_css(data: dict[str, Any] | None = None) -> str:
    data = data if data is not None else load_tokens()
    lines = [
        "/* Generated by scripts/build_tokens.py from design/tokens.json. Do not edit by hand.",
        " * Every value here is a semantic token; components must never reference a primitive.",
        ' * Selectors match the web app\'s attribute values (data-theme="dark" / "light").',
        " * See docs/DESIGN.md. */",
    ]
    scales = scale_tokens(data)
    if scales:
        lines.append("")
        lines.append(":root {")
        for token, value in scales.items():
            lines.append(f"  {_css_name(token)}: {_css_value(value)};")
        lines.append("}")
    for theme in THEMES:
        tokens = theme_tokens(theme, data)
        lines.append("")
        lines.append(f"{THEME_SELECTORS[theme]} {{")
        lines.append(f"  color-scheme:{THEME_SCHEMES[theme]};")
        for token, value in tokens.items():
            if isinstance(value, list):
                continue
            lines.append(f"  {_css_name(token)}: {value};")
        lines.append("}")
    return "\n".join(lines) + "\n"


def render_tokens_json(data: dict[str, Any] | None = None) -> str:
    """The same tokens as JSON, for the runtime theme engine (``kova.themes``).

    The stylesheet serves the browser; the server also needs the palette as values
    so a user theme can be merged over its base (#174). Both come from this one
    run, so they cannot disagree.
    """
    data = data if data is not None else load_tokens()
    payload: dict[str, Any] = {
        "generated": "scripts/build_tokens.py",
        "source": "design/tokens.json",
        "themes": {},
    }
    for theme in THEMES:
        tokens = theme_tokens(theme, data)
        payload["themes"][theme] = {
            "appearance": THEME_SCHEMES[theme],
            "tokens": {name: value for name, value in tokens.items()
                       if not isinstance(value, list)},
            "terminal": tokens.get("terminal", []),
        }
    return json.dumps(payload, indent=2) + "\n"


def render_gallery_json() -> str:
    """Bundle the example theme documents as package data for offline installs."""
    documents = {
        path.stem: json.loads(path.read_text(encoding="utf-8"))
        for path in sorted(EXAMPLE_THEMES.glob("*.json"))
    }
    return json.dumps(documents, indent=2, ensure_ascii=False) + "\n"


# --------------------------------------------------------------------------- checks


def check_contrast(data: dict[str, Any] | None = None) -> list[str]:
    """Return a list of human-readable failures; empty means every declared pair passes."""
    data = data if data is not None else load_tokens()
    failures: list[str] = []
    for theme in THEMES:
        tokens = theme_tokens(theme, data)
        for fg, bg, minimum, why in PAIRS:
            if fg not in tokens or bg not in tokens:
                failures.append(f"{theme}: missing token for pair {fg} on {bg}")
                continue
            ratio = contrast_ratio(str(tokens[fg]), str(tokens[bg]))
            if ratio is None:
                failures.append(f"{theme}: {fg} on {bg} is translucent, cannot measure ({why})")
            elif ratio < minimum:
                failures.append(
                    f"{theme}: {fg} on {bg} = {ratio:.2f}:1, below {minimum}:1 ({why})"
                )
    return failures


def check_shape(data: dict[str, Any] | None = None) -> list[str]:
    """Structural guard-rails that keep the semantic layer honest."""
    data = data if data is not None else load_tokens()
    problems: list[str] = []
    themes = set(data.get("semantic", {}))
    missing = set(THEMES) - themes
    if missing:
        problems.append(f"missing theme(s): {', '.join(sorted(missing))}")
    baseline = set(theme_tokens("kova-dark", data))
    for theme in sorted(themes & set(THEMES)):
        keys = set(theme_tokens(theme, data))
        for absent in sorted(baseline - keys):
            problems.append(f"{theme}: missing token {absent}")
    return problems


def _display(path: Path) -> str:
    """Repository-relative path when possible, so the message is short and shareable."""
    try:
        return str(path.relative_to(ROOT))
    except ValueError:
        return str(path)


def check_freshness(css: str, path: Path | None = None) -> str | None:
    """Return one line explaining why the committed stylesheet differs from `css`.

    Contrast and shape pass just as happily against a stale sheet, so `--check`
    also regenerates in memory and compares bytes: editing a token and forgetting
    to re-run this script must fail, not silently ship the old palette (#232).
    """
    path = CSS_PATH if path is None else path
    if not path.exists():
        return f"{_display(path)} is missing: run `python scripts/build_tokens.py`"
    committed = path.read_text(encoding="utf-8")
    if committed == css:
        return None
    # keepends so a trailing-newline-only difference still counts as a diff
    old = committed.splitlines(keepends=True)
    new = css.splitlines(keepends=True)
    differing = [
        number
        for number, (before, after) in enumerate(zip_longest(old, new), start=1)
        if before != after
    ]
    return (
        f"{_display(path)} is stale ({len(differing)} line(s) differ, "
        f"first at line {differing[0]}): run `python scripts/build_tokens.py` "
        "and commit the result"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--check",
        action="store_true",
        help="verify contrast, shape and freshness, write nothing",
    )
    parser.add_argument("--stdout", action="store_true", help="print CSS instead of writing it")
    args = parser.parse_args(argv)

    data = load_tokens()
    problems = check_shape(data) + check_contrast(data)
    css = render_css(data)
    tokens_json = render_tokens_json(data)
    gallery_json = render_gallery_json()

    if args.check:
        stale = [
            line
            for line in (
                check_freshness(css, CSS_PATH),
                check_freshness(tokens_json, TOKENS_JSON_PATH),
                check_freshness(gallery_json, GALLERY_JSON_PATH),
            )
            if line
        ]
        for problem in problems:
            print(f"FAIL {problem}", file=sys.stderr)
        for problem in stale:
            print(f"FAIL {problem}", file=sys.stderr)
        if problems or stale:
            print(f"\n{len(problems) + len(stale)} problem(s) found.", file=sys.stderr)
            return 1
        print(
            f"tokens ok: {len(PAIRS)} contrast pairs across {len(THEMES)} themes; "
            "stylesheet matches the generator, as do the token data and theme gallery"
        )
        return 0

    if args.stdout:
        sys.stdout.write(css)
        return 0

    for path, text in ((CSS_PATH, css), (TOKENS_JSON_PATH, tokens_json),
                       (GALLERY_JSON_PATH, gallery_json)):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        print(f"wrote {path.relative_to(ROOT)} ({len(text)} bytes)")
    for problem in problems:
        print(f"warn {problem}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
