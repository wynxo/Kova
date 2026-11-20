"""A disposable terminal theme preview; it never changes the active palette."""
import asyncio

from prompt_toolkit import Application
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import FormattedTextControl, Layout, Window
from prompt_toolkit.styles import Style

from kova.theme import terminal_from_resolved

TRY_SECONDS = 30


def keep_preview(data: dict) -> bool:
    roles = terminal_from_resolved(data).roles
    name = "".join(char for char in data["name"] if char.isprintable())
    bindings = KeyBindings()

    @bindings.add("K")
    @bindings.add("k")
    def keep(event):
        event.app.exit(result=True)

    @bindings.add("R")
    @bindings.add("r")
    @bindings.add("escape", eager=True)
    @bindings.add("c-c")
    @bindings.add("c-d")
    def revert(event):
        event.app.exit(result=False)

    content = FormattedTextControl([
        ("class:accent", f"  {name}\n\n"),
        ("", "  How can I help?\n"),
        ("class:muted", "  This preview reverts after 30 seconds.\n\n"),
        ("", "  K Keep  ·  R / Esc Revert"),
    ])
    app = Application(
        layout=Layout(Window(content, style="class:surface", wrap_lines=True)),
        key_bindings=bindings,
        style=Style.from_dict({
            "surface": f"bg:{roles['SURFACE']} {roles['ACCENT']}",
            "accent": roles["USER"],
            "muted": roles["MUTED"],
        }),
        full_screen=True,
    )

    async def run():
        try:
            return await asyncio.wait_for(app.run_async(), timeout=TRY_SECONDS)
        except (TimeoutError, EOFError):
            return False

    return asyncio.run(run())
