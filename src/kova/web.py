"""Offline browser UI and short-lived local browser sessions.

The launcher authenticates with the normal bearer token to mint a one-use ticket.
Only that ticket goes in the URL fragment (never an HTTP URL or access log).
The page exchanges it for an HttpOnly cookie, then removes the fragment.
"""

from __future__ import annotations

import asyncio
import json
import secrets
import time
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import Depends, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.datastructures import UploadFile
from starlette.formparsers import MultiPartException

COOKIE = "kova_session"
SESSION_SECONDS = 12 * 3600
TICKET_SECONDS = 60
SHARE_SECONDS = 300
SHARE_MAX_BYTES = 2 * 1024 * 1024
# The composer sends at most three attachments per turn; a share may not stage
# more files than the composer can ingest.
SHARE_MAX_FILES = 3
ASSETS = Path(__file__).with_name("web_assets")


class LoginBody(BaseModel):
    ticket: str = Field(min_length=1, max_length=128)


def require_same_origin(request: Request) -> None:
    """Reject rebinding and cross-origin browser requests, including other ports."""
    # The LAN listener has already pinned Host to its own address (kova.pairing).
    if not request.scope.get("kova_lan") and \
            urlsplit(str(request.url)).hostname not in {"127.0.0.1", "localhost", "::1"}:
        raise HTTPException(403, "browser access is local only")
    origin = request.headers.get("origin")
    expected = str(request.base_url).rstrip("/")
    if origin is not None and origin != expected:
        raise HTTPException(403, "cross-origin request rejected")
    if request.headers.get("sec-fetch-site", "none") not in {"same-origin", "none"}:
        raise HTTPException(403, "cross-origin request rejected")


def _prune(entries: dict[str, float]) -> None:
    for key, expires in list(entries.items()):
        if expires <= time.monotonic():
            entries.pop(key, None)


def require_browser_session(request: Request) -> None:
    if request.scope.get("kova_lan"):
        # A phone on the Wi-Fi: only a paired device's cookie counts, never a session.
        from kova.pairing import DEVICE_COOKIE, NOT_PAIRED
        device = request.app.state.devices.find(request.cookies.get(DEVICE_COOKIE))
        if device is None:
            raise HTTPException(401, NOT_PAIRED)
        request.app.state.devices.touch(device)
        require_same_origin(request)
        return
    sessions = request.app.state.web_sessions
    _prune(sessions)
    # A draft chat belongs to one unexpired share. Any authenticated browser
    # call is a good moment to drop the ones whose five minutes are up, rather
    # than waiting for the next share.
    _prune_shares(request.app)
    if request.cookies.get(COOKIE, "") not in sessions:
        raise HTTPException(401, "Open kova-web to connect this window.")
    require_same_origin(request)


def _share_owner(request: Request) -> str:
    """The browser session or paired device that submitted a share."""
    return request.cookies.get(COOKIE) or request.cookies.get("kova_device") or ""


def _drop_share(app, entry: dict) -> None:
    """Remove timing and any draft chat a share never handed to a composer."""
    if entry.get("chat"):
        app.state.store.delete_chat(entry["chat"]["id"])


def _prune_shares(app) -> None:
    entries = app.state.web_shares
    now = time.monotonic()
    for key, entry in list(entries.items()):
        if entry["expires"] <= now:
            entries.pop(key, None)
            _drop_share(app, entry)


