"""Colour for Kova's terminal chat, at whatever depth the terminal has.

Every colour is written once, as a truecolor SGR fragment built from the role
values in ``kova.theme`` (``ACCENT``, ``USER`` ...). A ``Palette`` paints with
those fragments, swaps each role for the active theme's colour (the same semantic
tokens the web app uses, mapped by ``kova.theme.terminal_theme``) and quietly
converts the result for the terminal it is talking to:

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
import tomllib
from functools import lru_cache
from pathlib import Path

from kova import theme

_THEME_ID = re.compile(r"^[a-z0-9][a-z0-9-]{1,39}$")


def _configured_theme() -> str:
    from kova.config import kova_home
    path = kova_home() / "config.toml"
    try:
        info = path.stat()
        return _config_theme(str(path), info.st_mtime_ns, info.st_size)
    except OSError:
        return "dark"


@lru_cache(maxsize=4)
def _config_theme(path: str, _mtime: int, _size: int) -> str:
    try:
        data = tomllib.loads(Path(path).read_text(encoding="utf-8"))
        value = data.get("tui", {}).get("theme", "dark")
    except (OSError, ValueError, AttributeError):
        return "dark"
    return value if isinstance(value, str) else "dark"


def active_theme(configured: str | None = None) -> str:
    """Resolve the terminal theme: ``KOVA_THEME``, then ``[tui] theme``, then dark.

    The answer is a theme id from ``kova themes list``, or ``dark``/``light`` for the
    two built-ins. Styling must not create or migrate a config just to print
    ``--help``, so only the theme setting is read and an unreadable config is dark.
    A name that is not a theme id is dark too; one whose file is missing falls back
    when its colours are resolved (``kova.theme.terminal_theme``).
    """
    name = os.environ.get("KOVA_THEME", "").strip()
    if not name:
        name = configured if configured is not None else _configured_theme()
    name = str(name).strip().lower()
    return name if name in theme.ALIASES or _THEME_ID.fullmatch(name) else "dark"


def terminal_theme(theme_name: str | None = None) -> theme.TerminalTheme:
    """The active (or named) theme's terminal roles."""
    return theme.terminal_theme(theme_name or active_theme())


def _paler(values, *, background: bool):
    # Colours outside the theme (Pygments, a chat's own colour) on a light theme:
    # darken text and pale tinted backgrounds so both stay readable.
    return tuple(round(235 + v * .06) if background else round(v * .48) for v in values)


def resolve(value: str, terminal: theme.TerminalTheme, *, background: bool = False) -> str:
    """One hex colour in ``terminal``'s palette.

    A role constant from ``kova.theme`` becomes that role's colour in the theme, a
    gradient stop becomes the theme's stop, and on a light theme any other colour
    is adjusted for a light background. Everything else passes through unchanged.
    """
    key = rgb(value)
    role = _REFERENCE.get(key)
    if role is not None:
        return terminal.roles[role]
    stop = _GRADIENT_STOPS.get(key)
    if stop is not None:
        return terminal.gradient[stop]
    if terminal.light:
        return "#" + "".join(f"{v:02x}" for v in _paler(key, background=background))
    return value


def colour(role: str, *, background: bool = False, theme_name: str | None = None) -> str:
    """Effective hex for a palette role (``"ACCENT"``) or role hex, before downscaling."""
    terminal = terminal_theme(theme_name)
    if not role.startswith("#"):
        return terminal.roles[role]
    return resolve(role, terminal, background=background)


def gradient_stops(stops=None, theme_name: str | None = None) -> tuple[str, ...]:
    """The theme's logo gradient, or ``stops`` mapped into the active theme."""
    terminal = terminal_theme(theme_name)
    if stops is None:
        return terminal.gradient
    return tuple(_stop(stop, terminal) for stop in stops)


def _stop(value: str, terminal: theme.TerminalTheme) -> str:
    index = _ALL_STOPS.get(rgb(value))
    return terminal.gradient[index] if index is not None else resolve(value, terminal)


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


# Which role (or logo gradient stop) a colour written in ``kova.theme`` stands for.
_REFERENCE = {rgb(getattr(theme, role)): role for role in reversed(theme.ROLES)}
_ALL_STOPS = {rgb(stop): index for index, stop in enumerate(theme.GRADIENT)}
_GRADIENT_STOPS = {key: index for key, index in _ALL_STOPS.items() if key not in _REFERENCE}


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
THINK = fg(theme.THINK)
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
    # Weight hue over brightness when choosing a basic colour.
    def distance(colour):
        cr, cg, cb = colour
        return 2 * (cr - r) ** 2 + 4 * (cg - g) ** 2 + 3 * (cb - b) ** 2
    index = min(range(16), key=lambda i: distance(_BASIC[i]))
    if background:
        return (40 + index) if index < 8 else (100 + index - 8)
    return (30 + index) if index < 8 else (90 + index - 8)


# Where the nearest basic colour would lose a role's meaning, pin it by hand. At 16
# colours the terminal's own palette draws these, so they name a meaning, not a hue.
_PINS = {"ACCENT": 97, "ON_ACCENT": 30, "USER": 36, "MUTED": 90, "FAINT": 90, "BORDER": 90,
         "CODE": 33, "MEMORY": 94, "BASH": 35, "THINK": 35, "WARNING": 33, "ERROR": 91, "SUCCESS": 32}
_BASIC_ROLES = {rgb(getattr(theme, role)): pin for role, pin in _PINS.items()}


def _chromatic(hex_colour: str) -> bool:
    channels = rgb(hex_colour)
    return max(channels) - min(channels) >= 64


