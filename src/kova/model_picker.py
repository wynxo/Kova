"""One way to choose the active model, shared by the CLI and the terminal chat.

The picker works from the server's ``/models`` list, so ``kova model use`` and
the chat's ``/model`` command offer the same rows, accept the same short
``provider/model`` references and save the same choice. Switching itself stays
a server call (``KovaClient.model_use``), which writes ``config.toml`` once, so
a choice made here applies to the current chat and to every future one.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Choice:
    """One configured model: what to save, and how to label it."""

    #: The saved entry name, which is what ``model_use`` takes.
    name: str
    #: The model id the provider serves, e.g. ``gpt-4o``.
    model: str
    #: The provider's display name, e.g. ``OpenAI``.
    provider: str
    #: The provider's saved id, e.g. ``openai``; empty for a bare server.
    provider_id: str = ""

    @property
    def reference(self) -> str:
        """``provider/model``, the short form users type."""
        return f"{self.provider_id}/{self.model}" if self.provider_id else self.name

    @property
    def shown(self) -> str:
        """The model id, or the entry name when the server lists no id."""
        return self.model or self.name


def choices(models: list[dict]) -> list[Choice]:
    """Every configured model as one sorted row per provider and model.

    An entry with no model id is the first-run placeholder, not a choice, so it
    is left out: the picker never offers a server Kova cannot call yet.
    """
    rows = []
    for entry in models:
        model = str(entry.get("model") or "")
        if not model:
            continue
        provider_id = str(entry.get("provider") or "")
        provider = str(entry.get("provider_name") or provider_id or "Custom")
        rows.append(Choice(name=str(entry.get("name") or ""), model=model,
                           provider=provider, provider_id=provider_id))
    return sorted(rows, key=lambda row: (row.provider.casefold(), row.shown.casefold(), row.name))


def label(row: Choice, *, active: str = "") -> str:
    """One picker row: ``● OpenAI · gpt-4o (current)``."""
    current = row.name == active
    text = f"{'● ' if current else '  '}{row.provider} · {row.shown}"
    return text + ("  (current)" if current else "")


def numbered(rows: list[Choice], *, active: str = "") -> list[str]:
    """Plain numbered rows, for a pipe or a terminal without raw keys."""
    return [f"{index:>2}  {label(row, active=active)}"
            for index, row in enumerate(rows, 1)]


def current(rows: list[Choice], active: str) -> int:
    """The row the picker opens on: the saved choice, or the first row."""
    return next((index for index, row in enumerate(rows) if row.name == active), 0)


def resolve(rows: list[Choice], text: str) -> Choice | None:
    """The one row ``text`` names, or ``None`` when it is unknown or ambiguous.

    Accepted, in order: the saved entry name, ``provider/model`` (with either
    the provider id or its display name), a provider that has only one model,
    and a bare model id when only one configured model has it.
    """
    wanted = " ".join(text.split()).casefold()
    if not wanted:
        return None
    for attempt in (
        lambda row: wanted == row.name.casefold(),
        lambda row: wanted in (f"{row.provider_id}/{row.model}".casefold(),
                               f"{row.provider}/{row.model}".casefold()),
        lambda row: wanted in (row.provider_id.casefold(), row.provider.casefold()),
        lambda row: wanted == row.model.casefold(),
    ):
        found = [row for row in rows if attempt(row)]
        if found:
            return found[0] if len(found) == 1 else None
    return None


def similar(rows: list[Choice], text: str, limit: int = 5) -> list[str]:
    """Short references for a near miss, to put in an error's hint."""
    wanted = text.strip().casefold()
    if not wanted:
        return []
    return [row.reference for row in rows
            if wanted in row.name.casefold() or wanted in row.shown.casefold()][:limit]


def pick(rows: list[Choice], *, active: str = "", title: str = "Switch model",
         subtitle: str = "Used for this chat and every future one", out=None) -> Choice | None:
    """Open the arrow-key picker and return the chosen row, or ``None`` on cancel.

    Raises :class:`kova.tui.prompts.Unavailable` where a picker cannot be drawn
    (a pipe, Windows), so the caller can print the list instead.
    """
    from kova.tui import prompts

    labels = [label(row, active=active) for row in rows]
    index = prompts.select(title, labels, default=current(rows, active),
                           subtitle=subtitle, out=out)
    return None if index is None else rows[index]
