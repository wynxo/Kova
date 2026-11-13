"""Colour for Kova's terminal chat, at whatever depth the terminal has.

Every colour is written once, as a truecolor SGR fragment built from the hex
values in ``kova.theme`` (``ACCENT``, ``USER`` ...). A ``Palette`` paints with
those fragments and quietly converts them for the terminal it is talking to:

* level 3: 24-bit colour, exactly the theme's hex values
* level 2: the nearest of the 256 xterm colours
* level 1: the nearest of the 16 basic ANSI colours (the ``linux`` console,
  plain ``xterm``, old Windows hosts)
* level 0: no escapes at all (``NO_COLOR``, ``TERM=dumb``, pipes)

``colour_level`` works the level out from the environment; ``KOVA_COLORS``
(``truecolor``, ``256``, ``16`` or ``none``) overrides it. Gradients
(``gradient``) blend smoothly at levels 2 and 3 and settle on one colour at
level 1.
"""
from __future__ import annotations

import os
import re

from kova import theme


# ------------------------------------------------------------------ hex and RGB
def rgb(hex_colour: str) -> tuple[int, int, int]:
    value = hex_colour.lstrip("#")
    return int(value[0:2], 16), int(value[2:4], 16), int(value[4:6], 16)


def fg(hex_colour: str) -> str:
    """A truecolor foreground SGR fragment for a theme colour."""
    r, g, b = rgb(hex_colour)
    return f"38;2;{r};{g};{b}"


def bg(hex_colour: str) -> str:
    r, g, b = rgb(hex_colour)
    return f"48;2;{r};{g};{b}"


# Kova's roles, as SGR fragments (see kova/theme.py for what each one is for).
ACCENT = fg(theme.ACCENT)
ACCENT_SOFT = fg(theme.ACCENT_SOFT)
ACCENT_END = fg(theme.GRADIENT[-1])
USER = fg(theme.USER)
MUTED = fg(theme.MUTED)
FAINT = fg(theme.FAINT)
BORDER = fg(theme.BORDER)
SUCCESS = fg(theme.SUCCESS)
WARNING = fg(theme.WARNING)
ERROR = fg(theme.ERROR)
BASH = fg(theme.BASH)
MEMORY = fg(theme.MEMORY)
SELECT = fg(theme.ACCENT_SOFT)
CODE = fg(theme.CODE)
HEADING = "1;" + ACCENT

# The 16 basic colours as most terminals draw them, for picking the closest.
_BASIC = [(0, 0, 0), (205, 49, 49), (13, 188, 121), (229, 229, 16), (36, 114, 200),
          (188, 63, 188), (17, 168, 205), (229, 229, 229), (118, 118, 118), (241, 76, 76),
          (35, 209, 139), (245, 245, 67), (59, 142, 234), (214, 112, 214), (41, 184, 219),
          (255, 255, 255)]
_CUBE = (0, 95, 135, 175, 215, 255)
_TRUECOLOR = re.compile(r"(38|48);2;(\d+);(\d+);(\d+)")


def _nearest_256(r: int, g: int, b: int) -> int:
    def level(value: int) -> int:
        return min(range(6), key=lambda index: abs(_CUBE[index] - value))
    cube = 16 + 36 * level(r) + 6 * level(g) + level(b)
    cr, cg, cb = _CUBE[level(r)], _CUBE[level(g)], _CUBE[level(b)]
    grey = max(0, min(23, round(((r + g + b) / 3 - 8) / 10)))
    gv = 8 + grey * 10
    cube_error = (cr - r) ** 2 + (cg - g) ** 2 + (cb - b) ** 2
    grey_error = (gv - r) ** 2 + (gv - g) ** 2 + (gv - b) ** 2
    return 232 + grey if grey_error < cube_error else cube


def _nearest_16(r: int, g: int, b: int, background: bool) -> int:
    # Weight hue over brightness so a soft violet still reads as magenta.
    def distance(colour):
        cr, cg, cb = colour
        return 2 * (cr - r) ** 2 + 4 * (cg - g) ** 2 + 3 * (cb - b) ** 2
    index = min(range(16), key=lambda i: distance(_BASIC[i]))
    if background:
        return (40 + index) if index < 8 else (100 + index - 8)
    return (30 + index) if index < 8 else (90 + index - 8)


