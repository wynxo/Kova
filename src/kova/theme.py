"""Kova's colours, in one place, so the CLI and the desktop app look like one product.

Use these names instead of raw colours (``style=theme.ACCENT`` or
``f"[{theme.MUTED}]...[/]"``). The desktop app mirrors the same values as CSS
variables. Change a colour here and every screen follows; the terminal chat
converts them for 256- and 16-colour terminals by itself (``kova.tui.style``).
"""

ACCENT = "#8b7cff"      # Kova violet: brand, the assistant's name, highlights
ACCENT_SOFT = "#b4a9ff"  # lighter violet: secondary highlights, links
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
GRADIENT = ("#7aa2ff", "#8b7cff", "#c77dff", "#ff7eb6")  # the logo: periwinkle, violet, pink
CODE_THEME = "monokai"   # Pygments theme for code blocks
