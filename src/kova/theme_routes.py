"""Validate a draft or install one new theme through the authenticated API."""

from __future__ import annotations

import json
import os
import re
import tempfile
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse

from kova import pairing, themes
from kova.theme_contrast import contrast_failures

MAX_THEME_BYTES = 32 * 1024
_WINDOWS_DEVICE = re.compile(r"con|prn|aux|nul|(?:com|lpt)[1-9]")
_require_settings_write = pairing.require_capability("settings:write")


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


def install_theme_routes(api: APIRouter, home: Path) -> None:
    @api.get("/themes/assets/{name}")
    async def theme_asset(name: str) -> FileResponse:
        """One background image from KOVA_HOME/themes/assets, authenticated.

        Registered before ``/themes/{theme_id}`` so ``assets`` is never read as
        a theme id. The helper confines the name to the assets folder and the
        response is sent with its real type; the app middleware adds nosniff
        and no-store to every response.
        """
        try:
            path = themes.background_asset(home, name)
        except themes.ThemeAssetTooLarge as exc:
            raise HTTPException(413, str(exc)) from exc
        except themes.ThemeError as exc:
            raise HTTPException(404, str(exc)) from exc
        return FileResponse(path, media_type=themes.BACKGROUND_TYPES[path.suffix])

    @api.post("/themes", status_code=201)
    async def save_theme(request: Request, validate_only: bool = False) -> JSONResponse:
        """Check a draft without writing, or save it as a new theme."""
        if not validate_only:
            _require_settings_write(request)
        body = await _bounded_body(request)
        try:
            payload = json.loads(body.decode("utf-8"))
            # Round-trip UTF-8 before returning any errors or metadata to the client.
            # This also rejects JSON escape sequences for unpaired surrogates.
            serialized = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        except (ValueError, RecursionError) as exc:
            raise HTTPException(422, "Choose a valid UTF-8 theme JSON file.") from exc

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

        result = {"id": ident, "appearance": theme["appearance"], "tokens": resolved}
        if validate_only:
            return JSONResponse(result)
        _install(home, ident, serialized)
        return JSONResponse(result, status_code=201)
