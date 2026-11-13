"""Offline browser UI and short-lived local browser sessions.

The launcher authenticates with the normal bearer token to mint a one-use ticket.
Only that ticket goes in the URL fragment (never an HTTP URL or access log).
The page exchanges it for an HttpOnly cookie, then removes the fragment.
"""

from __future__ import annotations

import secrets
import time
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import Depends, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

COOKIE = "kova_session"
SESSION_SECONDS = 12 * 3600
TICKET_SECONDS = 60
ASSETS = Path(__file__).with_name("web_assets")


class LoginBody(BaseModel):
    ticket: str = Field(min_length=1, max_length=128)


def require_same_origin(request: Request) -> None:
    """Reject rebinding and cross-origin browser requests, including other ports."""
    if urlsplit(str(request.url)).hostname not in {"127.0.0.1", "localhost", "::1"}:
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
    sessions = request.app.state.web_sessions
    _prune(sessions)
    if request.cookies.get(COOKIE, "") not in sessions:
        raise HTTPException(401, "Open kova-web to connect this window.")
    require_same_origin(request)


def install_web(app, require_bearer) -> None:
    """Register public packaged assets and bearer-bootstrapped browser sessions."""
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
                "frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
            )
            response.headers["X-Frame-Options"] = "DENY"
        return response

    @app.get("/app", include_in_schema=False)
    async def page():
        return FileResponse(ASSETS / "index.html")

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
        app.state.web_sessions.pop(request.cookies.get(COOKIE), None)
        response = JSONResponse({"ok": True})
        response.delete_cookie(COOKIE, path="/")
        return response

    app.mount("/app/assets", StaticFiles(directory=ASSETS), name="app-assets")
