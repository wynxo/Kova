"""One validated document for the client's UI preferences.

The preferences live on the server (``KOVA_HOME/preferences.json``) so every
client reads the same values, and ``kova prefs export`` / ``kova prefs import``
move them to another machine as one plain JSON file. The file carries a format
name and version so importing something else fails with a clear message instead
of silently changing nothing.
"""

from __future__ import annotations

import copy
import json
import os
import re
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
    "theme": ("system", "dark", "light"),
    "sidebar": ("open", "collapsed"),
    "notifications": ("on", "off"),
    "reasoning": ("collapsed", "expanded", "hidden"),
    # Typography and density (#179). "default" is today's look exactly, so an
    # older preferences.json without these keys keeps the shipped type scale.
    "text_size": ("small", "default", "large"),
    "density": ("compact", "default", "comfortable"),
    # Corner shape (#176): the theme editor's Simple controls write this
    # alongside density and font. "default" is the shipped radius scale.
    "radius": ("sharp", "default", "round"),
    "font": ("inter", "system", "mono"),
    "motion": ("full", "reduced", "off"),
    # The thinking indicator (#171): the mark plus one small movement, a still
    # mark, or a different animation each reply.
    "thinking_animation": (
        "off", "thunderstorm", "orbit", "collide", "breathe", "charge", "random"
    ),
    # Content width and avatars (#205). "normal" is the shipped 768px column,
    # and avatars are on until they are turned off.
    "chat_width": ("theme", "narrow", "normal", "wide"),
    "avatars": ("theme", "on", "off"),
    "message_style": ("theme", "document", "bubbles", "compact"),
    "message_timestamps": ("never", "hover", "always"),
    # Theme backgrounds (#206): on by default; "off" keeps the theme's colours
    # and leaves every panel solid.
    "backgrounds": ("on", "off"),
    # Power users only (#178): KOVA_HOME/custom.css, never inlined or remote.
    "custom_css": ("on", "off"),
}

# Short free-text lists: name -> (most items, longest item). Used by the start
# screen's suggestion chips; an empty list means "show no suggestions".
TEXT_LISTS: dict[str, tuple[int, int]] = {
    "starters": (4, 120),
}

# Keyboard bindings (#200): action id -> chord, for the actions that differ
# from their default. The ids are the stable names in shortcuts.js, repeated
# here because this document is what every paired device reads: a hand-edited
# file or an old client must not be able to put an unknown action, a broken
# chord, or an unbounded map into it.
SHORTCUT_ACTIONS = frozenset({
    "palette", "new-conversation", "new-incognito", "search-conversations",
    "toggle-sidebar", "toggle-theme", "focus-composer", "stop-reply",
    "shortcuts", "next-conversation", "previous-conversation", "toggle-zen",
})
MAX_BINDING_LENGTH = 40
_BINDING_MODIFIERS = ("mod", "ctrl", "meta", "alt", "shift")
_BINDING_NAMED_KEYS = frozenset({
    "escape", "enter", "tab", "space", "backspace", "delete", "insert", "home",
    "end", "pageup", "pagedown", "arrowup", "arrowdown", "arrowleft",
    "arrowright", *(f"f{index}" for index in range(1, 13)),
})

# Structured maps: name -> the ids it accepts as keys.
MAPS: dict[str, frozenset[str]] = {
    "shortcuts": SHORTCUT_ACTIONS,
}

DEFAULTS: dict[str, Any] = {
    "theme": "system",
    "sidebar": "open",
    "notifications": "off",
    "reasoning": "collapsed",
    "starters": [
        "Plan a small project with me",
        "Summarise a file I send",
        "Help me write a tricky email",
    ],
    "text_size": "default",
    "density": "default",
    "radius": "default",
    "font": "inter",
    "motion": "full",
    "thinking_animation": "thunderstorm",
    "chat_width": "theme",
    "avatars": "theme",
    "message_style": "theme",
    "message_timestamps": "never",
    "shortcuts": {},
    "backgrounds": "on",
    "custom_css": "off",
}

# Preferences that also accept an open-ended id. ``theme`` names one of the
# fixed choices above or an installed theme file (``KOVA_HOME/themes/<id>.json``),
# using the same id rule as ``kova.themes``. The file is not required to exist:
# preferences travel to machines that may not have it yet, and the client falls
# back to the system appearance when it cannot load the theme.
PATTERNS: dict[str, re.Pattern[str]] = {
    "theme": re.compile(r"^[a-z0-9][a-z0-9-]{1,39}$"),
}

# The reasoning switch first shipped as "shown"/"hidden". "shown" is now
# "collapsed", so an older preferences.json, export or open window keeps
# working instead of failing validation.
LEGACY = {"reasoning": {"shown": "collapsed"}}

