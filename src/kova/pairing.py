"""Phone pairing: use Kova from a phone on the same Wi-Fi, without opening it to the internet.

How it fits together:

* The main server always listens on 127.0.0.1. When phone access is on
  (``bind = "lan"`` in config.toml) a second listener serves the same app on the
  computer's private LAN address only, never on 0.0.0.0, so VPNs, hotspots and
  public interfaces stay closed.
* ``kova pair`` (or *Connect your phone* in the web/desktop app) mints a one-use,
  two-minute pairing secret. Only that secret goes in the QR code, inside the
  URL fragment, which browsers never send to a server or write to a log.
* The phone's page exchanges the secret for a long-lived device token in an
  HttpOnly cookie. Only a SHA-256 of the token is stored (``devices.json``),
  so the file alone cannot be replayed. Devices survive restarts and are
  revoked with ``kova devices revoke``.
* Requests that arrive on the LAN listener are held to stricter rules than
  local ones: a private-range client, the exact LAN ``Host``, a paired
  device's cookie, and no bearer token or local-only routes (pairing, devices,
  turning phone access on or off).
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import ipaddress
import json
import os
import secrets
import socket
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from fastapi import Request
from pydantic import BaseModel, Field

from kova.config import private_write

DEVICE_COOKIE = "kova_device"
DEVICE_SECONDS = 400 * 24 * 3600  # the longest lifetime browsers keep a cookie
PAIR_SECONDS = 120
MAX_OPEN_CODES = 4
FAILED_LIMIT = 10          # failed claims per client address ...
FAILED_WINDOW = 300.0      # ... within this many seconds
LAST_SEEN_EVERY = 300.0    # write a device's last_seen at most this often


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def is_private_client(host: str | None) -> bool:
    """A LAN or loopback peer; anything routable on the internet is refused."""
    try:
        address = ipaddress.ip_address((host or "").split("%")[0])
    except ValueError:
        return False
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped:
        address = address.ipv4_mapped
    return address.is_private or address.is_loopback or address.is_link_local


def lan_address() -> str | None:
    """This computer's private IPv4 address on the local network, or None.

    Asks the OS which source address it would use for an outside route; a UDP
    ``connect`` sends no packet. Only a private, non-loopback address counts.
    """
    override = os.environ.get("KOVA_LAN_ADDRESS")
    if override:
        return override if is_private_client(override) else None
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
        try:
            probe.connect(("10.254.254.254", 9))
            address = probe.getsockname()[0]
        except OSError:
            return None
    parsed = ipaddress.ip_address(address)
    if parsed.is_private and not parsed.is_loopback and not parsed.is_link_local:
        return address
    return None


def device_name(user_agent: str) -> str:
    """A friendly default name for a phone, from its browser's user agent."""
    ua = user_agent or ""
    for needle, name in (("iPhone", "iPhone"), ("iPad", "iPad"), ("Android", "Android phone"),
                         ("Windows", "Windows PC"), ("Macintosh", "Mac"), ("CrOS", "Chromebook"),
                         ("Linux", "Linux device")):
        if needle in ua:
            return name
    return "Phone"


