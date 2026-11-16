"""Every example theme in the repository must validate.

`design/themes/` holds the themes documented in `docs/THEMES.md`. They are copied by
hand, so they rot: a token gets renamed and the example silently stops loading. This
test makes an example a checked artefact rather than a snippet in prose.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
VALIDATOR = ROOT / "scripts" / "validate_theme.py"
EXAMPLES = sorted((ROOT / "design" / "themes").glob("*.json"))


def _load_validator():
    spec = importlib.util.spec_from_file_location("kova_validate_theme_examples", VALIDATOR)
    assert spec and spec.loader, f"cannot import {VALIDATOR}"
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


validator = _load_validator()


def test_there_is_at_least_one_example() -> None:
    assert EXAMPLES, "expected an example theme in design/themes/"


def test_every_example_validates() -> None:
    failures: list[str] = []
    for path in EXAMPLES:
        failures.extend(validator.check_file(path))
    assert not failures, "\n".join(failures)


def test_every_example_passes_contrast() -> None:
    for path in EXAMPLES:
        theme = validator.json.loads(path.read_text(encoding="utf-8"))
        assert validator.check_theme(theme) == [], path.name


def test_every_example_id_matches_its_filename() -> None:
    # Two themes with the same id would collide in settings; the filename is the id.
    for path in EXAMPLES:
        theme = validator.json.loads(path.read_text(encoding="utf-8"))
        assert theme["id"] == path.stem, f"{path.name} has id {theme['id']!r}"