# Where the nearest basic colour would lose a role's meaning, pin it by hand.
_BASIC_ROLES = {rgb(theme.ACCENT): 95, rgb(theme.ACCENT_SOFT): 95, rgb(theme.USER): 36,
                rgb(theme.MUTED): 90, rgb(theme.FAINT): 90, rgb(theme.BORDER): 90,
                rgb(theme.CODE): 33, rgb(theme.MEMORY): 94, rgb(theme.BASH): 35,
                rgb(theme.WARNING): 33, rgb(theme.ERROR): 91, rgb(theme.SUCCESS): 32,
                **{rgb(stop): 95 for stop in theme.GRADIENT}}


def downgrade(codes: str, level: int) -> str:
    """Rewrite the truecolor fragments in ``codes`` for a terminal at ``level``."""
    if level >= 3 or "2;" not in codes:
        return codes

    def swap(match: re.Match) -> str:
        kind, r, g, b = match.group(1), int(match.group(2)), int(match.group(3)), int(match.group(4))
        if level == 2:
            return f"{kind};5;{_nearest_256(r, g, b)}"
        role = _BASIC_ROLES.get((r, g, b))
        if role is not None:
            return str(role + 10 if kind == "48" else role)
        return str(_nearest_16(r, g, b, kind == "48"))
    return _TRUECOLOR.sub(swap, codes)


def colour_level(env=None, *, stream=None) -> int:
    """How many colours the terminal shows: 3 truecolor, 2 = 256, 1 = 16, 0 none."""
    env = os.environ if env is None else env
    forced = env.get("KOVA_COLORS", "").strip().lower()
    if forced in ("none", "0", "off", "no"):
        return 0
    if forced in ("16", "basic", "ansi"):
        return 1
    if forced in ("256", "8bit"):
        return 2
    if forced in ("truecolor", "24bit", "16m", "full"):
        return 3
    if "NO_COLOR" in env:
        return 0
    term = env.get("TERM", "").lower()
    if term == "dumb":
        return 0
    if stream is not None and not getattr(stream, "isatty", lambda: False)():
        return 0
    if env.get("COLORTERM", "").lower() in ("truecolor", "24bit"):
        return 3
    if any(name in term for name in ("kitty", "direct", "alacritty", "wezterm", "foot", "ghostty")) \
            or env.get("TERM_PROGRAM", "") in ("iTerm.app", "WezTerm", "vscode", "ghostty") \
            or "KONSOLE_VERSION" in env or "WT_SESSION" in env:
        return 3
    if "256" in term:
        return 2
    if term in ("linux", "xterm", "xterm-color", "screen", "tmux", "vt100", "vt220", "ansi",
                "cygwin", "rxvt", "putty"):
        return 1
    return 2


