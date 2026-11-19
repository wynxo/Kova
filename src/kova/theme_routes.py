"""List, preview and install themes through the authenticated API."""

from __future__ import annotations

import json
import os
import re
import tempfile
from importlib.resources import files
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse

from kova import pairing, themes
from kova.theme_contrast import contrast_failures

MAX_THEME_BYTES = 32 * 1024
_WINDOWS_DEVICE = re.compile(r"con|prn|aux|nul|(?:com|lpt)[1-9]")
_require_settings_write = pairing.require_capability("settings:write")
_SWATCH_ROLES = ("bg.base", "bg.raised", "fg.default", "accent.default")


async def _bounded_body(request: Request) -> bytearray:
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > MAX_THEME_BYTES:
            raise HTTPException(413, "Theme files must be at most 32 KiB.")
        body.extend(chunk)
    return body


def _install(home: Path, theme_id: str, serialized: bytes) -> None:
    directory = themes.theme_dir(home)
    try:
        if directory.resolve() != home.resolve() / "themes":
            raise HTTPException(409, "Kova's themes folder must not point to another folder.")
        directory.mkdir(exist_ok=True)
        path = directory / f"{theme_id}.json"
        # Publish the complete file in one step, without replacing an existing
        # file (or following a symlink), even for simultaneous saves.
        fd, staged = tempfile.mkstemp(prefix=".theme-", dir=directory)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(serialized)
                stream.flush()
                os.fsync(stream.fileno())
            os.link(staged, path)
        finally:
            os.unlink(staged)
    except FileExistsError as exc:
        raise HTTPException(409, "That theme ID is already installed. Choose a new ID.") from exc
    except (OSError, RuntimeError) as exc:
        raise HTTPException(500, "Could not write to Kova's themes folder.") from exc


def _prepare_theme(payload: Any) -> tuple[dict, bytes]:
    try:
        # Round-trip UTF-8 before returning metadata; reject unpaired surrogates.
        serialized = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    except (ValueError, RecursionError) as exc:
        raise HTTPException(422, "Choose a valid UTF-8 theme JSON file.") from exc
    if len(serialized) > MAX_THEME_BYTES:
        raise HTTPException(413, "Theme files must be at most 32 KiB.")

    try:
        theme = themes.validate(payload)
        if "extends" in theme and theme["extends"] not in themes.BUILTIN:
            raise themes.ThemeError("extends must be kova-dark or kova-light, or be left out.")
        if "$schema" in theme and not isinstance(theme["$schema"], str):
            raise themes.ThemeError("$schema must be a string.")
        ident = theme["id"]
        if ident in themes.BUILTIN or ident in themes.CHOICES or _WINDOWS_DEVICE.fullmatch(ident):
            raise themes.ThemeError("That theme ID is reserved. Choose a different ID.")
        resolved = themes.resolve(theme)
    except themes.ThemeError as exc:
        raise HTTPException(422, str(exc)) from exc

    resolved.pop("terminal")
    failures = contrast_failures(resolved, theme.get("terminal"))
    if failures:
        raise HTTPException(422, failures)
    return {"id": ident, "appearance": theme["appearance"], "tokens": resolved}, serialized


def _gallery_documents() -> dict[str, Any]:
    try:
        resource = files("kova").joinpath("theme_gallery.json")
        documents = json.loads(resource.read_text(encoding="utf-8"))
    except (OSError, ValueError, RecursionError) as exc:
        raise HTTPException(500, "Could not read Kova's bundled theme gallery.") from exc
    if not isinstance(documents, dict):
        raise HTTPException(500, "Kova's bundled theme gallery is not valid.")
    return documents


def _prepare_gallery_theme(theme_id: str, document: Any) -> tuple[dict, bytes]:
    result, serialized = _prepare_theme(document)
    if result["id"] != theme_id:
        raise HTTPException(422, "The theme document does not match its gallery ID.")
    return result, serialized


def install_theme_routes(api: APIRouter, home: Path) -> None:
    @api.get("/themes")
    def list_themes() -> list[dict]:
        """Every theme Kova can offer; a broken user file is listed with its error."""
        try:
            return themes.discover(home)
        except themes.ThemeError as exc:
            raise HTTPException(500, str(exc)) from exc

    @api.get("/themes/gallery")
    def list_gallery(installed: bool = False) -> list[dict] | dict:
        """Show bundled themes, or resolve a custom theme literally named gallery."""
        if installed:
            return show_theme("gallery")
        installed_ids = {entry["id"] for entry in list_themes() if not entry["error"]}
        entries = []
        for theme_id, document in _gallery_documents().items():
            try:
                result, _ = _prepare_gallery_theme(theme_id, document)
            except HTTPException as exc:
                if exc.status_code not in (413, 422):
                    raise
                continue
            entries.append({
                **result,
                "name": document["name"],
                "description": document.get("description", ""),
                "swatches": [result["tokens"][role] for role in _SWATCH_ROLES],
                "installed": theme_id in installed_ids,
            })
        return entries

    @api.post("/themes/gallery/{theme_id}/install", status_code=201,
              dependencies=[Depends(_require_settings_write)])
    def install_gallery_theme(theme_id: str) -> dict:
        """Install a validated copy of a bundled theme without overwriting a file."""
        documents = _gallery_documents()
        if theme_id not in documents:
            raise HTTPException(404, "No theme in the gallery with that ID. Run `kova themes gallery`.")
        result, serialized = _prepare_gallery_theme(theme_id, documents[theme_id])
        _install(home, result["id"], serialized)
        return result

    @api.get("/themes/{theme_id}")
    def show_theme(theme_id: str) -> dict:
        """One theme with its tokens resolved over the base it extends."""
        try:
            return themes.get(theme_id, home)
        except themes.ThemeError as exc:
            raise HTTPException(404, str(exc)) from exc

    @api.post("/themes", status_code=201)
    async def save_theme(request: Request, validate_only: bool = False) -> JSONResponse:
        """Check a draft without writing, or save it as a new theme."""
        if not validate_only:
            _require_settings_write(request)
        body = await _bounded_body(request)
        try:
            payload = json.loads(body.decode("utf-8"))
        except (ValueError, RecursionError) as exc:
            raise HTTPException(422, "Choose a valid UTF-8 theme JSON file.") from exc

        result, serialized = _prepare_theme(payload)
        if validate_only:
            return JSONResponse(result)
        _install(home, result["id"], serialized)
        return JSONResponse(result, status_code=201)
