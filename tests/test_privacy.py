"""Opt-in outbound secret protection at the shared model boundary.

Every value here is a made-up fixture. No fixture is a valid credential and no
test contacts a provider: httpx.MockTransport records the exact request.
"""

from __future__ import annotations

import copy
import json
from datetime import datetime

import httpx
import pytest
from typer.testing import CliRunner

import kova.privacy as privacy
from kova.agent import Agent
from kova.config import ModelEntry, ProviderEntry, load_config, save_config
from kova.memory import Memory
from kova.model_adapters import anthropic_request, responses_request
from kova.models import ModelClient, ModelError
from kova.privacy import is_loopback_url, normalize_mode, protect_messages, status_text
from kova.secrets import redact
from kova.store import Store
from kova.tools import Tool, ToolContext, build_tools
from tests.fake_model import FakeModel, text, tool

OPENAI = "sk-proj-" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8S9t0"
GITHUB = "ghp_" + "Ab1Cd2Ef3Gh4Ij5Kl6Mn7Op8Qr9St0Uv1Wx2Y"
ENV = "AWS_SECRET_ACCESS_KEY=fakeAwsSecretAccessKey"
PEM = ("-----BEGIN RSA PRIVATE KEY-----\n"
       "FAKEKEYMATERIAL\n"
       "-----END RSA PRIVATE KEY-----")
SECRETS = (OPENAI, GITHUB, "fakeAwsSecretAccessKey", "FAKEKEYMATERIAL")


def messages() -> list[dict]:
    return [
        {"role": "system", "content": f"You are Kova.\nOPENAI_API_KEY={OPENAI}"},
        {"role": "user", "content": f"Read this file:\n{GITHUB}"},
        {"role": "assistant", "content": None, "tool_calls": [{
            "id": "call_1", "type": "function",
            "function": {"name": "read_file", "arguments": json.dumps({"path": OPENAI})},
        }]},
        {"role": "tool", "tool_call_id": "call_1", "content": f"file says:\n{ENV}"},
    ]


def outgoing_text(messages: list[dict]) -> str:
    """Only the prose fields the guard promises to inspect."""
    parts = []
    for message in messages:
        content = message.get("content")
        if isinstance(content, str):
            parts.append(content)
        elif isinstance(content, list):
            parts.extend(part.get("text", "") for part in content if isinstance(part, dict))
    return "\n".join(parts)


def redacted_messages() -> list[dict]:
    expected = copy.deepcopy(messages())
    expected[0]["content"] = redact(expected[0]["content"]).text
    expected[1]["content"] = redact(expected[1]["content"]).text
    expected[3]["content"] = redact(expected[3]["content"]).text
    return expected


def openai_response(request: httpx.Request) -> httpx.Response:
    return httpx.Response(
        200,
        text='data: {"choices":[{"delta":{"content":"ok"}}]}\n\ndata: [DONE]\n\n',
        headers={"content-type": "text/event-stream"},
    )


def native_handler(adapter: str, seen: list[dict]):
    def handle(request: httpx.Request) -> httpx.Response:
        seen.append({"payload": json.loads(request.content), "request": request})
        events = (
            [{"type": "content_block_delta", "index": 0,
              "delta": {"type": "text_delta", "text": "ok"}},
             {"type": "message_stop"}]
            if adapter == "anthropic" else
            [{"type": "response.output_text.delta", "delta": "ok"},
             {"type": "response.completed"}]
        )
        return httpx.Response(200, text="".join(f"data: {json.dumps(e)}\n\n" for e in events))
    return handle


def native_entry(adapter: str) -> ModelEntry:
    provider = ProviderEntry("p", "Provider", "https://example.test/v1",
                             adapter=adapter, api_key="provider-auth-key")
    return ModelEntry("m", provider.base_url, "model", provider="p", _provider=provider)


