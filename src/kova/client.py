"""HTTP client for the Kova server and a helper to start it when it is down.

``KovaClient`` is a thin sync client the CLI drives. ``ensure_server`` starts
``kova serve`` in the background the first time the CLI needs one.
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from collections.abc import Iterator

import httpx

from kova.config import Config, ensure_token


class KovaError(RuntimeError):
    """A server error, or a connection failure when ``status`` is None."""

    def __init__(self, status: int | None, message: str):
        self.status = status
        self.message = message
        super().__init__(message)


def _check_response(response: httpx.Response) -> None:
    if response.is_error:
        response.read()
        try:
            body = response.json()
            detail = body.get("detail", response.reason_phrase) if isinstance(body, dict) else response.reason_phrase
        except ValueError:
            detail = response.reason_phrase
        message = detail if isinstance(detail, str) else json.dumps(detail)
        raise KovaError(response.status_code, message)


def _auth(token: str) -> dict[str, str]:
    """Bearer header every route except ``/health`` requires."""
    return {"Authorization": f"Bearer {token}"}


def _iter_events(response: httpx.Response) -> Iterator[dict]:
    """Yield the parsed event dict from each ``data:`` frame of an SSE stream."""
    buffer = ""
    for chunk in response.iter_text():
        buffer += chunk
        while "\n\n" in buffer:
            frame, buffer = buffer.split("\n\n", 1)
            data = ""
            for line in frame.splitlines():
                if line.startswith("data:"):
                    data = line[len("data:"):]
                    break
            if data.strip():
                yield json.loads(data)


class KovaClient:
    """A small sync HTTP client for the Kova server."""

    def __init__(self, base_url: str, token: str, http: httpx.Client | None = None):
        self.base_url = base_url
        self.token = token
        self.http = http if http is not None else httpx.Client(base_url=base_url, timeout=None)

    def _request(self, method: str, path: str, *, missing_ok: bool = False, **kwargs) -> httpx.Response:
        try:
            response = self.http.request(method, path, headers=_auth(self.token), **kwargs)
        except httpx.RequestError as exc:
            raise KovaError(None, f"Cannot reach Kova server at {self.base_url}.") from exc
        if not (missing_ok and response.status_code == 404):
            _check_response(response)
        return response

    def _stream(self, method: str, path: str, **kwargs) -> Iterator[dict]:
        try:
            with self.http.stream(method, path, headers=_auth(self.token), timeout=None, **kwargs) as response:
                _check_response(response)
                yield from _iter_events(response)
        except httpx.RequestError as exc:
            raise KovaError(None, f"Cannot reach Kova server at {self.base_url}.") from exc

    def health(self) -> dict:
        return self._request("GET", "/health").json()

    def create_chat(self) -> dict:
        return self._request("POST", "/chats").json()

    def list_chats(self) -> list[dict]:
        return self._request("GET", "/chats").json()

    def get_chat(self, chat_id: str) -> dict | None:
        return self._request("GET", f"/chats/{chat_id}").json()

    def delete_chat(self, chat_id: str) -> bool:
        return self._request("DELETE", f"/chats/{chat_id}", missing_ok=True).status_code == 200

    def stream_message(self, chat_id: str, text: str) -> Iterator[dict]:
        """Stream one turn's events from the server."""
        yield from self._stream("POST", f"/chats/{chat_id}/messages", json={"text": text})

    def events(self) -> Iterator[dict]:
        """Stream background events (currently reminders) from the server."""
        yield from self._stream("GET", "/events")

    def memory_list(self, q: str | None = None) -> list[dict]:
        url = "/memory" if q is None else f"/memory?q={q}"
        return self._request("GET", url).json()

    def memory_add(self, title: str, body: str, tags: list[str] | None = None) -> dict:
        payload = {"title": title, "body": body, "tags": tags or []}
        return self._request("POST", "/memory", json=payload).json()

    def memory_delete(self, slug: str) -> bool:
        return self._request("DELETE", f"/memory/{slug}", missing_ok=True).status_code == 200

    def memory_use(self, path: str, obsidian: bool) -> None:
        response = self.http.post(
            "/memory/location", json={"path": path, "obsidian": obsidian},
            headers=_auth(self.token), timeout=5,
        )
        response.raise_for_status()

    def reminders_list(self) -> list[dict]:
        return self._request("GET", "/reminders").json()

    def reminder_add(self, when: str, text: str) -> dict:
        return self._request("POST", "/reminders", json={"when": when, "text": text}).json()

    def reminder_delete(self, rid: int) -> bool:
        return self._request("DELETE", f"/reminders/{rid}", missing_ok=True).status_code == 200

    def models_list(self) -> list[dict]:
        return self._request("GET", "/models").json()

    def model_add(self, name: str, base_url: str, model: str, api_key: str | None = None) -> dict:
        payload = {"name": name, "base_url": base_url, "model": model, "api_key": api_key}
        return self._request("POST", "/models", json=payload).json()

    def model_use(self, name: str) -> dict:
        return self._request("POST", "/models/active", json={"name": name}).json()


def _spawn_server(cfg: Config) -> int:
    """Start ``kova serve`` detached and record its pid; return the pid."""
    log = cfg.home / "server.log"
    with open(log, "w") as handle:
        process = subprocess.Popen(
            [sys.executable, "-m", "kova", "serve"],
            stdout=handle,
            stderr=handle,
            start_new_session=True,
        )
    cfg.home.mkdir(parents=True, exist_ok=True)
    (cfg.home / "server.pid").write_text(str(process.pid))
    return process.pid


def ensure_server(cfg: Config, timeout: float = 10) -> KovaClient:
    """Return a client for ``cfg``'s server, starting it if it is not up.

    Waits up to ``timeout`` seconds for ``/health``; otherwise raises.
    """
    base = f"http://127.0.0.1:{cfg.port}"
    log = cfg.home / "server.log"
    client = KovaClient(base, ensure_token(cfg.home))
    try:
        client.health()
        return client
    except KovaError as exc:
        if exc.status is not None:
            raise

    _spawn_server(cfg)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            client.health()
            return client
        except KovaError as exc:
            if exc.status is not None:
                raise
            time.sleep(0.1)
    raise KovaError(None, f"Cannot reach Kova server at {base}; server didn't start, see {log}")
