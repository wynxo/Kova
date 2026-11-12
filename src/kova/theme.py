"""Kova's colours, in one place, so the CLI and the desktop app look like one product.

Use these names instead of raw colours (``style=theme.ACCENT`` or
``f"[{theme.MUTED}]...[/]"``). The desktop app mirrors the same values as CSS
variables. Change a colour here and every screen follows.
"""

ACCENT = "#8b7cff"      # Kova violet: brand, the assistant's name, highlights
ACCENT_SOFT = "#b4a9ff"  # lighter violet: secondary highlights, links
USER = "#5fd7af"         # the user's own messages and prompt
MUTED = "#8a8f98"        # hints, timings, secondary text
BORDER = "#3a3d45"       # panel and table borders
SUCCESS = "#5fd787"
WARNING = "#ffaf5f"
ERROR = "#ff5f6d"
CODE_THEME = "monokai"   # Pygments theme for code blocks
