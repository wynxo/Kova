"""The committed stylesheet must be exactly what the generator produces.

``build_tokens.py --check`` validates contrast and shape, not freshness: a token
edit without a rerun used to pass every gate while the app served the old palette
(#232). This closes that hole with the cheapest assertion that can catch it.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _build():
    path = ROOT / "scripts" / "build_tokens.py"
    spec = importlib.util.spec_from_file_location("kova_build_tokens", path)
    assert spec and spec.loader, f"cannot import {path}"
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


build = _build()


def test_generated_stylesheet_exists_where_the_app_serves_it() -> None:
    assert build.CSS_PATH.exists(), f"{build.CSS_PATH} is missing"


def test_committed_stylesheet_matches_the_generator() -> None:
    expected = build.render_css(build.load_tokens())
    actual = build.CSS_PATH.read_text(encoding="utf-8")
    assert actual == expected, (
        f"{build.CSS_PATH.name} is stale: run `python scripts/build_tokens.py` and commit the result"
    )
