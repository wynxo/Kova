"""End-to-end smoke test against a real local model server.

Runs the FastAPI server in-process (via ``create_app`` + ``TestClient``) over a
throwaway ``KOVA_HOME`` pointed at the server in KOVA_LIVE_URL. Skipped
unless ``KOVA_LIVE=1``. Does not need a GPU; it only needs the model reachable.
"""

from __future__ import annotations

import json
import os

import httpx
import pytest
from fastapi.testclient import TestClient

from kova.config import ModelEntry, ensure_token, load_config
from kova.server import create_app

# The real model can take a while to think, so give the in-process server and
# every request a generous ceiling.
TIMEOUT = 900


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _iter_frames(resp: "httpx.Response") -> "list[tuple[str, str]]":
    """Yield ``(event, data)`` for every SSE frame of a ``text/event-stream`` body."""
    frames: list[tuple[str, str]] = []
    for block in resp.text.split("\n\n"):
        if not block.strip():
            continue
        event = ""
        data_lines: list[str] = []
        for line in block.splitlines():
            if line.startswith("event:"):
                event = line.split(":", 1)[1].strip()
            elif line.startswith("data:"):
                data_lines.append(line.split(":", 1)[1].strip())
        frames.append((event, "\n".join(data_lines)))
    return frames


def _run_message(client: TestClient, token: str, chat_id: str, text: str) -> str:
    """Send one prompt and return the joined ``text`` events once the turn ends."""
    resp = client.post(
        f"/chats/{chat_id}/messages",
        headers=_auth(token),
        json={"text": text},
        timeout=TIMEOUT,
    )
    resp.raise_for_status()

    joined = ""
    finished = False
    error = None
    for event, data in _iter_frames(resp):
        if event == "text" and data:
            joined += json.loads(data).get("text", "")
        elif event == "done":
            finished = True
        elif event == "error":
            error = json.loads(data).get("message", "unknown error") if data else "unknown error"

    if not finished:
        raise AssertionError(f"turn did not finish (error={error!r})")
    return joined


def _new_chat(client: TestClient, token: str) -> str:
    return client.post("/chats", headers=_auth(token)).json()["id"]


@pytest.fixture
def live(tmp_path):
    """A live server over a fresh KOVA_HOME wired to a real model server.

    Point it at yours with KOVA_LIVE_URL (e.g. http://127.0.0.1:11434/v1) and KOVA_LIVE_MODEL.
    """
    cfg = load_config(tmp_path)
    cfg.models = [ModelEntry("live", os.environ["KOVA_LIVE_URL"], os.environ["KOVA_LIVE_MODEL"])]
    cfg.active = "live"
    token = ensure_token(tmp_path)
    with TestClient(create_app(cfg)) as client:
        yield client, token


@pytest.mark.live
@pytest.mark.skipif(
    os.environ.get("KOVA_LIVE") != "1",
    reason="set KOVA_LIVE=1 to run against a real model (also set KOVA_LIVE_URL and KOVA_LIVE_MODEL)",
)
def test_live_identity_contains_kova(live):
    client, token = live
    reply = _run_message(client, token, _new_chat(client, token), "What's your name? Answer one word.")
    assert "Kova" in reply


@pytest.mark.live
@pytest.mark.skipif(
    os.environ.get("KOVA_LIVE") != "1",
    reason="set KOVA_LIVE=1 to run against a real model (also set KOVA_LIVE_URL and KOVA_LIVE_MODEL)",
)
def test_live_memory_recall_across_chats(live):
    client, token = live

    remember = _new_chat(client, token)
    _run_message(client, token, remember, "Remember that my dog is called Rex.")

    # Recall in a brand-new chat so only injected memory (not chat history) can answer.
    recall = _new_chat(client, token)
    reply = _run_message(client, token, recall, "What's my dog called? Answer one word.")
    assert "Rex" in reply