class Devices:
    """Paired devices, persisted as hashed tokens in ``devices.json`` (0600)."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self._seen: dict[str, float] = {}
        self.items: list[dict] = []
        if self.path.exists():
            try:
                self.items = [d for d in json.loads(self.path.read_text()) if isinstance(d, dict)]
            except (OSError, ValueError):
                self.items = []

    def _save(self) -> None:
        private_write(self.path, json.dumps(self.items, indent=2))
        if os.name != "nt":
            os.chmod(self.path, 0o600)

    def add(self, name: str) -> tuple[dict, str]:
        """Register a device; returns its public record and its one-time token."""
        token = secrets.token_urlsafe(32)
        taken = {d["name"] for d in self.items}
        unique, n = name, 2
        while unique in taken:
            unique, n = f"{name} {n}", n + 1
        device = {"id": secrets.token_hex(4), "name": unique, "token_sha256": _hash(token),
                  "created": _now_iso(), "last_seen": _now_iso()}
        self.items.append(device)
        self._save()
        return self.public(device), token

    def find(self, token: str | None) -> dict | None:
        """The device this token belongs to, compared in constant time."""
        if not token:
            return None
        digest = _hash(token)
        match = None
        for device in self.items:
            if hmac.compare_digest(device["token_sha256"], digest):
                match = device
        return match

    def touch(self, device: dict) -> None:
        now = time.monotonic()
        if now - self._seen.get(device["id"], -LAST_SEEN_EVERY) >= LAST_SEEN_EVERY:
            self._seen[device["id"]] = now
            device["last_seen"] = _now_iso()
            try:
                self._save()
            except OSError:
                pass  # a read-only home must not lock the phone out

    def revoke(self, device_id: str | None = None) -> int:
        """Forget one device (by id), or every device when ``device_id`` is None."""
        before = len(self.items)
        self.items = [d for d in self.items if device_id is not None and d["id"] != device_id]
        removed = before - len(self.items)
        if removed:
            self._save()
        return removed

    @staticmethod
    def public(device: dict) -> dict:
        return {key: device[key] for key in ("id", "name", "created", "last_seen")}

    def list(self) -> list[dict]:
        return [self.public(d) for d in self.items]


@dataclass
class PairCode:
    id: str
    secret: str
    expires: float
    device: dict | None = None


@dataclass
class Pairing:
    """Short-lived, one-use pairing secrets plus a limit on failed claims."""

    codes: dict[str, PairCode] = field(default_factory=dict)
    failures: dict[str, list[float]] = field(default_factory=dict)

    def _prune(self) -> None:
        now = time.monotonic()
        for key, code in list(self.codes.items()):
            # Claimed codes stay a little longer so the computer can see the result.
            if code.expires + (PAIR_SECONDS if code.device else 0) <= now:
                self.codes.pop(key, None)

    def start(self) -> PairCode:
        self._prune()
        while len(self.codes) >= MAX_OPEN_CODES:
            self.codes.pop(next(iter(self.codes)))
        code = PairCode(secrets.token_hex(6), secrets.token_urlsafe(18), time.monotonic() + PAIR_SECONDS)
        self.codes[code.id] = code
        return code

    def status(self, code_id: str) -> tuple[str, PairCode | None]:
        self._prune()
        code = self.codes.get(code_id)
        if code is None:
            return "expired", None
        return ("paired" if code.device else "waiting"), code

    def cancel(self, code_id: str) -> None:
        self.codes.pop(code_id, None)

    def blocked(self, client: str) -> bool:
        now = time.monotonic()
        recent = [t for t in self.failures.get(client, []) if now - t < FAILED_WINDOW]
        self.failures[client] = recent
        return len(recent) >= FAILED_LIMIT

    def claim(self, secret: str, client: str) -> PairCode | None:
        """Consume a secret once; None (and a recorded failure) when it is wrong or old."""
        self._prune()
        for code in self.codes.values():
            if code.device is None and code.expires > time.monotonic() \
                    and hmac.compare_digest(code.secret, secret):
                return code
        self.failures.setdefault(client, []).append(time.monotonic())
        return None


class LanListener:
    """A second uvicorn server for the same app, bound to the LAN address only.

    It shares the main server's event loop and app state; the app's lifespan
    is not run again.
    """

    def __init__(self, app, host: str, port: int):
        self.app, self.host, self.port = app, host, port
        self.server = None
        self.task: asyncio.Task | None = None

    async def start(self) -> None:
        import uvicorn
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        if sys.platform != "win32":  # on Windows SO_REUSEADDR would allow port stealing
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind((self.host, self.port))
        except OSError:
            sock.close()
            raise
        config = uvicorn.Config(self.app, lifespan="off", proxy_headers=False, log_level="warning",
                                timeout_graceful_shutdown=2)
        config.load()
        server = uvicorn.Server(config)
        server.lifespan = config.lifespan_class(config)
        await server.startup(sockets=[sock])
        self.server = server

        async def run() -> None:
            try:
                await server.main_loop()
            finally:
                await server.shutdown(sockets=[sock])
                sock.close()

        self.task = asyncio.create_task(run())

    async def stop(self) -> None:
        if self.server is not None:
            self.server.should_exit = True
            self.server.force_exit = True
        if self.task is not None:
            try:
                await asyncio.wait_for(self.task, 5)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                self.task.cancel()
        self.server = self.task = None

    @property
    def origin(self) -> str:
        return f"http://{self.host}:{self.port}"


def qr_svg_data_uri(text: str) -> str:
    """The QR code as an SVG data URI for the web page (img-src allows data:)."""
    import segno
    return segno.make(text, error="m").svg_data_uri(scale=6, border=2, dark="#000", light="#fff")


def qr_terminal(text: str) -> str:
    """The QR code as compact Unicode blocks for a terminal."""
    import io
    import segno
    buffer = io.StringIO()
    segno.make(text, error="m").terminal(out=buffer, compact=True, border=2)
    return buffer.getvalue()


# ---------------------------------------------------------------- LAN guard
UNAUTHENTICATED_LAN_PATHS = ("/app", "/app/pair")


def lan_app(app, host: str, port: int):
    """Wrap the app for the LAN listener: mark requests, refuse anything off-policy early.

    * the peer must be on a private network (a port-forward from the internet is refused);
    * ``Host`` must be exactly the LAN address, which also defeats DNS rebinding;
    * except for the page, its assets and the pairing claim, a paired device's
      cookie is required before any route runs.
    """
    from starlette.requests import Request
    from starlette.responses import PlainTextResponse
    expected_host = f"{host}:{port}"

    async def guarded(scope, receive, send):
        if scope["type"] not in ("http", "websocket"):
            return await app(scope, receive, send)
        scope = {**scope, "kova_lan": True}
        request = Request(scope)
        client = scope.get("client") or (None, None)
        refusal = None
        if not is_private_client(client[0]):
            refusal = "Kova only answers devices on your local network."
        elif request.headers.get("host") != expected_host:
            refusal = "Open Kova with the address from its pairing code."
        if refusal:
            return await PlainTextResponse(refusal, status_code=403)(scope, receive, send)
        path = scope.get("path", "")
        public = path in UNAUTHENTICATED_LAN_PATHS or path.startswith("/app/assets/")
        if not public and app.state.devices.find(request.cookies.get(DEVICE_COOKIE)) is None:
            return await PlainTextResponse(NOT_PAIRED, status_code=401)(scope, receive, send)
        await app(scope, receive, send)

    return guarded


NOT_PAIRED = "This phone is not paired with Kova. On your computer, run kova pair and scan the code."


def is_lan_request(request: Request) -> bool:
    return bool(request.scope.get("kova_lan"))


def require_local(request: Request) -> None:
    """Routes that manage access itself are for the computer Kova runs on, never a phone."""
    from fastapi import HTTPException
    if is_lan_request(request):
        raise HTTPException(403, "Do this on the computer Kova runs on.")


# ------------------------------------------------------------ lifecycle
async def start_lan(app) -> None:
    """Start the LAN listener if phone access is on; record why when it cannot."""
    app.state.lan_error = None
    if app.state.lan is not None:
        return
    host = lan_address()
    if host is None:
        app.state.lan_error = "No Wi-Fi or wired network found. Connect this computer to your network, then try again."
        return
    listener = LanListener(lan_app(app, host, app.state.config.port), host, app.state.config.port)
    try:
        await listener.start()
    except OSError as exc:
        app.state.lan_error = f"Could not listen on {host}:{app.state.config.port}: {exc.strerror or exc}"
        return
    app.state.lan = listener


async def stop_lan(app) -> None:
    listener, app.state.lan = app.state.lan, None
    if listener is not None:
        await listener.stop()


def phone_status(app) -> dict:
    lan = app.state.lan
    return {"enabled": app.state.config.bind == "lan", "listening": lan is not None,
            "address": lan.origin if lan else None, "error": app.state.lan_error,
            "devices": app.state.devices.list()}


# ---------------------------------------------------------------- routes
class PhoneBody(BaseModel):
    enabled: bool


class ClaimBody(BaseModel):
    secret: str = Field(min_length=1, max_length=128)


def install_phone(app, require_token) -> None:
    """Pairing, device and phone-access routes (local only) and the phone's claim route."""
    from fastapi import APIRouter, Depends, HTTPException
    from fastapi.responses import JSONResponse
    from kova.config import load_config, save_config
    from kova.web import require_same_origin

    app.state.devices = Devices(app.state.config.home / "devices.json")
    app.state.pairing = Pairing()
    app.state.lan = None
    app.state.lan_error = None

    local = APIRouter(dependencies=[Depends(require_local), Depends(require_token)])

    @local.get("/phone")
    async def phone() -> dict:
        return phone_status(app)

    @local.post("/phone")
    async def set_phone(body: PhoneBody) -> dict:
        current = load_config(app.state.config.home)
        current.bind = app.state.config.bind = "lan" if body.enabled else "loopback"
        save_config(current)
        if body.enabled:
            await start_lan(app)
        else:
            await stop_lan(app)
            app.state.lan_error = None
        return phone_status(app)

    @local.post("/phone/pair")
    async def start_pairing() -> dict:
        if app.state.config.bind != "lan":
            raise HTTPException(409, "Phone access is off. Turn it on first.")
        if app.state.lan is None:
            await start_lan(app)
        if app.state.lan is None:
            raise HTTPException(409, app.state.lan_error or "Phone access is not listening.")
        code = app.state.pairing.start()
        url = f"{app.state.lan.origin}/app#pair={code.secret}"
        return {"id": code.id, "url": url, "address": app.state.lan.origin,
                "expires_in": PAIR_SECONDS, "qr": qr_svg_data_uri(url)}

    @local.get("/phone/pair/{code_id}")
    async def pairing_status(code_id: str) -> dict:
        status, code = app.state.pairing.status(code_id)
        return {"status": status, "device": code.device if code and code.device else None}

    @local.delete("/phone/pair/{code_id}")
    async def cancel_pairing(code_id: str) -> dict:
        app.state.pairing.cancel(code_id)
        return {"ok": True}

    @local.get("/devices")
    async def devices() -> list[dict]:
        return app.state.devices.list()

    @local.delete("/devices/{device_id}")
    async def revoke(device_id: str) -> dict:
        if not app.state.devices.revoke(device_id):
            raise HTTPException(404, "No paired device with that id.")
        return {"ok": True, "removed": 1}

    @local.delete("/devices")
    async def revoke_all() -> dict:
        return {"ok": True, "removed": app.state.devices.revoke()}

    app.include_router(local)

    @app.post("/app/pair", include_in_schema=False)
    async def claim(request: Request, body: ClaimBody):
        if not is_lan_request(request):
            raise HTTPException(403, "Open the pairing link on your phone, over your Wi-Fi.")
        require_same_origin(request)
        client = (request.client.host if request.client else "") or "unknown"
        if app.state.pairing.blocked(client):
            raise HTTPException(429, "Too many wrong pairing codes. Wait a few minutes, then scan a new code.")
        code = app.state.pairing.claim(body.secret, client)
        if code is None:
            raise HTTPException(401, "This pairing code expired or was already used. "
                                     "Make a new one on your computer and scan it again.")
        device, token = app.state.devices.add(device_name(request.headers.get("user-agent", "")))
        code.device = device
        response = JSONResponse({"ok": True, "device": device})
        # Plain HTTP on the LAN, so Secure cannot be set; see docs/remote-access.md.
        response.set_cookie(DEVICE_COOKIE, token, httponly=True, samesite="strict",
                            max_age=DEVICE_SECONDS, path="/")
        return response
