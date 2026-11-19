"""Shared WCAG colour checks for built-in, gallery and user themes."""

from __future__ import annotations

import re

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
    ("fg.muted", "bg.base", MIN_TEXT, "secondary text on the page"),
    ("fg.muted", "bg.raised", MIN_TEXT, "secondary text inside raised cards"),
    ("fg.inverse", "accent.default", MIN_TEXT, "text drawn on a solid accent chip"),
    ("accent.on", "accent.default", MIN_TEXT, "text on an accented button"),
    ("accent.default", "bg.surface", MIN_NONTEXT, "accent controls on panels"),
    ("accent.default", "bg.raised", MIN_NONTEXT, "accent controls on raised cards"),
    ("accent.default", "bg.base", MIN_NONTEXT, "the accent edge, icon and activity dot"),
    ("status.danger", "bg.surface", MIN_TEXT, "error text"),
    ("status.success", "bg.surface", MIN_TEXT, "success text"),
    ("status.warning", "bg.surface", MIN_TEXT, "warning text"),
    ("status.info", "bg.surface", MIN_TEXT, "informational text"),
    ("status.success", "bg.raised", MIN_TEXT, "success text inside raised cards"),
    ("status.danger", "bg.raised", MIN_TEXT, "error text inside raised cards"),
    ("status.warning", "bg.raised", MIN_TEXT, "warning text inside raised cards"),
    ("status.info", "bg.raised", MIN_TEXT, "informational text inside raised cards"),
    ("border.strong", "bg.surface", MIN_NONTEXT, "input and focus-ring boundaries (SC 1.4.11)"),
    ("border.subtle", "bg.surface", MIN_VISIBLE, "decorative dividers: perceptible, not a control boundary"),
    ("syntax.keyword", "bg.base", MIN_LARGE, "code keyword colour"),
    ("syntax.comment", "bg.base", MIN_LARGE, "code comment colour"),
    ("syntax.string", "bg.base", MIN_TEXT, "code string text"),
    ("syntax.number", "bg.base", MIN_TEXT, "code number text"),
    ("syntax.function", "bg.base", MIN_TEXT, "code function text"),
    ("syntax.type", "bg.base", MIN_TEXT, "code type text"),
    ("syntax.invalid", "bg.base", MIN_TEXT, "code error text"),
)


_NUMBER = re.compile(r"(?:[0-9]+|[0-9]*\.[0-9]+)%?")
_RGB = re.compile(r"rgba?\(([0-9.,% \t\r\n\f/]+)\)")
_HEX = re.compile(r"#(?:[0-9a-fA-F]{3,4}|[0-9a-fA-F]{6}|[0-9a-fA-F]{8})")
_SPACE = " \t\r\n\f"


def _unit(value: str, scale: int) -> float:
    if value.endswith("%"):
        return min(1.0, float(value[:-1]) / 100)
    return min(1.0, float(value) / scale)


def parse_colour(value: str) -> tuple[float, float, float, float] | None:
    """Read a CSS hex or numeric RGB colour as sRGB and alpha, all in 0..1."""
    if _HEX.fullmatch(value):
        digits = value[1:]
        if len(digits) <= 4:
            digits = "".join(channel * 2 for channel in digits)
        if len(digits) == 6:
            digits += "ff"
        return tuple(int(digits[i:i + 2], 16) / 255 for i in (0, 2, 4, 6))

    match = _RGB.fullmatch(value)
    if not match:
        return None
    body = match[1]
    if "," in body:  # Legacy comma syntax requires matching channel units.
        parts = [part.strip(_SPACE) for part in body.split(",")]
        channels, alpha = parts[:3], parts[3:]
        if len({channel.endswith("%") for channel in channels}) != 1:
            return None
    else:
        channel_text, *alpha = body.split("/")
        channels = re.split(r"[ \t\r\n\f]+", channel_text.strip(_SPACE))
        alpha = [part.strip(_SPACE) for part in alpha]
    if len(channels) != 3 or len(alpha) > 1:
        return None
    if not all(_NUMBER.fullmatch(part) for part in (*channels, *alpha)):
        return None
    r, g, b = (_unit(channel, 255) for channel in channels)
    return r, g, b, _unit(alpha[0], 1) if alpha else 1.0


def _srgb_to_linear(channel: float) -> float:
    return channel / 12.92 if channel <= 0.04045 else ((channel + 0.055) / 1.055) ** 2.4


def relative_luminance(rgb: tuple[float, float, float]) -> float:
    r, g, b = map(_srgb_to_linear, rgb)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast_ratio(fg_value: str, bg_value: str) -> float | None:
    """WCAG 2.x ratio, or None if either colour is invalid or translucent."""
    fg = parse_colour(fg_value)
    bg = parse_colour(bg_value)
    if fg is None or bg is None or fg[3] != 1 or bg[3] != 1:
        return None
    l1, l2 = relative_luminance(fg[:3]), relative_luminance(bg[:3])
    return (max(l1, l2) + 0.05) / (min(l1, l2) + 0.05)


def contrast_failures(tokens: dict[str, str], terminal: list[str] | None = None) -> list[str]:
    """Describe invalid colours and failed pairs after merging over the built-in base."""
    failures = [
        f"{name}: use a valid hex or rgb()/rgba() colour."
        for name, colour in tokens.items()
        if parse_colour(colour) is None
    ]
    if failures:
        return failures
    for fg, bg, minimum, why in PAIRS:
        ratio = contrast_ratio(tokens[fg], tokens[bg])
        if ratio is None:
            failures.append(f"{fg} on {bg}: translucent or invalid, contrast cannot be measured ({why})")
        elif ratio < minimum:
            failures.append(f"{fg} on {bg} = {ratio:.2f}:1, below {minimum}:1 ({why})")

    for index, colour in enumerate(terminal or []):
        rgba = parse_colour(colour)
        if rgba is None or rgba[3] != 1:
            failures.append(f"terminal[{index}]: {colour!r} is not a measurable colour")
    return failures
