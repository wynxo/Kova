"""Compare one prompt across several models (issue #96).

The stub transport answers per model id, so the tests need no model server
and no network.
"""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from kova.compare import MAX_COMPARE_MODELS, compare_prompts, run_single
from kova.config import ModelEntry, ensure_token, load_config
from kova.server import create_app


def _sse_response(text: str) -> httpx.Response:
    chunks = [text[i:i + 3] for i in range(0, len(text), 3)] or [""]
    lines = "".join(
        f"data: {json.dumps({'choices': [{'delta': {'content': chunk}}]})}\n\n" for chunk in chunks
    )
    return httpx.Response(200, text=lines + "data: [DONE]\n\n",
                          headers={"content-type": "text/event-stream"})


def _transport_for(mapping: dict) -> httpx.MockTransport:
    """Answer per requested model id; an Exception value fails that model."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": []})
        body = json.loads(request.content)
        item = mapping.get(body.get("model"), "")
        if isinstance(item, Exception):
            raise item
        return _sse_response(item)

    return httpx.MockTransport(handler)


def _entry(name: str, model: str) -> ModelEntry:
    return ModelEntry(name=name, base_url="http://model.test/v1", model=model)


def test_run_single_collects_text():
    entry = _entry("fast", "fast-id")
    result = asyncio.run(run_single(entry, "hi", transport=_transport_for({"fast-id": "hello"})))
    assert result == {"model": "fast", "text": "hello", "ms": result["ms"], "error": None}
    assert isinstance(result["ms"], int) and result["ms"] >= 0


def test_compare_preserves_entry_order():
    entries = [_entry("a", "id-a"), _entry("b", "id-b")]
    results = asyncio.run(compare_prompts(
        entries, "hi", transport=_transport_for({"id-a": "answer-a", "id-b": "answer-b"})))
    assert [(r["model"], r["text"], r["error"]) for r in results] == [
        ("a", "answer-a", None), ("b", "answer-b", None)]


def test_compare_one_failure_does_not_break_the_other():
    entries = [_entry("good", "id-good"), _entry("bad", "id-bad")]
    transport = _transport_for({"id-good": "still here",
                                "id-bad": httpx.ConnectError("down")})
    results = asyncio.run(compare_prompts(entries, "hi", transport=transport))
    by_name = {r["model"]: r for r in results}
    assert by_name["good"]["text"] == "still here" and by_name["good"]["error"] is None
    assert by_name["bad"]["error"] and by_name["bad"]["text"] == ""


@pytest.fixture
def compare_server(tmp_path, monkeypatch):
    """App with two configured models and per-model stub answers."""
    monkeypatch.setenv("KOVA_HOME", str(tmp_path))
    cfg = load_config()
    cfg.models.append(ModelEntry(name="second", base_url="http://model.test/v1",
                                 model="second-id", context=32768))
    save = cfg.models[0]
    save.model = "first-id"
    transport = _transport_for({"first-id": "first answer", "second-id": "second answer"})
    app = create_app(cfg, transport=transport)
    headers = {"Authorization": f"Bearer {ensure_token(cfg.home)}"}
    with TestClient(app) as client:
        yield client, headers, cfg


def test_server_compare_two_models(compare_server):
    client, headers, _cfg = compare_server
    response = client.post("/compare", json={"text": "which is better?", "models": ["local", "second"]},
                           headers=headers)
    assert response.status_code == 200
    results = response.json()["results"]
    assert [(r["model"], r["text"], r["error"]) for r in results] == [
        ("local", "first answer", None), ("second", "second answer", None)]
    assert all(isinstance(r["ms"], int) for r in results)
    assert [r["model"] for r in results] == ["local", "second"]


def test_server_compare_defaults_to_every_model(compare_server):
    client, headers, _cfg = compare_server
    response = client.post("/compare", json={"text": "hi"}, headers=headers)
    assert response.status_code == 200
    assert [r["model"] for r in response.json()["results"]] == ["local", "second"]


def test_server_compare_unknown_model_is_404(compare_server):
    client, headers, _cfg = compare_server
    response = client.post("/compare", json={"text": "hi", "models": ["ghost"]}, headers=headers)
    assert response.status_code == 404


def test_server_compare_rejects_empty_and_too_many(compare_server):
    client, headers, _cfg = compare_server
    assert client.post("/compare", json={"text": "hi", "models": []},
                       headers=headers).status_code == 422
    too_many = [f"m{i}" for i in range(MAX_COMPARE_MODELS + 1)]
    response = client.post("/compare", json={"text": "hi", "models": too_many}, headers=headers)
    assert response.status_code == 422


def test_server_compare_requires_auth(compare_server):
    client, _headers, _cfg = compare_server
    assert client.post("/compare", json={"text": "hi"}).status_code == 401


def test_server_compare_one_model_down_still_answers(tmp_path, monkeypatch):
    """A dead model is one failed result, not a failed request."""
    monkeypatch.setenv("KOVA_HOME", str(tmp_path))
    cfg = load_config()
    cfg.models.append(ModelEntry(name="down", base_url="http://model.test/v1",
                                 model="down-id", context=32768))
    cfg.models[0].model = "up-id"
    transport = _transport_for({"up-id": "here", "down-id": httpx.ConnectError("down")})
    app = create_app(cfg, transport=transport)
    headers = {"Authorization": f"Bearer {ensure_token(cfg.home)}"}
    with TestClient(app) as client:
        response = client.post("/compare", json={"text": "hi"}, headers=headers)
    assert response.status_code == 200
    by_name = {r["model"]: r for r in response.json()["results"]}
    assert by_name["local"]["error"] is None and by_name["local"]["text"] == "here"
    assert by_name["down"]["error"] and by_name["down"]["text"] == ""
