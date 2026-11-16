"""Optional per-chat colour and icon for issue #213.

A chat may carry a ``colour`` (one key of :data:`CHAT_COLOURS`, never a
free-form hex value) and an ``icon`` (one name from :data:`CHAT_ICONS`, which
names the icons vendored in ``web_assets/app.css`` as ``.i-<name>`` classes).
Both are nullable: ``None`` means the chat uses the default sidebar look.

Colours resolve to theme tokens, so themes and the coming theme gallery can
change them without a code change. The mapping lives in the web client
(``web_assets/chat-appearance.js`` mirrors these tables); this module is the
server-side gate that keeps anything else out of the database.
"""

from __future__ import annotations

#: Chat colour key -> CSS variable it renders with. Every value is a semantic
#: theme token from ``design/tokens.json`` (via generated ``tokens.css``), so
#: no hard-coded colour ever reaches a chat row.
CHAT_COLOURS: dict[str, str] = {
    "accent": "var(--accent-default)",
    "success": "var(--status-success)",
    "warning": "var(--status-warning)",
    "danger": "var(--status-danger)",
    "info": "var(--status-info)",
    "keyword": "var(--syntax-keyword)",
    "string": "var(--syntax-string)",
    "function": "var(--syntax-function)",
}

#: Icon names a chat may use. Each must match a vendored ``.i-<name>`` icon
#: class in ``web_assets/app.css`` (Lucide-derived, offline data-URIs).
CHAT_ICONS: tuple[str, ...] = (
    "bell",
    "brain",
    "compass",
    "folder",
    "globe",
    "lightbulb",
    "message-square",
    "moon",
    "puzzle",
    "shield-check",
    "square-terminal",
    "sun",
)


def normalize_colour(value: str | None) -> str | None:
    """Validate a chat colour; ``None``/blank clears it back to the default."""
    if value is None:
        return None
    key = value.strip().lower()
    if not key:
        return None
    if key not in CHAT_COLOURS:
        raise ValueError(f"unknown chat colour '{value}'")
    return key


def normalize_icon(value: str | None) -> str | None:
    """Validate a chat icon; ``None``/blank clears it back to the default."""
    if value is None:
        return None
    name = value.strip().lower()
    if not name:
        return None
    if name not in CHAT_ICONS:
        raise ValueError(f"unknown chat icon '{value}'")
    return name
