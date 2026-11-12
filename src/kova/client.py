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


def _auth(token: str) -> dict[str, str]:
    """Bearer header every route except ``/health`` requires."""
    return {"Authorization": f"Bearer {token}"}


def _iter_events(response: httpx.Response) -> Iterator[dict]:
    """Yield the parsed event dict from each ``data:`` frame of an SSE stream.

    Works with a real httpx response and with FastAPI's TestClient stream,
    both of which are usable as context managers.
    """
    with response as stream:
        buffer = ""
        for chunk in stream.iter_text():
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

    def health(self) -> dict:
        return self.http.get("/health", headers=_auth(self.token)).json()

    def create_chat(self) -> dict:
        return self.http.post("/chats", headers=_auth(self.token)).json()

    def list_chats(self) -> list[dict]:
        return self.http.get("/chats", headers=_auth(self.token)).json()

    def get_chat(self, chat_id: str) -> dict | None:
        return self.http.get(f"/chats/{chat_id}", headers=_auth(self.token)).json()

    def delete_chat(self, chat_id: str) -> bool:
        return self.http.delete(f"/chats/{chat_id}", headers=_auth(self.token)).status_code == 200

    def stream_message(self, chat_id: str, text: str) -> Iterator[dict]:
        """Stream one turn's events from the server."""
        response = self.http.stream(
            "POST",
            f"/chats/{chat_id}/messages",
            json={"text": text},
            headers=_auth(self.token),
            timeout=None,
        )
        yield from _iter_events(response)

    def events(self) -> Iterator[dict]:
        """Stream background events (currently reminders) from the server."""
        response = self.http.stream("GET", "/events", headers=_auth(self.token), timeout=None)
        yield from _iter_events(response)

    def memory_list(self, q: str | None = None) -> list[dict]:
        url = "/memory" if q is None else f"/memory?q={q}"
        return self.http.get(url, headers=_auth(self.token)).json()

    def memory_add(self, title: str, body: str, tags: list[str] | None = None) -> dict:
        payload = {"title": title, "body": body, "tags": tags or []}
        return self.http.post("/memory", json=payload, headers=_auth(self.token)).json()

    def memory_delete(self, slug: str) -> bool:
        return self.http.delete(f"/memory/{slug}", headers=_auth(self.token)).status_code == 200

    def memory_use(self, path: str, obsidian: bool) -> None:
        response = self.http.post(
            "/memory/location", json={"path": path, "obsidian": obsidian},
            headers=_auth(self.token), timeout=5,
        )
        response.raise_for_status()

    def reminders_list(self) -> list[dict]:
        return self.http.get("/reminders", headers=_auth(self.token)).json()

    def reminder_add(self, when: str, text: str) -> dict:
        return self.http.post("/reminders", json={"when": when, "text": text}, headers=_auth(self.token)).json()

    def reminder_delete(self, rid: int) -> bool:
        return self.http.delete(f"/reminders/{rid}", headers=_auth(self.token)).status_code == 200

    def models_list(self) -> list[dict]:
        return self.http.get("/models", headers=_auth(self.token)).json()

    def model_add(self, name: str, base_url: str, model: str, api_key: str | None = None) -> dict:
        payload = {"name": name, "base_url": base_url, "model": model, "api_key": api_key}
        return self.http.post("/models", json=payload, headers=_auth(self.token)).json()

    def model_use(self, name: str) -> dict:
        return self.http.post("/models/active", json={"name": name}, headers=_auth(self.token)).json()


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
    except httpx.RequestError:
        pass

    _spawn_server(cfg)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            client.health()
            return client
        except httpx.RequestError:
            time.sleep(0.1)
    raise RuntimeError(f"Kova server didn't start, see {log}")
