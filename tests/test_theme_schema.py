"""Theme files are untrusted input; these tests are the fence around them.

The validators run on files a user downloads or a gallery serves, so the tests focus
on what must be *rejected*: unknown keys, code masquerading as a colour, and themes that
quietly make text unreadable.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
VALIDATOR = ROOT / "scripts" / "validate_theme.py"


def _load_validator():
    spec = importlib.util.spec_from_file_location("kova_validate_theme", VALIDATOR)
    assert spec and spec.loader, f"cannot import {VALIDATOR}"
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


validator = _load_validator()
SCHEMA = validator.load_schema()


def _errors(theme: dict) -> list[str]:
    return validator.validate(theme, SCHEMA, SCHEMA)


def _theme(**overrides):
    base = {
        "id": "midnight-teal",
        "name": "Midnight Teal",
        "version": "1.0.0",
        "appearance": "dark",
        "extends": "kova-dark",
    }
    base.update(overrides)
    return base


def test_a_minimal_theme_is_valid() -> None:
    assert _errors(_theme()) == []


def test_unknown_top_level_keys_are_rejected() -> None:
    # A theme is not a place to put CSS.
    problems = _errors(_theme(css="body { display: none }"))
    assert any("css" in p for p in problems), problems


def test_urls_are_not_colours() -> None:
    theme = _theme(tokens={"bg": {"base": "url(https://example.com/pixel.png)"}})
    problems = _errors(theme)
    assert any("tokens.bg.base" in p for p in problems), problems


def test_named_colours_are_rejected() -> None:
    problems = _errors(_theme(tokens={"fg": {"default": "rebeccapurple"}}))
    assert any("tokens.fg.default" in p for p in problems), problems


def test_hex_lengths_follow_css_for_tokens_and_terminal() -> None:
    for invalid in ("#abcde", "#abcdef0"):
        assert _errors(_theme(tokens={"fg": {"default": invalid}})), invalid
        assert _errors(_theme(terminal=[invalid] * 16)), invalid
    for valid in ("#abc", "#abcd", "#abcdef", "#abcdef01"):
        assert _errors(_theme(tokens={"fg": {"default": valid}})) == [], valid
        assert _errors(_theme(terminal=[valid] * 16)) == [], valid


def test_primitive_overrides_are_rejected() -> None:
    # `tokens` accepts semantic groups only; a primitive ramp is not a group name.
    problems = _errors(_theme(tokens={"primitive": {"orange": {"500": "#ff0000"}}}))
    assert problems, "a theme must not be able to rewrite the primitive ramps"


def test_terminal_palette_must_be_sixteen_roles() -> None:
    problems = _errors(_theme(terminal=["#000000"]))
    assert any("terminal" in p for p in problems), problems
    assert _errors(_theme(terminal=["#000000"] * 16)) == []


def test_ids_are_lowercase_and_hyphenated() -> None:
    assert _errors(_theme(id="Bad_ID")) != []
    assert _errors(_theme(id="ok-2")) == []


def test_a_theme_that_hides_its_text_fails_contrast() -> None:
    theme = _theme(tokens={"fg": {"default": "#20202a"}})
    failures = validator.check_theme(theme)
    assert failures, "dark-on-dark text must not pass"
    assert all("4.5:1" in f for f in failures), failures


def test_a_readable_override_passes_contrast() -> None:
    theme = _theme(tokens={"accent": {"default": "#2dd4bf", "on": "#04201c"}})
    assert validator.check_theme(theme) == []


def test_merged_tokens_inherit_the_base() -> None:
    merged = validator.merged_tokens(_theme(tokens={"accent": {"default": "#2dd4bf"}}))
    assert merged["accent.default"] == "#2dd4bf"
    assert merged["bg.base"] == validator.tokens.theme_tokens("kova-dark")["bg.base"]


def test_schema_rejects_unknown_keys_everywhere() -> None:
    assert SCHEMA["additionalProperties"] is False
    assert SCHEMA["properties"]["tokens"]["additionalProperties"] is False
    assert set(SCHEMA["required"]) == {"id", "name", "version", "appearance"}
