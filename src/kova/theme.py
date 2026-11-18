"""Kova's terminal colours, shared by the CLI, setup wizard and chat.

Use these names instead of raw colours (``style=theme.ACCENT`` or
``f"[{theme.MUTED}]...[/]"``). The terminal chat converts them for 256- and 16-colour
terminals by itself (``kova.tui.style``).
"""

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
CODE = "#ffb38a"         # inline code: a warm peach that reads on dark and light themes
GRADIENT = ("#ffffff", "#d8d8e0", "#b4b4c0", "#8a8f98")  # monochrome logo
CODE_THEME = "monokai"   # Pygments theme for code blocks