@pytest.mark.parametrize(("url", "local"), [
    ("http://127.0.0.1:11434/v1", True),
    ("http://127.0.0.2:11434/v1", True),
    ("http://[::1]:11434/v1", True),
    ("http://[0:0:0:0:0:0:0:1]/v1", True),
    ("http://[::ffff:127.0.0.1]/v1", True),
    ("http://localhost:11434/v1", True),
    ("http://LOCALHOST./v1", True),
    ("http://192.0.2.10:11434/v1", False),
    ("http://127.0.0.1.example.test/v1", False),
    ("http://localhost.example.test/v1", False),
    ("http://127.0.0.1@example.test/v1", False),
    ("http://example.test@127.0.0.1/v1", True),
    ("http://[::1", False),
    ("model.test:11434/v1", False),
    ("", False),
])
def test_loopback_bypass_parses_the_hostname_not_a_substring(url, local):
    assert is_loopback_url(url) is local


def test_unknown_mode_is_fail_closed_but_absent_is_off():
    assert normalize_mode(None) == "off"
    assert normalize_mode("REDACT") == "redact"
    assert normalize_mode("warn") == "block"


def test_redaction_is_idempotent_and_a_deep_copy():
    source = messages()
    before = copy.deepcopy(source)
    first = protect_messages(source, "redact")
    second = protect_messages(first.messages, "redact")
    assert first.messages == second.messages
    assert source == before
    assert first.count == 3
    assert "openai_key" in first.kinds and "env_assignment" in first.kinds
    for secret in SECRETS:
        assert secret not in outgoing_text(first.messages)
    # Tool-call arguments stay verbatim: they are execution instructions.
    assert first.messages[2]["tool_calls"][0]["function"]["arguments"] == \
        json.dumps({"path": OPENAI})
    assert "before sending" in first.status_text()


def test_overflow_and_truncation_are_removed_not_sent_untouched(monkeypatch):
    source = [{"role": "user", "content": f"{OPENAI}\n{GITHUB}"}]
    monkeypatch.setattr(privacy, "MAX_FINDINGS", 1)
    result = protect_messages(source, "redact")
    assert result.incomplete
    assert result.count == 1
    assert "[REDACTED:overflow:" in result.messages[0]["content"]
    assert GITHUB not in result.messages[0]["content"]

    monkeypatch.setattr(privacy, "MAX_SCAN_CHARS", 12)
    truncated = protect_messages(source, "redact")
    assert truncated.incomplete
    assert "[REDACTED:unscanned:" in truncated.messages[0]["content"]

    blocked = protect_messages(source, "block")
    assert blocked.blocked
    assert "Nothing was sent" in blocked.block_message()


def test_status_text_never_contains_a_value():
    result = protect_messages(messages(), "redact")
    line = status_text(result.status())
    assert "3" in line and "openai_key" in line
    for secret in SECRETS:
        assert secret not in line


async def test_openai_payload_is_exactly_the_redacted_copy():
    seen: list[dict] = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append({"payload": json.loads(request.content),
                     "authorization": request.headers.get("authorization")})
        return openai_response(request)

    entry = ModelEntry("m", "https://example.test/v1", "model", api_key="provider-auth-key")
    source = messages()
    before = copy.deepcopy(source)
    events = [event async for event in ModelClient(
        entry, transport=httpx.MockTransport(handle), privacy="redact").stream(source)]
    assert events[-1].type == "done"
    assert [event.privacy for event in events if event.type == "privacy"] == [
        {"mode": "redact", "count": 3,
         "kinds": ["openai_key", "github_token", "env_assignment"],
         "incomplete": False}]
    assert seen[0]["payload"] == {
        "model": "model", "messages": redacted_messages(), "stream": True,
        "stream_options": {"include_usage": True}}
    assert seen[0]["authorization"] == "Bearer provider-auth-key"
    # The tool-call arguments are execution instructions, not prose.
    assert seen[0]["payload"]["messages"][2]["tool_calls"] == before[2]["tool_calls"]
    assert source == before