def install_web(app, require_bearer) -> None:
    """Register public packaged assets and bearer-bootstrapped browser sessions."""
    app.state.web_shares = {}
    app.state.web_sessions = {}
    app.state.web_tickets = {}

    @app.middleware("http")
    async def browser_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Cache-Control"] = "no-store"
        if request.url.path.startswith("/app"):
            response.headers["Content-Security-Policy"] = (
                "default-src 'none'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
                "connect-src 'self'; img-src 'self' data:; font-src 'self'; "
                "frame-ancestors 'none'; base-uri 'none'; form-action 'self'; worker-src 'self'; manifest-src 'self'"
            )
            response.headers["X-Frame-Options"] = "DENY"
        return response

    @app.get("/app", include_in_schema=False)
    async def page():
        return FileResponse(ASSETS / "index.html")

    @app.get("/app/manifest.webmanifest", include_in_schema=False)
    async def manifest():
        from . import theme
        data = json.loads((ASSETS / "manifest.webmanifest").read_text())
        data.update(name=app.state.config.persona_name, short_name=app.state.config.persona_name,
                    theme_color=theme.ACCENT, background_color=theme.SURFACE)
        return JSONResponse(data, media_type="application/manifest+json")

    @app.get("/app/sw.js", include_in_schema=False)
    async def worker():
        return FileResponse(ASSETS / "sw.js", media_type="application/javascript")

    @app.post("/app/share", dependencies=[Depends(require_browser_session)])
    async def share(request: Request):
        """Turn a platform share into one-use composer text and native attachments."""
        # Bound the whole request before parsing multipart (including chunked bodies).
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > SHARE_MAX_BYTES:
                raise HTTPException(413, "Share is too large (2 MB maximum).")
        request._body = bytes(body)
        from kova import attachments
        from kova.modes import DEFAULT_MODE

        parts: list[str] = []
        uploads: list[UploadFile] = []
        rows: list[dict] = []
        chat = None
        try:
            async with request.form(max_files=SHARE_MAX_FILES, max_fields=3) as form:
                parts = [str(form.get(key, "")).strip() for key in ("title", "text", "url")]
                uploads = [file for file in form.getlist("files")
                           if isinstance(file, UploadFile) and file.filename]
                chat_title = (parts[0] or parts[1] or parts[2] or "Shared files")[:80]
                for file in uploads:
                    content = await file.read(attachments.MAX_BYTES + 1)
                    if chat is None:
                        chat = app.state.store.create_chat(
                            chat_title, mode=DEFAULT_MODE, web=True, permission="manual",
                            cwd=str(app.state.config.files_path.resolve()))
                    rows.append(await asyncio.to_thread(
                        attachments.save, app.state.config.home, app.state.store,
                        chat["id"], file.filename, content, now=app.state.now))
        except MultiPartException as exc:
            if chat is not None:
                app.state.store.delete_chat(chat["id"])
            raise HTTPException(400, str(exc)) from None
        except attachments.AttachmentError as exc:
            if chat is not None:
                app.state.store.delete_chat(chat["id"])
            # A quota rejection is a conflict with the chat's current usage;
            # any other attachment problem is a bad request.
            status = 409 if isinstance(exc, attachments.AttachmentQuotaError) else 400
            raise HTTPException(status, str(exc)) from None
        except Exception:
            if chat is not None:
                app.state.store.delete_chat(chat["id"])
            raise

        _prune_shares(app)
        entries = app.state.web_shares
        if len(entries) >= 32:
            if chat is not None:
                app.state.store.delete_chat(chat["id"])
            raise HTTPException(429, "Too many pending shares.")
        token = secrets.token_urlsafe(32)
        if chat is not None:
            try:
                app.state.store.add_share_draft(chat["id"])
            except Exception:
                app.state.store.delete_chat(chat["id"])
                raise
        entries[token] = {
            "expires": time.monotonic() + SHARE_SECONDS,
            "owner": _share_owner(request),
            "text": "\n\n".join(part for part in parts if part),
            "chat": chat,
            "attachments": rows if chat is not None else [],
        }
        return RedirectResponse("/app#share=" + token, status_code=303)

    @app.post("/app/share/{token}", dependencies=[Depends(require_browser_session)])
    async def consume_share(token: str, request: Request):
        _prune_shares(app)
        entries = app.state.web_shares
        entry = entries.get(token)
        if entry is None or entry["owner"] != _share_owner(request):
            raise HTTPException(404, "Share expired or already used.")
        # The draft becomes an ordinary chat here. Retire the journal entry
        # first so an interrupted handoff can never make a restart delete a
        # conversation that may already be in use.
        if entry.get("chat"):
            app.state.store.drop_share_draft(entry["chat"]["id"])
        entries.pop(token)
        return {"text": entry["text"], "chat": entry["chat"],
                "attachments": entry["attachments"]}

    @app.post("/app/session", dependencies=[Depends(require_bearer)])
    async def issue_ticket():
        tickets = app.state.web_tickets
        _prune(tickets)
        if len(tickets) >= 32:
            tickets.pop(next(iter(tickets)))
        ticket = secrets.token_urlsafe(32)
        tickets[ticket] = time.monotonic() + TICKET_SECONDS
        return {"ticket": ticket}

    @app.post("/app/login")
    async def login(request: Request, body: LoginBody):
        require_same_origin(request)
        _prune(app.state.web_tickets)
        if app.state.web_tickets.pop(body.ticket, None) is None:
            raise HTTPException(401, "Connection link expired. Open kova-web again.")
        sessions = app.state.web_sessions
        _prune(sessions)
        if len(sessions) >= 64:
            sessions.pop(next(iter(sessions)))
        session = secrets.token_urlsafe(32)
        sessions[session] = time.monotonic() + SESSION_SECONDS
        response = JSONResponse({"ok": True})
        # HTTP is deliberately loopback-only, so Secure cookies cannot be required.
        response.set_cookie(COOKIE, session, httponly=True, samesite="strict",
                            max_age=SESSION_SECONDS, path="/")
        return response

    @app.post("/app/logout", dependencies=[Depends(require_browser_session)])
    async def logout(request: Request):
        response = JSONResponse({"ok": True})
        if request.scope.get("kova_lan"):
            # "Disconnect" on a phone unpairs it for good.
            from kova.pairing import DEVICE_COOKIE
            device = app.state.devices.find(request.cookies.get(DEVICE_COOKIE))
            if device is not None:
                app.state.devices.revoke(device["id"])
            response.delete_cookie(DEVICE_COOKIE, path="/")
            return response
        app.state.web_sessions.pop(request.cookies.get(COOKIE), None)
        response.delete_cookie(COOKIE, path="/")
        return response

    app.mount("/app/assets", StaticFiles(directory=ASSETS), name="app-assets")
