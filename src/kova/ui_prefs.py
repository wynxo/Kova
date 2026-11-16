"""One validated document for the client's UI preferences.

The preferences live on the server (``KOVA_HOME/preferences.json``) so every
client reads the same values, and ``kova prefs export`` / ``kova prefs import``
move them to another machine as one plain JSON file. The file carries a format
name and version so importing something else fails with a clear message instead
of silently changing nothing.
"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any

PREFS_FORMAT = "kova-ui-preferences"
PREFS_VERSION = 1
DEFAULT_PREFIX = "Kova-ui-preferences-"

# Every preference a client can change, with the values it accepts. Add a key
# here (and bump PREFS_VERSION if the meaning changes) when the UI gains one.
SCHEMA: dict[str, tuple[str, ...]] = {
    "theme": ("dark", "light"),
    "sidebar": ("open", "collapsed"),
    "notifications": ("on", "off"),
}

DEFAULTS: dict[str, str] = {
    "theme": "dark",
    "sidebar": "open",
    "notifications": "off",
}

_KNOWN = ", ".join(sorted(SCHEMA))


class UiPrefsError(ValueError):
    """A preferences document or file did not validate."""


def _check_values(values: Any) -> dict[str, str]:
    """Validate a plain mapping of preference names to accepted values."""
    if not isinstance(values, dict):
        raise UiPrefsError("Preferences must be a JSON object.")
    unknown = sorted(set(values) - set(SCHEMA))
    if unknown:
        raise UiPrefsError(
            f"Unknown preference{'s' if len(unknown) > 1 else ''}: "
            f"{', '.join(unknown)}. Known preferences: {_KNOWN}."
        )
    checked: dict[str, str] = {}
    for name, value in values.items():
        allowed = SCHEMA[name]
        if value not in allowed:
            raise UiPrefsError(
                f"Preference {name!r} must be one of {', '.join(allowed)}; "
                f"got {value!r}."
            )
        checked[name] = value
    return checked


def document(values: dict[str, str]) -> dict[str, Any]:
    """The export shape: a named, versioned envelope around the preferences."""
    return {
        "format": PREFS_FORMAT,
        "version": PREFS_VERSION,
        "preferences": dict(values),
    }


def parse(payload: Any) -> dict[str, str]:
    """Validate an exported document, returning only its preferences.

    Raises :class:`UiPrefsError` with a sentence naming what is wrong, so a
    hand-edited or unrelated file never half-imports.
    """
    if not isinstance(payload, dict):
        raise UiPrefsError("Preferences file must contain one JSON object.")
    if payload.get("format") != PREFS_FORMAT:
        raise UiPrefsError(
            f"Not a Kova UI preferences file: expected format {PREFS_FORMAT!r}."
        )
    version = payload.get("version")
    if version != PREFS_VERSION:
        raise UiPrefsError(
            f"Unsupported preferences version {version!r}; this Kova reads "
            f"version {PREFS_VERSION}."
        )
    if "preferences" not in payload:
        raise UiPrefsError("Preferences file has no 'preferences' object.")
    return _check_values(payload["preferences"])


def default_export_path(now: datetime | None = None) -> Path:
    """``~/Kova-ui-preferences-YYYY-MM-DD.json``, next to Kova's backups."""
    day = (now or datetime.now()).strftime("%Y-%m-%d")
    return Path.home() / f"{DEFAULT_PREFIX}{day}.json"


def target_path(path: Path | str | None) -> Path:
    """Resolve the requested export path, adding the default name or ``.json``."""
    if path is None:
        return default_export_path()
    target = Path(path).expanduser()
    if target.is_dir():
        return target / default_export_path().name
    if not target.suffix:
        target = target.with_suffix(".json")
    return target


def read_file(path: Path | str) -> dict[str, str]:
    """Read and validate one preferences file."""
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError as exc:
        raise UiPrefsError(f"Could not read {path}: {exc.strerror or exc}") from exc
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise UiPrefsError(
            f"{path} is not valid JSON: {exc.msg} at line {exc.lineno} column {exc.colno}."
        ) from exc
    try:
        return parse(payload)
    except UiPrefsError as exc:
        raise UiPrefsError(f"{path}: {exc}") from exc


def write_file(values: dict[str, str], path: Path | str | None = None) -> Path:
    """Write the versioned document atomically and return the path used."""
    target = target_path(path)
    text = json.dumps(document(_check_values(values)), indent=2) + "\n"
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".kova-prefs-", dir=target.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return target


class Preferences:
    """The server-side preferences object, stored as one small JSON file."""

    def __init__(self, home: Path | str):
        self.path = Path(home) / "preferences.json"

    def read(self) -> dict[str, str]:
        """Current preferences; defaults when nothing has been saved yet."""
        if not self.path.exists():
            return dict(DEFAULTS)
        try:
            return read_file(self.path)
        except UiPrefsError as exc:
            raise UiPrefsError(
                f"The saved UI preferences are not valid ({exc}). "
                f"Fix or delete {self.path.name}."
            ) from exc

    def update(self, patch: Any) -> dict[str, str]:
        """Merge validated changes over the current values and save them."""
        changes = _check_values(patch)
        values = {**self.read(), **changes}
        write_file(values, self.path)
        return values