_KNOWN = ", ".join(sorted({*SCHEMA, *TEXT_LISTS, *MAPS}))


class UiPrefsError(ValueError):
    """A preferences document or file did not validate."""


def _check_binding(value: Any) -> str:
    """Validate one chord in the spelling shortcuts.js writes and reads.

    Modifiers are lowercase in the fixed order mod, ctrl, meta, alt, shift,
    followed by one lowercase key: a printable character, or a named key such
    as ``escape``. A printable key needs Ctrl, Alt or Cmd, because a bare letter
    would steal typing from the message box.
    """
    if not isinstance(value, str):
        raise UiPrefsError("A shortcut binding must be text such as 'mod+shift+k'.")
    text = value.strip().lower()
    if not text or len(text) > MAX_BINDING_LENGTH:
        raise UiPrefsError(
            f"A shortcut binding must be 1-{MAX_BINDING_LENGTH} characters, "
            f"such as 'mod+shift+k'; got {value[:MAX_BINDING_LENGTH]!r}."
        )
    pieces = text.split("+")
    if not all(pieces):
        raise UiPrefsError(f"{text!r} is not a shortcut: every part must be a modifier or one key.")
    modifiers, key = pieces[:-1], pieces[-1]
    canonical = [name for name in _BINDING_MODIFIERS if name in modifiers]
    if modifiers != canonical or len(set(modifiers)) != len(modifiers):
        raise UiPrefsError(
            f"{text!r} is not a shortcut: write its modifiers once, in the order "
            f"{', '.join(_BINDING_MODIFIERS)}."
        )
    if key in _BINDING_MODIFIERS:
        raise UiPrefsError(f"{text!r} is not a shortcut: it has no key after its modifiers.")
    if key in _BINDING_NAMED_KEYS:
        return "+".join([*modifiers, key])
    if not (len(key) == 1 and 0x20 < ord(key) < 0x7F):
        raise UiPrefsError(f"{text!r} is not a shortcut: {key!r} is not a usable key.")
    if not {"mod", "ctrl", "meta", "alt"} & set(modifiers):
        raise UiPrefsError(
            f"{text!r} is not a shortcut: a letter or symbol needs Ctrl, Alt or Cmd."
        )
    return "+".join([*modifiers, key])


def _check_shortcut_map(value: Any) -> dict[str, str]:
    """Validate the shared action -> chord map: known ids only, bounded size."""
    if not isinstance(value, dict):
        raise UiPrefsError(
            "Preference 'shortcuts' must be a JSON object of action ids to keys."
        )
    if len(value) > len(SHORTCUT_ACTIONS):
        raise UiPrefsError(
            f"Preference 'shortcuts' holds at most {len(SHORTCUT_ACTIONS)} bindings; "
            f"got {len(value)}."
        )
    unknown = sorted(set(value) - SHORTCUT_ACTIONS)
    if unknown:
        raise UiPrefsError(
            f"Unknown shortcut action{'s' if len(unknown) > 1 else ''}: "
            f"{', '.join(unknown)}. Known actions: {', '.join(sorted(SHORTCUT_ACTIONS))}."
        )
    return {action: _check_binding(value[action]) for action in sorted(value)}


def _check_text_list(name: str, value: Any) -> list[str]:
    """Validate a short list of prompts: trimmed, de-duplicated, bounded."""
    limit, length = TEXT_LISTS[name]
    if not isinstance(value, list):
        raise UiPrefsError(
            f"Preference {name!r} must be a list of at most {limit} short "
            f"texts, each at most {length} characters."
        )
    checked: list[str] = []
    for item in value:
        if not isinstance(item, str):
            raise UiPrefsError(f"Preference {name!r} accepts text items only.")
        text = " ".join(item.split())
        if not text:
            continue
        if len(text) > length:
            raise UiPrefsError(
                f"Preference {name!r} accepts items up to {length} characters; "
                f"got {len(text)}."
            )
        if text not in checked:
            checked.append(text)
    if len(checked) > limit:
        raise UiPrefsError(
            f"Preference {name!r} accepts at most {limit} items; got {len(checked)}."
        )
    return checked


