"""Every theme shipped in ``design/themes`` must pass the validator CI runs.

A theme is user-facing data, so one bad file is a bug report waiting to happen.
These tests load the same validator the CLI uses and fail the build instead.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
THEME_DIR = ROOT / "design" / "themes"


def _validator():
    path = ROOT / "scripts" / "validate_theme.py"
    spec = importlib.util.spec_from_file_location("kova_validate_theme", path)
    assert spec and spec.loader, f"cannot import {path}"
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


validate_theme = _validator()
FILES = sorted(THEME_DIR.glob("*.json"))


def test_themes_are_shipped() -> None:
    assert len(FILES) >= 3, f"expected a theme set, found {[f.name for f in FILES]}"


def test_every_shipped_theme_validates() -> None:
    problems: list[str] = []
    for path in FILES:
        problems.extend(validate_theme.check_file(path))
    assert not problems, "\n".join(problems)


def test_ids_match_filenames_and_are_unique() -> None:
    seen: dict[str, str] = {}
    for path in FILES:
        theme = json.loads(path.read_text(encoding="utf-8"))
        assert theme["id"] == path.stem, f"{path.name} declares id {theme['id']!r}"
        assert theme["id"] not in seen, f"{path.name} repeats {theme['id']!r}"
        seen[theme["id"]] = path.name


def test_both_appearances_are_represented() -> None:
    appearances = {json.loads(p.read_text(encoding="utf-8"))["appearance"] for p in FILES}
    assert appearances == {"dark", "light"}, appearances
