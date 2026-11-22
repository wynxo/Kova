"""Kova's terminal colours, shared by the CLI, setup wizard and chat.

The constants below are the terminal roles of the built-in ``kova-dark`` theme. Code
names a role by its constant (``style=theme.ACCENT`` or ``f"[{theme.MUTED}]...[/]"``);
the renderers (``kova.tui.style``) swap each one for the active theme's colour and then
convert it for 256- and 16-colour terminals. So the constants are both the dark palette
and the stable names every other theme maps onto.

A theme never ships terminal colours of its own beyond its 16-colour ANSI palette.
Every role follows a semantic token instead (``ROLE_TOKENS``), the same values the web
app paints with, so a theme that changes ``accent.default`` changes the terminal accent
too. ``terminal_theme`` does that mapping.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

ACCENT = "#f6f6f8"       # brand, the assistant's name, highlights
ACCENT_SOFT = "#b4b4c0"  # secondary highlights, links
USER = "#5fd7af"         # the user's own messages and prompt
MUTED = "#8a8f98"        # hints, timings, secondary text
FAINT = "#5c616b"        # the quietest text: separators, disabled rows
BORDER = "#3a3d45"       # panel and table borders
SURFACE = "#1c1b24"      # what tinted chips fade towards
ON_ACCENT = "#14121c"    # text drawn on a solid accent chip
SUCCESS = "#5fd787"
WARNING = "#ffaf5f"
ERROR = "#ff5f6d"
BASH = "#ff79c6"         # the "!" shell prefix and shell tool rows
MEMORY = "#7aa2ff"       # the "#" memory prefix and memory tool rows
CODE = "#ffb38a"         # inline code, and trace steps that change things (writes, commands)
THINK = "#c77dff"        # reasoning: the thinking node in the step trace
GRADIENT = ("#ffffff", "#d8d8e0", "#b4b4c0", "#8a8f98")  # monochrome logo
CODE_THEME = "monokai"   # Pygments theme for code blocks

#: Every role a renderer may name, in a stable order.
ROLES: tuple[str, ...] = (
    "ACCENT", "ACCENT_SOFT", "USER", "MUTED", "FAINT", "BORDER", "SURFACE", "ON_ACCENT",
    "SUCCESS", "WARNING", "ERROR", "BASH", "MEMORY", "CODE", "THINK",
)

#: The semantic token each role follows. ``kova-light`` takes every role from its
#: tokens. ``kova-dark`` keeps the values above, tuned for dark terminals, and any
#: other theme moves a role away from its base's value as soon as it changes the
#: token behind it.
ROLE_TOKENS: dict[str, str] = {
    "ACCENT": "accent.default",
    "ON_ACCENT": "accent.on",
    "MUTED": "fg.muted",
    "FAINT": "fg.faint",
    "BORDER": "border.subtle",
    "SURFACE": "bg.surface",
    "SUCCESS": "status.success",
    "WARNING": "status.warning",
    "ERROR": "status.danger",
    "MEMORY": "status.info",
    "CODE": "syntax.keyword",
}

#: Roles with no semantic token follow the theme's 16-colour ANSI palette instead.
ROLE_ANSI: dict[str, int] = {"USER": 6, "BASH": 5, "THINK": 5}

#: ``tui.theme`` / ``KOVA_THEME`` spellings that name a built-in.
ALIASES: dict[str, str] = {"dark": "kova-dark", "light": "kova-light"}


def theme_id(name: str | None) -> str:
    """The theme id a setting names: ``dark``/``light`` are the two built-ins."""
    text = str(name or "").strip().lower()
    return ALIASES.get(text, text or "kova-dark")


@dataclass(frozen=True)
class TerminalTheme:
    """One theme's terminal roles, resolved from its semantic tokens."""

    id: str
    appearance: str
    roles: dict[str, str]
    gradient: tuple[str, ...]
    changed: frozenset[str] = field(default_factory=frozenset)

    @property
    def light(self) -> bool:
        return self.appearance == "light"


DARK = TerminalTheme("kova-dark", "dark", {role: globals()[role] for role in ROLES}, GRADIENT)


