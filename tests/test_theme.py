"""Kova's shared colour palette lives in one module so every screen matches."""

import re

import kova.theme as theme

_HEX6 = re.compile(r"^#[0-9a-f]{6}$")

# Every named swatch the UI is allowed to reference.
_NAMED = (
    "ACCENT", "ACCENT_SOFT", "USER", "MUTED", "FAINT", "BORDER", "SURFACE",
    "ON_ACCENT", "SUCCESS", "WARNING", "ERROR", "BASH", "MEMORY", "CODE",
)


def test_named_colours_are_six_digit_hex():
    for name in _NAMED:
        value = getattr(theme, name)
        assert _HEX6.match(value), f"{name}={value!r} is not a #rrggbb colour"


def test_brand_values_are_stable():
    # Callers pin to these exact values; a drift breaks the whole product's look.
    assert theme.ACCENT == "#8b7cff"
    assert theme.ACCENT_SOFT == "#b4a9ff"
    assert theme.USER == "#5fd7af"
    assert theme.SUCCESS == "#5fd787"
    assert theme.ERROR == "#ff5f6d"


def test_gradient_has_four_logo_stops():
    assert theme.GRADIENT == ("#7aa2ff", "#8b7cff", "#c77dff", "#ff7eb6")
    assert len(theme.GRADIENT) == 4
    assert all(_HEX6.match(stop) for stop in theme.GRADIENT)


def test_code_block_theme_is_monokai():
    assert theme.CODE_THEME == "monokai"