class Palette:
    """ANSI styles at the terminal's colour depth, or none at all.

    ``Palette(True)`` paints in 256 colours, ``Palette(False)`` not at all, and
    ``Palette(level)`` at an explicit depth (see ``colour_level``).
    ``theme_name`` picks the background family: the default ``dark`` keeps the
    theme's colours as written; ``light`` darkens foregrounds and pales tinted
    backgrounds so they stay readable on a light terminal.
    """

    def __init__(self, color: bool | int = True, *, theme_name: str | None = None):
        if color is True:
            color = 2
        self.level = max(0, min(3, int(color or 0)))
        self.color = self.level > 0
        self.theme_name = (theme_name or os.environ.get("KOVA_THEME", "dark")).lower()

    def _codes(self, codes: str) -> str:
        if self.theme_name == "light" and self.level >= 2:
            def translate(match):
                kind = match.group(1)
                values = [int(match.group(i)) for i in (2, 3, 4)]
                # Keep text dark and tints pale on a light terminal background.
                values = [round(v * .48) for v in values] if kind == "38" else [round(235 + v * .06) for v in values]
                return f"{kind};2;{values[0]};{values[1]};{values[2]}"
            codes = _TRUECOLOR.sub(translate, codes)
        return downgrade(codes, self.level)

    def paint(self, codes: str, text: str) -> str:
        if not (self.color and text):
            return text
        return f"\x1b[{self._codes(codes)}m{text}\x1b[0m"

    def sgr(self, codes: list[str]) -> str:
        if not self.color:
            return ""
        if not codes:
            return "\x1b[0m"
        return f"\x1b[0;{self._codes(';'.join(codes))}m"

    def bold(self, text): return self.paint("1", text)
    def dim(self, text): return self.paint(MUTED, text) if self.level >= 2 else self.paint("2", text)
    def faint(self, text): return self.paint(FAINT, text) if self.level >= 2 else self.paint("2", text)
    def italic(self, text): return self.paint("3", text)
    def green(self, text): return self.paint(SUCCESS, text)
    def red(self, text): return self.paint(ERROR, text)
    def yellow(self, text): return self.paint(WARNING, text)
    def blue(self, text): return self.paint(MEMORY, text)
    def accent(self, text): return self.paint(ACCENT, text)
    def code(self, text): return self.paint(CODE, text)

    def pill(self, text: str, colour: str = theme.ACCENT, *, solid: bool = False) -> str:
        """A small label chip: `` text `` on a tinted background.

        ``solid`` fills it with ``colour`` (dark text on top); otherwise the chip
        is a soft tint of ``colour`` with the colour as its text. Without colour
        it is the bare text, so nothing reads as noise in a plain terminal.
        """
        if not self.color:
            return text
        if self.level == 1:
            return self.paint("7;1;" + fg(colour), f" {text} ")
        if solid:
            return self.paint(f"1;{fg(theme.ON_ACCENT)};{bg(colour)}", f" {text} ")
        return self.paint(f"{fg(colour)};{bg(mix(colour, theme.SURFACE, 0.78))}", f" {text} ")

    def gradient(self, text: str, stops=None, *, bold: bool = False) -> str:
        return gradient(text, self, stops, bold=bold)


# ------------------------------------------------------------------ gradients
def mix(first: str, second: str, amount: float) -> str:
    """``amount`` of the way from ``first`` to ``second``, as hex."""
    a, b = rgb(first), rgb(second)
    return "#" + "".join(f"{round(x + (y - x) * amount):02x}" for x, y in zip(a, b, strict=True))


def blend(stops, position: float) -> str:
    """The colour ``position`` (0..1) of the way along a gradient of hex ``stops``."""
    stops = list(stops)
    if len(stops) == 1:
        return stops[0]
    position = max(0.0, min(1.0, position))
    span = position * (len(stops) - 1)
    index = min(int(span), len(stops) - 2)
    return mix(stops[index], stops[index + 1], span - index)


def gradient(text: str, p: Palette, stops=None, *, bold: bool = False) -> str:
    """``text`` coloured left to right along ``stops`` (Kova's gradient by default)."""
    stops = stops or theme.GRADIENT
    if not p.color or not text:
        return text
    weight = "1;" if bold else ""
    if p.level == 1:
        return p.paint(weight + fg(stops[0]), text)
    visible = [index for index, char in enumerate(text) if not char.isspace()]
    if len(visible) <= 1:
        return p.paint(weight + fg(stops[0]), text)
    out, last = [], None
    for order, index in enumerate(visible):
        colour = blend(stops, order / (len(visible) - 1))
        codes = p._codes(weight + fg(colour))
        start = visible[order - 1] + 1 if order else 0
        out.append(text[start:index])
        out.append(text[index] if codes == last else f"\x1b[{codes}m{text[index]}")
        last = codes
    out.append(text[visible[-1] + 1:])
    return "".join(out) + "\x1b[0m"


__all__ = ["ACCENT", "ACCENT_END", "ACCENT_SOFT", "BASH", "BORDER", "CODE", "ERROR", "FAINT",
           "HEADING", "MEMORY", "MUTED", "SELECT", "SUCCESS", "USER", "WARNING", "Palette", "bg",
           "blend", "colour_level", "downgrade", "fg", "gradient", "mix", "rgb"]
