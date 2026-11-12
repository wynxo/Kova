"""The chat modes: how much Kova is allowed to do in a conversation.

``chat`` is plain conversation with no tools except web search; ``work`` (the
default) is everything Kova can do today. A future ``code`` mode belongs to a
separate app (kova-code), so new modes are added here and in
:mod:`kova.agent`'s tool selection only.
"""

MODES = ("chat", "work")
DEFAULT_MODE = "work"
