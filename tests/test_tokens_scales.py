"""Space, radius, motion and type tokens reach the stylesheet the app loads.

docs/DESIGN.md tells components to write `var(--space-4)`, `var(--radius-md)` and
`var(--motion-fast)`. Those groups live in `design/tokens.json`, but the generator
used to emit colours only, so every such reference resolved to nothing. These tests
keep the documented names real, and keep app.css from using a scale token that the
generator does not produce.
"""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
APP_CSS = ROOT / "src" / "kova" / "web_assets" / "app.css"
SCALE_REFERENCE = re.compile(r"var\((--(?:space|radius|motion|type)-[a-z0-9-]+)\)")


def _build():
    path = ROOT / "scripts" / "build_tokens.py"
    spec = importlib.util.spec_from_file_location("kova_build_tokens_scales", path)
    assert spec and spec.loader, f"cannot import {path}"
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


build = _build()


def _declared(css: str) -> set[str]:
    return set(re.findall(r"^\s*(--[a-z0-9-]+):", css, re.MULTILINE))


def test_documented_scale_tokens_are_emitted() -> None:
    declared = _declared(build.render_css(build.load_tokens()))
    for name in (
        "--space-0-5", "--space-1", "--space-4", "--space-12",
        "--radius-sm", "--radius-md", "--radius-lg", "--radius-pill",
        "--motion-fast", "--motion-base", "--motion-slow", "--motion-ease-standard",
        "--type-size-body", "--type-size-label", "--type-measure-chat",
    ):
        assert name in declared, f"{name} is documented in DESIGN.md but not generated"


def test_easing_is_valid_css() -> None:
    css = build.render_css(build.load_tokens())
    assert "--motion-ease-standard: cubic-bezier(0.2, 0, 0, 1);" in css


def test_scales_are_theme_independent() -> None:
    """Scales sit in their own :root block, before any theme block."""
    css = build.render_css(build.load_tokens())
    scales = css.index("--space-1:")
    first_theme = css.index('[data-theme="dark"]')
    assert scales < first_theme
    assert css.count("--space-1:") == 1


def test_app_css_only_uses_generated_scale_tokens() -> None:
    declared = _declared(build.render_css(build.load_tokens()))
    used = set(SCALE_REFERENCE.findall(APP_CSS.read_text(encoding="utf-8")))
    assert used, "app.css should take its spacing from the token scale"
    assert used <= declared, f"app.css uses undefined tokens: {sorted(used - declared)}"
