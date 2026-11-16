#!/usr/bin/env python3
"""Generate Kova's CSS custom properties and check theme contrast and freshness.

One source of truth (`design/tokens.json`) produces the semantic custom properties
that the web app consumes, plus the TUI palette that `kova.tui.style` needs.
No third-party dependencies: this reads JSON, resolves `{...}` aliases, does WCAG
maths and writes files.

Usage:
    python scripts/build_tokens.py                 # write src/kova/web_assets/tokens.css
    python scripts/build_tokens.py --check         # exit 1 if contrast, shape or freshness fails
    python scripts/build_tokens.py --stdout        # print the CSS instead of writing

Why not Style Dictionary or Tokens Studio: this repository generates four outputs
(CSS, TUI palettes, gallery exports, docs), the alias resolver is a dozen lines, and
there is no package manager here to run them with. See
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
TOKENS_PATH = ROOT / "design" / "tokens.json"
# src/kova/web_assets is mounted at /app/assets by src/kova/web.py; a stylesheet at the
# repository root would never be served.
CSS_PATH = ROOT / "src" / "kova" / "web_assets" / "tokens.css"

THEMES = ("kova-dark", "kova-light")

# The web app switches themes with `document.documentElement.dataset.theme`, so the
# generated selectors must speak the values it actually sets ("dark" / "light"), not the
# theme ids used in the JSON. Without this the stylesheet would define nothing.
THEME_SELECTORS = {
    "kova-dark": ':root, [data-theme="dark"]',
    "kova-light": '[data-theme="light"]',
}
THEME_SCHEMES = {"kova-dark": "dark", "kova-light": "light"}

ALIAS = re.compile(r"^\{([a-z0-9_.]+)\}$", re.IGNORECASE)

# Contrast pairs that must hold, per WCAG 2.2 SC 1.4.3 (text) and SC 1.4.11 (non-text).
# An explicit manifest, not a cross product: most fg/bg combinations never co-occur.
MIN_TEXT = 4.5
MIN_LARGE = 3.0
MIN_NONTEXT = 3.0
# Decorative rules are not control boundaries, so SC 1.4.11 does not apply. They still
# have to be visible: anything under ~1.2:1 reads as a gap rather than a divider.
MIN_VISIBLE = 1.2

PAIRS: tuple[tuple[str, str, float, str], ...] = (
    # (foreground token, background token, minimum ratio, why)
    ("fg.default", "bg.base", MIN_TEXT, "body text on the app background"),
    ("fg.default", "bg.surface", MIN_TEXT, "body text on panels"),
    ("fg.default", "bg.raised", MIN_TEXT, "body text on raised chips and cards"),
    ("fg.muted", "bg.surface", MIN_TEXT, "timestamps and secondary copy"),
    ("fg.inverse", "accent.default", MIN_TEXT, "text drawn on a solid accent chip"),
    ("accent.default", "bg.base", MIN_NONTEXT, "the accent edge, icon and activity dot"),
    ("status.danger", "bg.surface", MIN_TEXT, "error text"),
    ("status.success", "bg.surface", MIN_TEXT, "success text"),
    ("status.warning", "bg.surface", MIN_TEXT, "warning text"),
    ("border.strong", "bg.surface", MIN_NONTEXT, "input and focus-ring boundaries (SC 1.4.11)"),
    ("border.subtle", "bg.surface", MIN_VISIBLE, "decorative dividers: perceptible, not a control boundary"),
    ("syntax.keyword", "bg.base", MIN_LARGE, "code keyword colour"),
    ("syntax.comment", "bg.base", MIN_LARGE, "code comment colour"),
)


# --------------------------------------------------------------------------- colour


def _srgb_to_linear(channel: float) -> float:
    return channel / 12.92 if channel <= 0.04045 else ((channel + 0.055) / 1.055) ** 2.4


def _parse_colour(value: str) -> tuple[float, float, float] | None:
    """Return linear-light sRGB in 0..1, or None for anything non-opaque.

    Only what the token file is allowed to contain: #rgb, #rrggbb, rgb()/rgba() with an
    opaque alpha. Translucent values (`accent.soft`, `bg.overlay`) have no single
    contrast ratio, so they are reported by the checker rather than guessed at.
    """
    text = value.strip()
    if text.startswith("#"):
        digits = text[1:]
        if len(digits) == 3:
            digits = "".join(c * 2 for c in digits)
        if len(digits) != 6:
            return None
        try:
            channels = [int(digits[i : i + 2], 16) / 255 for i in (0, 2, 4)]
        except ValueError:
            return None
        return tuple(_srgb_to_linear(c) for c in channels)  # type: ignore[return-value]
    if text.startswith("rgb"):
        body = text[text.index("(") + 1 : text.rindex(")")]
        parts = [p.strip() for p in body.replace("/", ",").split(",") if p.strip()]
        if len(parts) == 4:
            alpha = parts[3]
            opaque = alpha in {"1", "1.0"} or alpha.endswith("%") and float(alpha[:-1]) >= 100
            if not opaque:
                return None
        if len(parts) < 3:
            return None
        try:
            channels = [float(p.rstrip("%")) / (100 if p.endswith("%") else 255) for p in parts[:3]]
        except ValueError:
            return None
        return tuple(_srgb_to_linear(c) for c in channels)  # type: ignore[return-value]
    return None


def relative_luminance(linear_rgb: tuple[float, float, float]) -> float:
    r, g, b = linear_rgb
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast_ratio(fg_value: str, bg_value: str) -> float | None:
    """WCAG 2.x ratio `(L1 + 0.05) / (L2 + 0.05)`, or None if either side is translucent."""
    fg = _parse_colour(fg_value)
    bg = _parse_colour(bg_value)
    if fg is None or bg is None:
        return None
    l1, l2 = relative_luminance(fg), relative_luminance(bg)
    lighter, darker = max(l1, l2), min(l1, l2)
    return (lighter + 0.05) / (darker + 0.05)


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


def render_css(data: dict[str, Any] | None = None) -> str:
    data = data if data is not None else load_tokens()
    lines = [
        "/* Generated by scripts/build_tokens.py from design/tokens.json. Do not edit by hand.",
        " * Every value here is a semantic token; components must never reference a primitive.",
        ' * Selectors match the web app\'s attribute values (data-theme="dark" / "light").',
        " * See docs/DESIGN.md. */",
    ]
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

    if args.check:
        stale = check_freshness(render_css(data))
        for problem in problems:
            print(f"FAIL {problem}", file=sys.stderr)
        if stale:
            print(f"FAIL {stale}", file=sys.stderr)
        if problems or stale:
            print(f"\n{len(problems) + (1 if stale else 0)} problem(s) found.", file=sys.stderr)
            return 1
        print(
            f"tokens ok: {len(PAIRS)} contrast pairs across {len(THEMES)} themes; "
            "stylesheet matches the generator"
        )
        return 0

    css = render_css(data)
    if args.stdout:
        sys.stdout.write(css)
        return 0

    CSS_PATH.parent.mkdir(parents=True, exist_ok=True)
    CSS_PATH.write_text(css, encoding="utf-8")
    print(f"wrote {CSS_PATH.relative_to(ROOT)} ({len(css)} bytes)")
    for problem in problems:
        print(f"warn {problem}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
