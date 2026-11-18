"""Translate explicitly supported model settings, not reasoning-stream support."""

from __future__ import annotations

import re
from typing import Literal

from kova.config import ModelEntry

ReasoningEffort = Literal["off", "low", "medium", "high"]
LEVELS = ("low", "medium", "high")
ALL_LEVELS = ("off", *LEVELS)

# Strip only dated snapshots, not variants such as chat-latest or pro.
_SNAPSHOT = re.compile(r"-(?:\d{4}-\d{2}-\d{2}|\d{8})$")
_OPENAI_OFF = {
    "gpt-5.1", "gpt-5.2", "gpt-5.4", "gpt-5.4-mini", "gpt-5.4-nano",
    "gpt-5.5", "gpt-5.6", "gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna",
}
_OPENAI_LEVELS = {
    "o3", "o3-mini", "o4-mini", "gpt-5", "gpt-5-mini", "gpt-5-nano",
    "gpt-5-codex", "gpt-5.1-codex", "gpt-5.1-codex-max",
    "gpt-5.2-codex", "gpt-5.3-codex",
}
_CLAUDE_OFF = {
    "claude-opus-4-6", "claude-opus-4-7", "claude-opus-4-8", "claude-sonnet-4-6",
    "claude-opus-5", "claude-sonnet-5", "claude-haiku-5-5",
}
_CLAUDE_LEVELS = {
    "claude-fable-5", "claude-fable-5-1", "claude-fable-5-2", "claude-mythos-5",
    "claude-mythos-5-1", "claude-mythos-5-2", "claude-opus-5-5", "claude-sonnet-5-5",
}


def choices(entry: ModelEntry) -> tuple[str, ...]:
    """Only advertise levels the provider/model pair can actually accept."""
    provider = entry._provider
    if provider is None:
        return ()
    model = _SNAPSHOT.sub("", entry.model.lower())
    if provider.kind in {"custom", "local"} and provider.adapter == "openai":
        # A generic endpoint's model ID alone cannot tell us what its server
        # accepts. Its operator can opt in to the Chat Completions field.
        declared = entry.reasoning_efforts
        return tuple(level for level in ALL_LEVELS if isinstance(declared, list) and level in declared)
    if provider.adapter == "anthropic":
        if model in _CLAUDE_OFF:
            return ALL_LEVELS
        if model in _CLAUDE_LEVELS:
            return LEVELS
    if provider.kind in {"openai", "chatgpt", "codex", "openrouter"}:
        if provider.kind == "openrouter":
            namespace, _, model = model.partition("/")
            if namespace != "openai":
                return ()
        if model in _OPENAI_OFF:
            return ALL_LEVELS
        if model in _OPENAI_LEVELS:
            return LEVELS
    return ()


def payload_fields(entry: ModelEntry, effort: str | None) -> dict:
    """Translate a selected level to this provider's wire format."""
    if effort not in choices(entry):
        return {}
    provider = entry._provider
    if provider.adapter == "anthropic":
        if effort == "off":
            return {"thinking": {"type": "disabled"}}
        return {"thinking": {"type": "adaptive"}, "output_config": {"effort": effort}}
    wire_effort = "none" if effort == "off" else effort
    if provider.adapter == "responses" or provider.kind == "openrouter":
        return {"reasoning": {"effort": wire_effort}}
    return {"reasoning_effort": wire_effort}