def _check_values(values: Any) -> dict[str, Any]:
    """Validate a plain mapping of preference names to accepted values."""
    if not isinstance(values, dict):
        raise UiPrefsError("Preferences must be a JSON object.")
    unknown = sorted(set(values) - set(SCHEMA) - set(TEXT_LISTS) - set(MAPS))
    if unknown:
        raise UiPrefsError(
            f"Unknown preference{'s' if len(unknown) > 1 else ''}: "
            f"{', '.join(unknown)}. Known preferences: {_KNOWN}."
        )
    checked: dict[str, Any] = {}
    for name, value in values.items():
        if name in TEXT_LISTS:
            checked[name] = _check_text_list(name, value)
            continue
        if name in MAPS:
            checked[name] = _check_shortcut_map(value)
            continue
        if isinstance(value, str):
            value = LEGACY.get(name, {}).get(value, value)
        allowed = SCHEMA[name]
        pattern = PATTERNS.get(name)
        if value in allowed:
            pass
        elif pattern is not None and isinstance(value, str) and pattern.match(value):
            pass
        else:
            extra = " or an installed theme id" if pattern is not None else ""
            raise UiPrefsError(
                f"Preference {name!r} must be one of {', '.join(allowed)}{extra}; "
                f"got {value!r}."
            )
        checked[name] = value
    return checked


def _check_themes(value: Any) -> list[str]:
    """Validate the exported list of installed theme ids (names only)."""
    if not isinstance(value, list):
        raise UiPrefsError("Preferences file 'themes' must be a list of theme ids.")
    if len(value) > 200:
        raise UiPrefsError("Preferences file 'themes' holds at most 200 theme ids.")
    checked: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise UiPrefsError("Preferences file 'themes' must be a list of theme ids.")
        name = item.strip()
        if len(name) > 64:
            raise UiPrefsError(f"Theme id {name!r} is too long (at most 64 characters).")
        if name not in checked:
            checked.append(name)
    return checked


def document(values: dict[str, Any], themes: list[str] | None = None) -> dict[str, Any]:
    """The export shape: a named, versioned envelope around the preferences."""
    payload: dict[str, Any] = {
        "format": PREFS_FORMAT,
        "version": PREFS_VERSION,
        "preferences": dict(values),
    }
    if themes is not None:
        payload["themes"] = list(themes)
    return payload


def parse(payload: Any) -> dict[str, Any]:
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
    if "themes" in payload:
        _check_themes(payload["themes"])
    return _check_values(payload["preferences"])


def parse_export(payload: Any) -> tuple[dict[str, Any], list[str]]:
    """Validate an exported document, returning ``(preferences, theme ids)``."""
    values = parse(payload)
    themes: list[str] = []
    if isinstance(payload, dict) and "themes" in payload:
        themes = _check_themes(payload["themes"])
    return values, themes


def diff_preferences(current: dict[str, Any], incoming: dict[str, Any]) -> list[tuple[str, Any, Any]]:
    """Keys that would change, as ``(name, old, new)`` sorted by name."""
    rows: list[tuple[str, Any, Any]] = []
    for name in sorted(set(current) | set(incoming)):
        old, new = current.get(name), incoming.get(name)
        if old != new:
            rows.append((name, old, new))
    return rows


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


def _read_payload(path: Path | str) -> Any:
    """Read one preferences file and return its raw JSON payload."""
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError as exc:
        raise UiPrefsError(f"Could not read {path}: {exc.strerror or exc}") from exc
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise UiPrefsError(
            f"{path} is not valid JSON: {exc.msg} at line {exc.lineno} column {exc.colno}."
        ) from exc


def read_file(path: Path | str) -> dict[str, Any]:
    """Read and validate one preferences file."""
    payload = _read_payload(path)
    try:
        return parse(payload)
    except UiPrefsError as exc:
        raise UiPrefsError(f"{path}: {exc}") from exc


def read_export(path: Path | str) -> tuple[dict[str, Any], list[str]]:
    """Read one exported file, returning ``(preferences, theme ids)``."""
    payload = _read_payload(path)
    try:
        return parse_export(payload)
    except UiPrefsError as exc:
        raise UiPrefsError(f"{path}: {exc}") from exc


def write_file(
    values: dict[str, Any], path: Path | str | None = None, themes: list[str] | None = None
) -> Path:
    """Write the versioned document atomically and return the path used."""
    target = target_path(path)
    if themes is not None:
        themes = _check_themes(themes)
    text = json.dumps(document(_check_values(values), themes), indent=2) + "\n"
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

    def read(self) -> dict[str, Any]:
        """Current preferences, with defaults filled in for keys it predates."""
        if not self.path.exists():
            return copy.deepcopy(DEFAULTS)
        try:
            saved = read_file(self.path)
        except UiPrefsError as exc:
            raise UiPrefsError(
                f"The saved UI preferences are not valid ({exc}). "
                f"Fix or delete {self.path.name}."
            ) from exc
        return {**copy.deepcopy(DEFAULTS), **saved}

    def update(self, patch: Any) -> dict[str, Any]:
        """Merge validated changes over the current values and save them."""
        changes = _check_values(patch)
        values = {**self.read(), **changes}
        write_file(values, self.path)
        return values