def basic_pins(terminal: theme.TerminalTheme) -> dict[str, int]:
    """Each role's 16-colour code in ``terminal``.

    A light theme swaps the accent pair (black text, white chips), and a theme with
    a coloured accent takes the basic colour nearest to it; every other role keeps
    its meaning (green for success, red for errors) on any background.
    """
    pins = dict(_PINS)
    if terminal.light:
        pins["ACCENT"], pins["ON_ACCENT"] = _PINS["ON_ACCENT"], _PINS["ACCENT"]
    accent = terminal.roles["ACCENT"]
    if "ACCENT" in terminal.changed and _chromatic(accent):
        pins["ACCENT"] = _nearest_16(*rgb(accent), False)
    return pins


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
    ``theme_name`` is the theme to paint with (a theme id, or ``dark``/``light``
    for the built-ins); by default the active one (``active_theme``). Codes are
    written with the role colours of ``kova.theme`` and swapped for the theme's
    own colours here, just before downscaling.
    """

    def __init__(self, color: bool | int = True, *, theme_name: str | None = None):
        if color is True:
            color = 2
        self.level = max(0, min(3, int(color or 0)))
        self.color = self.level > 0
        self.theme_name = theme_name or active_theme()

    @property
    def theme_name(self) -> str:
        return self._theme_name

    @theme_name.setter
    def theme_name(self, value: str) -> None:
        self._theme_name = str(value).lower()
        self.terminal = theme.terminal_theme(self._theme_name)
        self._pins = basic_pins(self.terminal)

    def hex(self, value: str) -> str:
        """A role colour (or any hex) as this palette's theme draws it."""
        return resolve(value, self.terminal)

    def _codes(self, codes: str) -> str:
        terminal = self.terminal

        def translate(match):
            kind = match.group(1)
            values = tuple(int(match.group(i)) for i in (2, 3, 4))
            role = _REFERENCE.get(values)
            if self.level == 1:
                pin = self._pins.get(role) if role else None
                if pin is None:
                    return match.group(0)
                return str(pin + 10 if kind == "48" else pin)
            if role is not None:
                values = rgb(terminal.roles[role])
            elif values in _GRADIENT_STOPS:
                values = rgb(terminal.gradient[_GRADIENT_STOPS[values]])
            elif terminal.light:
                values = _paler(values, background=kind == "48")
            else:
                return match.group(0)
            return f"{kind};2;{values[0]};{values[1]};{values[2]}"
        return downgrade(_TRUECOLOR.sub(translate, codes), self.level)

    def paint(self, codes: str, text: str) -> str:
        if not (self.color and text):
            return text
        return f"\x1b[{self._codes(codes)}m{text}\x1b[0m"

    def paint_resolved(self, codes: str, text: str) -> str:
        """Paint with colours already in this theme: downscale them, nothing else."""
        if not (self.color and text):
            return text
        return f"\x1b[{downgrade(codes, self.level)}m{text}\x1b[0m"

    def paint_mixed(self, codes: str, resolved: str, text: str) -> str:
        """``codes`` in role colours plus ``resolved`` ones already in this theme."""
        if not (self.color and text):
            return text
        return f"\x1b[{self._codes(codes)};{downgrade(resolved, self.level)}m{text}\x1b[0m"

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

        ``solid`` fills it with ``colour`` and contrasting text; otherwise the chip
        is a soft tint of ``colour`` with the colour as its text. Without colour
        it is the bare text, so nothing reads as noise in a plain terminal.
        """
        if not self.color:
            return text
        if solid and colour == theme.ACCENT:
            return self.paint(f"1;{fg(theme.ON_ACCENT)};{bg(colour)}", f" {text} ")
        if self.level == 1:
            return self.paint("7;1;" + fg(colour), f" {text} ")
        fill = self.hex(colour)
        surface = self.terminal.roles["SURFACE"]
        if solid:
            # A light theme writes pale text on its darker status colours.
            ink = surface if self.terminal.light else self.terminal.roles["ON_ACCENT"]
            return self.paint_resolved(f"1;{fg(ink)};{bg(fill)}", f" {text} ")
        return self.paint_resolved(f"{fg(fill)};{bg(mix(fill, surface, 0.78))}", f" {text} ")

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
    """``text`` coloured left to right along ``stops`` (the theme's gradient by default).

    ``stops`` are written in ``kova.theme`` colours, like every other code, and are
    mapped into the palette's theme before blending.
    """
    if not p.color or not text:
        return text
    weight = "1;" if bold else ""
    if p.level == 1:
        return p.paint(weight + (fg(stops[0]) if stops else ACCENT), text)
    stops = p.terminal.gradient if stops is None else [_stop(stop, p.terminal) for stop in stops]
    visible = [index for index, char in enumerate(text) if not char.isspace()]
    if len(visible) <= 1:
        return p.paint_resolved(weight + fg(stops[0]), text)
    out, last = [], None
    for order, index in enumerate(visible):
        colour = blend(stops, order / (len(visible) - 1))
        codes = downgrade(weight + fg(colour), p.level)
        start = visible[order - 1] + 1 if order else 0
        out.append(text[start:index])
        out.append(text[index] if codes == last else f"\x1b[{codes}m{text[index]}")
        last = codes
    out.append(text[visible[-1] + 1:])
    return "".join(out) + "\x1b[0m"


__all__ = ["ACCENT", "ACCENT_END", "ACCENT_SOFT", "BASH", "BORDER", "CODE", "ERROR", "FAINT",
           "HEADING", "MEMORY", "MUTED", "SELECT", "SUCCESS", "THINK", "USER", "WARNING", "Palette", "bg",
           "active_theme", "basic_pins", "blend", "colour", "colour_level", "downgrade", "fg",
           "gradient", "gradient_stops", "mix", "resolve", "rgb", "terminal_theme"]
