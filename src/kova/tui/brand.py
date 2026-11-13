"""Kova's look outside the chat: the wordmark, ``--help``, ``status`` and setup.

These are Rich renderables, so they drop to plain text when output is piped
and to fewer colours on simpler terminals by themselves. The chat's own
widgets (``widgets``) draw the same wordmark and colours with raw escapes.
"""
from __future__ import annotations

from rich import box
from rich.console import Group, RenderableType
from rich.panel import Panel
from rich.text import Text

from kova import theme

from .style import blend
from .widgets import LOGO

TAGLINE = "your private AI"

# Which ``--help`` panel each top-level command belongs to, in display order.
HELP_PANELS = {
    "ask": "Chat", "chats": "Chat",
    "setup": "Models", "providers": "Models", "model": "Models",
    "memory": "Memory", "reminders": "Memory",
    "export": "Your data", "import": "Your data",
    "status": "Server & updates", "serve": "Server & updates", "stop": "Server & updates",
    "update": "Server & updates", "uninstall": "Server & updates",
}


def gradient_text(text: str, *, bold: bool = True, stops=theme.GRADIENT) -> Text:
    """``text`` shaded left to right along Kova's gradient."""
    out = Text()
    visible = [index for index, char in enumerate(text) if not char.isspace()]
    last = max(1, len(visible) - 1)
    order = {index: position for position, index in enumerate(visible)}
    for index, char in enumerate(text):
        if index in order:
            colour = blend(stops, order[index] / last)
            out.append(char, style=f"{'bold ' if bold else ''}{colour}")
        else:
            out.append(char)
    return out


def wordmark(version: str | None = None) -> Group:
    """The two-row half-block KOVA, a tagline and the version beside it."""
    top = Text(" ") + gradient_text(LOGO[0])
    bottom = Text(" ") + gradient_text(LOGO[1])
    bottom.append("   " + TAGLINE, style=theme.MUTED)
    if version:
        bottom.append(f"  v{version}", style=theme.FAINT)
    return Group(top, bottom)


def heading(text: str, detail: str = "") -> Text:
    """A section heading: a violet diamond, the title and a quiet detail."""
    line = Text.assemble((" ◆ ", f"bold {theme.ACCENT}"), (text, "bold"))
    if detail:
        line.append("  " + detail, style=theme.MUTED)
    return line


def card(body: RenderableType, title: str, *, subtitle: str = "", tone: str = theme.ACCENT) -> Panel:
    """A rounded card with a Kova-coloured title in its top edge."""
    name, _, rest = title.partition(" ")
    heading_text = gradient_text(name) + Text(" " + rest if rest else "", style=f"bold {theme.MUTED}")
    return Panel(body, title=heading_text, title_align="left",
                 subtitle=Text(subtitle, style=theme.MUTED) if subtitle else None,
                 subtitle_align="right", border_style=tone, box=box.ROUNDED,
                 padding=(1, 2), expand=False)


def style_help(group) -> None:
    """Dress Typer's ``--help`` in Kova's colours and sort a click group's
    commands into panels (``HELP_PANELS``)."""
    from typer import rich_utils

    rich_utils.STYLE_OPTION = f"bold {theme.ACCENT_SOFT}"
    rich_utils.STYLE_SWITCH = f"bold {theme.USER}"
    rich_utils.STYLE_NEGATIVE_OPTION = f"bold {theme.WARNING}"
    rich_utils.STYLE_NEGATIVE_SWITCH = f"bold {theme.WARNING}"
    rich_utils.STYLE_METAVAR = f"{theme.FAINT}"
    rich_utils.STYLE_TYPES = theme.FAINT
    rich_utils.STYLE_TYPES_SEPARATOR = theme.FAINT
    rich_utils.STYLE_USAGE = f"bold {theme.ACCENT}"
    rich_utils.STYLE_USAGE_COMMAND = "bold"
    rich_utils.STYLE_HELPTEXT_FIRST_LINE = ""
    rich_utils.STYLE_HELPTEXT = theme.MUTED
    rich_utils.STYLE_OPTION_HELP = ""
    rich_utils.STYLE_OPTION_DEFAULT = theme.MUTED
    rich_utils.STYLE_REQUIRED_SHORT = theme.ERROR
    rich_utils.STYLE_REQUIRED_LONG = theme.ERROR
    rich_utils.STYLE_OPTIONS_PANEL_BORDER = theme.BORDER
    rich_utils.STYLE_COMMANDS_PANEL_BORDER = theme.BORDER
    rich_utils.STYLE_ERRORS_PANEL_BORDER = theme.ERROR
    rich_utils.STYLE_COMMANDS_TABLE_FIRST_COLUMN = f"bold {theme.ACCENT_SOFT}"
    commands = getattr(group, "commands", None)
    if not isinstance(commands, dict):
        return
    for name, command in commands.items():
        if name in HELP_PANELS and not getattr(command, "rich_help_panel", None):
            command.rich_help_panel = HELP_PANELS[name]
    # Panels print in the order their first command is listed: put Chat first.
    order = list(dict.fromkeys(HELP_PANELS.values()))
    ranked = sorted(commands.items(), key=lambda item: order.index(HELP_PANELS[item[0]])
                    if item[0] in HELP_PANELS else len(order))
    commands.clear()
    commands.update(ranked)


def setup_banner(console, version: str) -> None:
    """The first thing ``kova setup`` shows: the wordmark and what happens next."""
    console.print()
    console.print(wordmark(version))
    console.print()
    console.print(Text.assemble(
        ("  Let's connect a model. ", "bold"),
        (("Kova talks to a model server on your machine or your network,\n"
          "  or to a provider you have an account with. Nothing is sent until you chat."),
         theme.MUTED)))


__all__ = ["HELP_PANELS", "TAGLINE", "card", "gradient_text", "heading", "setup_banner",
           "style_help", "wordmark"]
