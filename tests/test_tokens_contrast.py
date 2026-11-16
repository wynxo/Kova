"""Guard-rails for the design token layer.

These run on every `uv run pytest -q`, so a theme that breaks WCAG contrast, loses a
token or drops the TUI palette fails CI rather than shipping. No browser, no
dependency: a couple of hundred milliseconds of arithmetic.

See docs/DESIGN.md and docs/research/design-ui-2-testing-and-conversation.md.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BUILD = ROOT / "scripts" / "build_tokens.py"
INDEX = ROOT / "src" / "kova" / "web_assets" / "index.html"


def _load_build():
    spec = importlib.util.spec_from_file_location("kova_build_tokens", BUILD)
    assert spec and spec.loader, f"cannot import {BUILD}"
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


build = _load_build()
tokens = build.load_tokens()


def test_theme_files_exist() -> None:
    for theme in build.THEMES:
        assert theme in tokens["semantic"], f"{theme} missing from design/tokens.json"


def test_every_theme_has_the_same_semantic_tokens() -> None:
    problems = build.check_shape(tokens)
    assert not problems, "\n".join(problems)


def test_contrast_pairs_pass() -> None:
    failures = build.check_contrast(tokens)
    assert not failures, "\n".join(failures)


def test_alias_cycles_are_rejected() -> None:
    broken = {"a": {"$value": "{b}"}, "b": {"$value": "{a}"}}
    try:
        build.resolve("{a}", broken)
    except ValueError as error:
        assert "cycle" in str(error)
    else:  # pragma: no cover - the resolver must never return on a cycle
        raise AssertionError("alias cycle was not detected")


def test_unknown_alias_is_rejected() -> None:
    try:
        build.resolve("{does.not.exist}", {"a": {"$value": "#fff"}})
    except KeyError as error:
        assert "does.not.exist" in str(error)
    else:  # pragma: no cover
        raise AssertionError("unknown alias was not detected")


def test_contrast_maths_matches_wcag_reference() -> None:
    # Reference values from the WCAG 2.2 definition of the contrast ratio.
    assert _approx(build.contrast_ratio("#000000", "#ffffff"), 21.0)
    assert _approx(build.contrast_ratio("#ffffff", "#ffffff"), 1.0)
    assert _approx(build.contrast_ratio("#777777", "#ffffff"), 4.48, abs_tol=0.02)


def test_translucent_values_are_unmeasurable_not_silently_passed() -> None:
    assert build.contrast_ratio("rgba(0, 0, 0, 0.5)", "#ffffff") is None
    assert build.contrast_ratio("#000", "rgba(255, 255, 255, 0.2)") is None


def test_css_is_generated_and_theme_agnostic() -> None:
    css = build.render_css(tokens)
    # The web app sets data-theme="dark" / "light"; anything else defines nothing.
    assert ':root, [data-theme="dark"]' in css
    assert '[data-theme="light"]' in css
    assert "kova-dark" not in css and "kova-light" not in css
    # The whole point of the semantic layer: identical property names in every theme.
    for name in ("--fg-default", "--bg-base", "--accent-default"):
        assert css.count(f"{name}:") >= 2, f"{name} missing from a theme block"
        assert "--dark-" not in css and "--light-" not in css


def test_tui_palettes_are_sixteen_roles() -> None:
    for theme in build.THEMES:
        palette = build.theme_tokens(theme, tokens).get("terminal")
        assert isinstance(palette, list), f"{theme} has no terminal palette"
        assert len(palette) == 16, f"{theme} terminal palette has {len(palette)} roles"
        assert all(str(c).startswith("#") for c in palette)


def test_stylesheet_lands_where_the_web_app_serves_it() -> None:
    # src/kova/web_assets is mounted at /app/assets by src/kova/web.py, so the file has
    # to live there; a stylesheet at the repository root would never be served.
    assert build.CSS_PATH.parts[-2:] == ("web_assets", "tokens.css")
    assert build.CSS_PATH.parts[-4:-1] == ("src", "kova", "web_assets")
    assert "src" in build.CSS_PATH.parts and "kova" in build.CSS_PATH.parts


def test_stylesheet_is_linked_before_app_css() -> None:
    """`app.css` still owns the shipped palette, so it has to win the cascade.

    Two names (`--accent-soft`, `--border-strong`) are defined by both files with
    different values. The app's declaration wins today because `app.css` is linked
    second and, for the light theme, is more specific. Reordering these links would
    silently repaint the UI, so the order is asserted instead of assumed.
    """
    html = INDEX.read_text(encoding="utf-8")
    assert html.index("/app/assets/tokens.css") < html.index("/app/assets/app.css")


def _approx(value: float | None, expected: float, abs_tol: float = 1e-6) -> bool:
    return value is not None and abs(value - expected) <= abs_tol
