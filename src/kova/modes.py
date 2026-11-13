"""The chat modes: how much Kova is allowed to do in a conversation.

``chat`` is plain conversation with no tools except web search; ``work`` (the
default) is everything Kova can do today. A future ``code`` mode belongs to a
separate app (kova-code), so new modes are added here and in
:mod:`kova.agent`'s tool selection only.

Each mode declares the capabilities it offers (the vocabulary lives in
:mod:`kova.providers`); a UI asks :func:`capabilities` — or, for one
conversation, :func:`kova.providers.offered` — instead of branching on the
mode name.
"""

MODES = ("chat", "work")
DEFAULT_MODE = "work"

# What a conversation may do in each mode. A provider may declare less than
# this (see ``kova.providers.capabilities``); it can never declare more.
MODE_CAPABILITIES = {
    "chat": frozenset({"reasoning", "steering"}),
    "work": frozenset({"tools", "approvals", "reasoning", "steering"}),
}


def capabilities(mode: str) -> frozenset[str]:
    """What this mode offers; an unknown mode falls back to the default."""
    return MODE_CAPABILITIES.get(mode, MODE_CAPABILITIES[DEFAULT_MODE])
