"""HTTP client for the Kova server and a helper to start it when it is down.

``KovaClient`` is a thin sync client the CLI drives. ``ensure_server`` starts
``kova serve`` in the background the first time the CLI needs one.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from collections.abc import Iterator

import httpx

from kova.config import Config, ensure_token

ASK_TITLE_PREFIX = "[ask] "


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
    """Bearer header for CLI API requests."""
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
                try:
                    event = json.loads(data)
                    if not isinstance(event, dict):
                        raise TypeError()
                except (ValueError, TypeError):
                    raise KovaError(None, 'Kova returned an invalid stream. Run kova doctor, then retry.') from None
                yield event


class KovaClient:
    """A small sync HTTP client for the Kova server."""

    def __init__(self, base_url: str, token: str, http: httpx.Client | None = None):
        self.base_url = base_url
        self.token = token
        self.http = http if http is not None else httpx.Client(base_url=base_url, timeout=30, trust_env=False)

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
            with self.http.stream(method, path, headers=_auth(self.token), timeout=kwargs.pop("timeout", None), **kwargs) as response:
                _check_response(response)
                yield from _iter_events(response)
        except httpx.RequestError as exc:
            raise KovaError(None, f"Cannot reach Kova server at {self.base_url}.") from exc

    def app_ticket(self) -> str:
        """Mint a one-use browser login ticket without exposing the server token."""
        return self._request("POST", "/app/session").json()["ticket"]

    def health(self) -> dict:
        try:
            data = self._request("GET", "/health", timeout=4).json()
            if not isinstance(data, dict) or 'version' not in data or 'model_ok' not in data:
                raise ValueError()
            return data
        except ValueError:
            raise KovaError(502, 'This port is not serving Kova. Choose another port in config.toml, then retry.') from None

    def create_chat(self, title: str | None = None, mode: str | None = None,
                    web: bool | None = None, permission: str | None = None,
                    cwd: str | None = None) -> dict:
        body = {"title": title, "mode": mode, "web": web, "permission": permission, "cwd": cwd}
        body = {field: value for field, value in body.items() if value is not None}
        return self._request("POST", "/chats", **({"json": body} if body else {})).json()

    def list_chats(self, include_ask: bool = False) -> list[dict]:
        """Chats, most recently used first. One-off ``kova ask`` chats are hidden unless asked for."""
        chats = self._request("GET", "/chats").json()
        if include_ask:
            return chats
        return [c for c in chats if not (c.get("title") or "").startswith(ASK_TITLE_PREFIX)]

    def search_chats(self, query: str, limit: int = 20) -> list[dict]:
        """Chats with a matching message, newest first, each with a snippet."""
        params = {"q": query, "limit": limit}
        return self._request("GET", "/chats/search", params=params).json()

    def get_chat(self, chat_id: str) -> dict | None:
        return self._request("GET", f"/chats/{chat_id}").json()

    def fork_chat(self, chat_id: str) -> dict:
        """A full copy of a conversation — messages, plan and compacted context —
        that continues independently from the same point."""
        return self._request("POST", f"/chats/{chat_id}/fork").json()

    def update_chat(self, chat_id: str, title: str | None = None, mode: str | None = None,
                    web: bool | None = None, permission: str | None = None,
                    cwd: str | None = None) -> dict:
        """Change a chat's title, mode, web toggle, permission or working folder;
        ``None`` leaves a field alone."""
        body = {"title": title, "mode": mode, "web": web, "permission": permission, "cwd": cwd}
        body = {field: value for field, value in body.items() if value is not None}
        return self._request("PATCH", f"/chats/{chat_id}", json=body).json()

    def rename_chat(self, chat_id: str, title: str) -> dict:
        return self.update_chat(chat_id, title=title)

    def delete_chat(self, chat_id: str) -> bool:
        return self._request("DELETE", f"/chats/{chat_id}", missing_ok=True).status_code == 200

    def stream_message(self, chat_id: str, text: str, model: str | None = None) -> Iterator[dict]:
        """Stream one turn's events; ``model`` overrides the active model for this turn only."""
        payload = {"text": text} if model is None else {"text": text, "model": model}
        finished = False
        for event in self._stream("POST", f"/chats/{chat_id}/messages", json=payload, timeout=330):
            finished = finished or event.get('type') in ('done', 'error', 'stopped')
            yield event
        if not finished:
            raise KovaError(None, 'Chat stream ended before completion. Run kova doctor, then retry.')

    def compact_chat(self, chat_id: str) -> dict:
        return self._request("POST", f"/chats/{chat_id}/compact", timeout=330).json()

    def stop_chat(self, chat_id: str) -> bool:
        """Cancel the chat's running turn on the server; True if one was running."""
        return bool(self._request("POST", f"/chats/{chat_id}/stop", timeout=10).json().get("stopped"))

    def queue_message(self, chat_id: str, text: str, model: str | None = None) -> dict:
        """Persist a message to run after the chat's current turn ends."""
        body = {"text": text}
        if model is not None:
            body["model"] = model
        return self._request("POST", f"/chats/{chat_id}/queue", json=body).json()

    def queued_messages(self, chat_id: str) -> list[dict]:
        """The chat's queued messages, oldest first; they survive reloads."""
        return self._request("GET", f"/chats/{chat_id}/queue").json()

    def delete_queued_message(self, chat_id: str, message_id: int) -> bool:
        return self._request("DELETE", f"/chats/{chat_id}/queue/{message_id}",
                             missing_ok=True).status_code == 200

    def next_queued_message(self, chat_id: str) -> dict | None:
        """Take the oldest queued message (or None) so it can run next."""
        row = self._request("POST", f"/chats/{chat_id}/queue/next").json()
        return row if row.get("text") else None

    def steer_message(self, chat_id: str, text: str) -> dict:
        """Send a message into the turn that is running now."""
        return self._request("POST", f"/chats/{chat_id}/steer", json={"text": text},
                             timeout=10).json()

    def list_checkpoints(self, chat_id: str) -> list[dict]:
        """This chat's change sets, newest first: what each turn changed."""
        return self._request("GET", f"/chats/{chat_id}/checkpoints").json()

    def checkpoint_diff(self, chat_id: str, checkpoint_id: int, path: str) -> dict:
        """One changed file's diff and both sides, to preview before restoring."""
        return self._request("GET", f"/chats/{chat_id}/checkpoints/{checkpoint_id}/diff",
                             params={"path": path}).json()

    def restore_checkpoint(self, chat_id: str, checkpoint_id: int) -> dict:
        """Put the working folder back to a checkpoint; never touches git."""
        return self._request("POST", f"/chats/{chat_id}/checkpoints/{checkpoint_id}/restore",
                             timeout=120).json()

    def permission_rules(self) -> list[dict]:
        return self._request("GET", "/permission-rules").json()

    def replace_permission_rules(self, rules: list[dict]) -> dict:
        return self._request("PUT", "/permission-rules", json=rules).json()

    def answer_approval(self, chat_id: str, approval_id: str, decision: str,
                        message: str | None = None) -> dict:
        """Answer an ``approval`` event: ``allow``, ``allow_session`` or ``deny``."""
        body = {"decision": decision}
        if message:
            body["message"] = message
        return self._request("POST", f"/chats/{chat_id}/approvals/{approval_id}",
                             json=body, timeout=10).json()

    def events(self) -> Iterator[dict]:
        """Stream background events (currently reminders) from the server."""
        yield from self._stream("GET", "/events")

    def memory_list(self, q: str | None = None) -> list[dict]:
        url = "/memory" if q is None else f"/memory?q={q}"
        return self._request("GET", url).json()

    def memory_add(self, title: str, body: str, tags: list[str] | None = None) -> dict:
        payload = {"title": title, "body": body, "tags": tags or []}
        return self._request("POST", "/memory", json=payload).json()

    def memory_edit(self, slug: str, *, title: str | None = None, body: str | None = None,
                    tags: list[str] | None = None) -> dict:
        """Rewrite one note in place, keeping its slug and provenance."""
        payload = {"title": title, "body": body, "tags": tags}
        payload = {key: value for key, value in payload.items() if value is not None}
        return self._request("PATCH", f"/memory/{slug}", json=payload).json()

    def memory_delete(self, slug: str) -> bool:
        return self._request("DELETE", f"/memory/{slug}", missing_ok=True).status_code == 200

    def memory_forget(self, slug: str) -> bool:
        """Delete one note; the user-facing name for :meth:`memory_delete`."""
        return self.memory_delete(slug)

    def memory_diary(self, limit: int = 50) -> list[dict]:
        return self._request("GET", "/memory/diary", params={"limit": limit}).json()

    def memory_revisions(self, limit: int = 50) -> list[dict]:
        """Recent background memory changes, newest first, with their undo state."""
        return self._request("GET", "/memory/revisions", params={"limit": limit}).json()

    def memory_settings(self, **kwargs) -> dict:
        """Read the background passes' switches, or PATCH the ones given."""
        if kwargs:
            payload = {key: value for key, value in kwargs.items() if value is not None}
            if payload:
                return self._request("PATCH", "/memory/settings", json=payload).json()
        return self._request("GET", "/memory/settings").json()

    def memory_undo(self, revision_id: int | None = None) -> list[str]:
        """Undo background memory writes; returns the slugs put back."""
        payload = {} if revision_id is None else {"revision_id": revision_id}
        return self._request("POST", "/memory/undo", json=payload, timeout=60).json()["undone"]

    def memory_consolidate(self, dry_run: bool = False) -> dict:
        """Plan one consolidation run; applies it unless ``dry_run``."""
        return self._request("POST", "/memory/consolidate", json={"dry_run": dry_run},
                             timeout=330).json()

    def memory_use(self, path: str, obsidian: bool) -> None:
        self._request("POST", "/memory/location", json={"path": path, "obsidian": obsidian}, timeout=5)

    def reminders_list(self) -> list[dict]:
        return self._request("GET", "/reminders").json()

    def reminder_add(self, when: str, text: str) -> dict:
        return self._request("POST", "/reminders", json={"when": when, "text": text}).json()

    def reminder_delete(self, rid: int) -> bool:
        return self._request("DELETE", f"/reminders/{rid}", missing_ok=True).status_code == 200

    def jobs_list(self) -> list[dict]:
        return self._request("GET", "/jobs").json()

    def job_add(self, text: str, schedule: str, *, command: str | None = None,
                prompt: str | None = None, first_run: str | None = None,
                idle_only: bool = False) -> dict:
        """Schedule a recurring job: a shell command, or one agent turn per run."""
        payload = {"text": text, "schedule": schedule, "command": command, "prompt": prompt,
                   "first_run": first_run, "idle_only": idle_only}
        return self._request("POST", "/jobs", json=payload).json()

    def job_delete(self, jid: int) -> bool:
        return self._request("DELETE", f"/jobs/{jid}", missing_ok=True).status_code == 200

    def models_list(self) -> list[dict]:
        return self._request("GET", "/models").json()

    def model_add(self, name: str, base_url: str, model: str, api_key: str | None = None) -> dict:
        payload = {"name": name, "base_url": base_url, "model": model, "api_key": api_key}
        return self._request("POST", "/models", json=payload).json()

    def providers_list(self) -> list[dict]:
        return self._request("GET", "/providers").json()

    def capabilities(self) -> dict:
        """The capability contract: the active provider's declarations and each mode's."""
        return self._request("GET", "/capabilities").json()

    def provider_add(self, provider, models, active=None, *, legacy_name=None):
        payload = {k: getattr(provider, k) for k in ('id', 'name', 'kind', 'base_url', 'api_key', 'headers', 'auth')}
        payload.update(models=models, active=active, legacy_name=legacy_name, consent=provider.kind == 'codex')
        return self._request('POST', '/providers', json=payload, timeout=30).json()

    def provider_remove(self, identifier):
        return self._request('DELETE', f'/providers/{identifier}').json()

    def provider_logout(self, identifier):
        return self._request('POST', f'/providers/{identifier}/logout').json()

    def provider_login(self, identifier, *, api_key=None, auth=None, consent=False):
        return self._request('POST', f'/providers/{identifier}/login',
                             json={'api_key': api_key, 'auth': auth or {}, 'consent': consent}, timeout=30).json()

    def model_use(self, name: str) -> dict:
        return self._request("POST", "/models/active", json={"name": name}).json()


def _spawn_server(cfg: Config) -> int:
    """Start ``kova serve`` detached and record its pid; return the pid."""
    log = cfg.home / "server.log"
    cfg.home.mkdir(parents=True, exist_ok=True)
    with open(log, "w") as handle:
        process = subprocess.Popen(
            [sys.executable, "-m", "kova", "serve"],
            stdout=handle,
            stderr=handle,
            start_new_session=True,
            env={**os.environ, "KOVA_HOME": str(cfg.home)},
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

    try:
        _spawn_server(cfg)
    except OSError:
        client.http.close()
        raise KovaError(None, f'Could not start Kova. Check permissions on {cfg.home}, then run kova serve.') from None
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            client.health()
            return client
        except KovaError as exc:
            if exc.status is not None:
                raise
            time.sleep(0.1)
    client.http.close()
    raise KovaError(None, f"Cannot reach Kova server at {base}; server didn't start, see {log}")