def _hex(value: str, backdrop: str) -> str | None:
    """A token value as ``#rrggbb``; translucent colours are composited over ``backdrop``."""
    from kova.theme_contrast import parse_colour

    parsed = parse_colour(value) if isinstance(value, str) else None
    if parsed is None:
        return None
    r, g, b, alpha = parsed
    if alpha < 1:
        under = parse_colour(backdrop) or (0, 0, 0, 1)
        r, g, b = (c * alpha + u * (1 - alpha) for c, u in zip((r, g, b), under[:3], strict=True))
    return "#" + "".join(f"{round(c * 255):02x}" for c in (r, g, b))


def _mix(first: str, second: str, amount: float) -> str:
    a = [int(first[i:i + 2], 16) for i in (1, 3, 5)]
    b = [int(second[i:i + 2], 16) for i in (1, 3, 5)]
    return "#" + "".join(f"{round(x + (y - x) * amount):02x}" for x, y in zip(a, b, strict=True))


def from_tokens(theme_id_: str, appearance: str, tokens: dict, terminal: list[str],
                base: TerminalTheme, reference: dict, reference_terminal: list) -> TerminalTheme:
    """Map one resolved theme onto the terminal roles.

    ``base`` is the terminal palette of the built-in the theme extends, and
    ``reference``/``reference_terminal`` are that built-in's resolved tokens and ANSI
    palette: a role only moves away from its base value when the theme changes the
    token behind it. An empty reference maps every token.
    """
    backdrop = tokens.get("bg.base") or "#000000"

    def token(name: str) -> str | None:
        value = tokens.get(name)
        if value is None or value == reference.get(name):
            return None
        return _hex(value, backdrop)

    roles = dict(base.roles)
    changed: set[str] = set()
    for role, name in ROLE_TOKENS.items():
        value = token(name)
        if value:
            roles[role] = value
            changed.add(role)
    for role, index in ROLE_ANSI.items():
        if len(terminal) == 16 and terminal[index] != (reference_terminal or [None] * 16)[index]:
            value = _hex(terminal[index], backdrop)
            if value:
                roles[role] = value
                changed.add(role)
    if token("fg.default") or token("fg.muted"):
        roles["ACCENT_SOFT"] = _mix(_hex(tokens["fg.default"], backdrop) or roles["ACCENT_SOFT"],
                                    roles["MUTED"], 0.5)
        changed.add("ACCENT_SOFT")
    gradient = base.gradient
    if {"ACCENT", "MUTED"} & changed:
        start, end = roles["ACCENT"], roles["MUTED"]
        gradient = (start, _mix(start, end, 1 / 3), _mix(start, end, 2 / 3), end)
    return TerminalTheme(theme_id_, appearance, roles, gradient, frozenset(changed | base.changed))


def terminal_from_resolved(data: dict) -> TerminalTheme:
    """Build terminal roles from resolved theme data, including unsaved previews."""
    from kova import themes

    ident = data["id"]
    if ident == "kova-dark":
        return DARK
    base_id = data.get("extends") or themes.default_base(data["appearance"])
    reference = themes.base_tokens(base_id)
    reference_terminal = reference.pop("terminal")
    if base_id == "kova-light":
        base = from_tokens("kova-light", "light", reference, reference_terminal, DARK, {}, [])
        if ident == "kova-light":
            return base
    else:
        base = DARK
    return from_tokens(ident, data["appearance"], data["tokens"], data["terminal"],
                       base, reference, reference_terminal)


@lru_cache(maxsize=16)
def _cached(theme_id_: str, home: str, stamp: tuple) -> TerminalTheme:
    from kova import themes

    try:
        return terminal_from_resolved(themes.get(theme_id_, home))
    except (themes.ThemeError, OSError, ValueError, KeyError):
        return DARK


def terminal_theme(name: str | None, home: Path | str | None = None) -> TerminalTheme:
    """The terminal roles for a theme id (or ``dark``/``light``).

    A theme file that is missing or broken falls back to ``kova-dark``, so a bad file
    can never leave the terminal unreadable. Results are cached until the file changes.
    """
    ident = theme_id(name)
    if ident == "kova-dark":
        return DARK
    if home is None:
        from kova.config import kova_home
        home = kova_home()
    from kova import themes

    stamp: tuple = ()
    if ident not in themes.BUILTIN:
        try:
            info = os.stat(themes.theme_dir(home) / f"{ident}.json")
            stamp = (info.st_mtime_ns, info.st_size)
        except OSError:
            return DARK
    try:
        info = os.stat(themes.TOKENS_PATH)
        stamp += (info.st_mtime_ns,)
    except OSError:
        return DARK
    return _cached(ident, str(home), stamp)