async def test_tool_rejection_retry_emits_one_privacy_event():
    seen: list[dict] = []

    def handle(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        seen.append(payload)
        if "tools" in payload:
            return httpx.Response(
                400, text='{"error": {"message": "tools are not supported"}}',
                headers={"content-type": "application/json"})
        return openai_response(request)

    entry = ModelEntry("m", "https://example.test/v1", "model", api_key="provider-auth-key")
    tools_schema = [{"type": "function", "function": {"name": "read_file", "parameters": {}}}]
    events = [event async for event in ModelClient(
        entry, transport=httpx.MockTransport(handle), privacy="redact").stream(messages(), tools_schema)]
    assert events[-1].type == "done"
    # The guard reports once per turn, not once per internal retry.
    assert [event.privacy for event in events if event.type == "privacy"] == [
        {"mode": "redact", "count": 3,
         "kinds": ["openai_key", "github_token", "env_assignment"],
         "incomplete": False}]
    assert len(seen) == 2
    assert seen[1]["messages"] == redacted_messages()
    assert "tools" not in seen[1]


async def test_anthropic_payload_is_exactly_the_redacted_copy():
    seen: list[dict] = []
    entry = native_entry("anthropic")
    events = [event async for event in ModelClient(
        entry, transport=httpx.MockTransport(native_handler("anthropic", seen)),
        privacy="redact").stream(messages())]
    assert events[-1].type == "done"
    _, expected = anthropic_request("model", redacted_messages(), None)
    assert seen[0]["payload"] == expected
    assert seen[0]["request"].headers["x-api-key"] == "provider-auth-key"
    assert seen[0]["payload"]["system"] == redacted_messages()[0]["content"]
    assert seen[0]["payload"]["messages"][0]["content"][0]["text"] == \
        redacted_messages()[1]["content"]
    assert seen[0]["payload"]["messages"][2]["content"][0]["content"] == \
        redacted_messages()[3]["content"]
    # The guard never rewrites a tool-use input.
    assert seen[0]["payload"]["messages"][1]["content"][0]["input"] == {"path": OPENAI}


async def test_responses_payload_is_exactly_the_redacted_copy():
    seen: list[dict] = []
    entry = native_entry("responses")
    events = [event async for event in ModelClient(
        entry, transport=httpx.MockTransport(native_handler("responses", seen)),
        privacy="redact").stream(messages())]
    assert events[-1].type == "done"
    _, expected = responses_request("model", redacted_messages(), None)
    assert seen[0]["payload"] == expected
    payload = seen[0]["payload"]
    assert payload["instructions"] == redacted_messages()[0]["content"]
    assert payload["input"][0]["content"] == redacted_messages()[1]["content"]
    assert payload["input"][2]["output"] == redacted_messages()[3]["content"]
    # Function-call arguments stay verbatim.
    assert json.loads(payload["input"][1]["arguments"]) == {"path": OPENAI}


@pytest.mark.parametrize("url", [
    "http://127.0.0.1:11434/v1",
    "http://localhost:11434/v1",
    "http://[::1]:11434/v1",
])
async def test_loopback_provider_is_not_modified(url):
    seen: list[dict] = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return openai_response(request)

    entry = ModelEntry("local", url, "model")
    source = messages()
    events = [event async for event in ModelClient(
        entry, transport=httpx.MockTransport(handle), privacy="redact").stream(source)]
    assert events[-1].type == "done"
    assert seen[0]["messages"] == source
    assert not any(event.type == "privacy" for event in events)


@pytest.mark.parametrize("adapter", ["openai", "anthropic", "responses"])
async def test_block_makes_no_request_and_uses_no_secret(adapter):
    calls: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(500)

    entry = ModelEntry("m", "https://example.test/v1", "model", api_key="provider-auth-key") \
        if adapter == "openai" else native_entry(adapter)
    with pytest.raises(ModelError) as caught:
        _ = [event async for event in ModelClient(
            entry, transport=httpx.MockTransport(handle), privacy="block").stream(messages())]
    assert calls == []
    message = str(caught.value)
    assert "privacy.secrets=block" in message and "Nothing was sent" in message
    for secret in SECRETS:
        assert secret not in message


async def test_block_on_bounded_scan_overflow_makes_no_request(monkeypatch):
    calls: list[httpx.Request] = []
    monkeypatch.setattr(privacy, "MAX_SCAN_CHARS", 8)

    def handle(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return openai_response(request)

    entry = ModelEntry("m", "https://example.test/v1", "model")
    with pytest.raises(ModelError, match="could not be fully inspected"):
        _ = [event async for event in ModelClient(
            entry, transport=httpx.MockTransport(handle), privacy="block").stream(
                [{"role": "user", "content": "x" * 32}])]
    assert calls == []


@pytest.fixture
def agent_setup(tmp_path):
    cfg = load_config(tmp_path)
    store = Store(tmp_path / "db.sqlite")
    memory = Memory(cfg.memory_path)
    tools = build_tools(ToolContext(cfg, store, memory, lambda: datetime.now().astimezone()))
    chat = store.create_chat()["id"]
    return cfg, store, memory, tools, chat


async def test_agent_redacts_every_round_and_keeps_the_stored_transcript(agent_setup):
    cfg, store, memory, tools, chat = agent_setup
    cfg.privacy_secrets = "redact"
    tools["leak"] = Tool("leak", "Return a fixture.", {"type": "object", "properties": {}},
                         lambda args: f"file contents\n{GITHUB}")
    fake = FakeModel([tool("leak", {}), text("Done")])
    agent = Agent(cfg, store, memory, tools, transport=fake.transport)
    events = [event async for event in agent.run_turn(chat, f"my key {OPENAI}")]
    assert events[-1]["type"] == "done"
    assert sum(event["type"] == "privacy" for event in events) == 2
    assert OPENAI not in json.dumps(fake.requests[0]["messages"])
    assert GITHUB not in json.dumps(fake.requests[1]["messages"])
    assert "[REDACTED:openai_key:" in json.dumps(fake.requests[0]["messages"])
    assert "[REDACTED:github_token:" in json.dumps(fake.requests[1]["messages"])
    stored = store.get_messages(chat)
    assert stored[0] == {"role": "user", "content": f"my key {OPENAI}"}
    assert any(message["role"] == "tool" and GITHUB in message["content"]
               for message in stored)


async def test_agent_block_error_is_clear_and_sends_nothing(agent_setup):
    cfg, store, memory, tools, chat = agent_setup
    cfg.privacy_secrets = "block"
    fake = FakeModel([text("must not happen")])
    agent = Agent(cfg, store, memory, tools, transport=fake.transport)
    events = [event async for event in agent.run_turn(chat, f"my key {OPENAI}")]
    assert fake.requests == []
    assert events[-1]["type"] == "error"
    message = events[-1]["message"]
    assert "privacy.secrets=block" in message and OPENAI not in message
    assert store.get_messages(chat) == [{"role": "user", "content": f"my key {OPENAI}"}]


def test_config_defaults_off_round_trips_and_unknown_values_block(tmp_path):
    cfg = load_config(tmp_path)
    assert cfg.privacy_secrets == "off"
    cfg.privacy_secrets = "redact"
    save_config(cfg)
    assert load_config(tmp_path).privacy_secrets == "redact"

    path = tmp_path / "config.toml"
    path.write_text(path.read_text().replace('secrets = "redact"', 'secrets = "warn"'))
    assert load_config(tmp_path).privacy_secrets == "block"


def test_config_cli_sets_and_shows_privacy_mode(tmp_path, monkeypatch):
    from kova import cli
    monkeypatch.setenv("KOVA_HOME", str(tmp_path))
    load_config()
    runner = CliRunner()
    result = runner.invoke(cli.app, ["config", "set", "privacy.secrets", "redact"])
    assert result.exit_code == 0, result.output
    assert load_config().privacy_secrets == "redact"
    shown = runner.invoke(cli.app, ["config", "show", "--json"])
    assert shown.exit_code == 0, shown.output
    assert json.loads(shown.stdout)["privacy"]["secrets"] == "redact"
    bad = runner.invoke(cli.app, ["config", "set", "privacy.secrets", "warn"])
    assert bad.exit_code == 1
    assert "off, redact, block" in bad.output
