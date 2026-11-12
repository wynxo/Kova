"""Tests for the first-run setup wizard and non-interactive flags.

All model-server HTTP goes through ``httpx.MockTransport``; no real network.
"""

from __future__ import annotations

import json

import httpx
import pytest
from typer.testing import CliRunner

import kova.cli as cli
from kova import onboarding
from kova.config import ModelEntry, load_config


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("KOVA_HOME", str(tmp_path))
    return tmp_path


@pytest.fixture
def no_live(monkeypatch):
    """Pretend no kova server is running, so setup writes the file."""
    monkeypatch.setattr(onboarding, "_running_client", lambda cfg: None)


def feed(monkeypatch, *answers):
    queue = list(answers)

    def fake_input(*args, **kwargs):
        if not queue:
            raise EOFError
        return queue.pop(0)

    monkeypatch.setattr("builtins.input", fake_input)


def no_input(monkeypatch):
    def boom(*args, **kwargs):
        raise AssertionError("setup asked for input in non-interactive mode")

    monkeypatch.setattr("builtins.input", boom)


def _sse(reply: str) -> str:
    pieces = [reply[i:i + 3] for i in range(0, len(reply), 3)] or [""]
    frames = "".join(
        f'data: {json.dumps({"choices": [{"delta": {"content": piece}}]})}\n\n'
        for piece in pieces
    )
    return frames + "data: [DONE]\n\n"


class FakeServers:
    """A tiny stand-in for the OpenAI-compatible servers on the network."""

    def __init__(self, servers, *, locked=None, chat_status=200, reply="pong"):
        self.servers = servers            # origin -> [model ids]
        self.locked = locked or {}        # origin -> [model ids], only with a key
        self.chat_status = chat_status
        self.reply = reply
        self.chat_requests: list[dict] = []
        self.auth: list[str | None] = []
        self.transport = httpx.MockTransport(self._handle)

    def _handle(self, request):
        port = request.url.port
        root = f"{request.url.scheme}://{request.url.host}" + (f":{port}" if port else "")
        authorization = request.headers.get("authorization")
        self.auth.append(authorization)
        if root in self.locked:
            if not authorization:
                return httpx.Response(401, json={"error": "unauthorized"})
            models = self.locked[root]
        elif root in self.servers:
            models = self.servers[root]
        else:
            raise httpx.ConnectError("connection refused", request=request)

        if request.method == "GET" and request.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": [{"id": model} for model in models]})

        self.chat_requests.append(json.loads(request.content))
        if self.chat_status >= 400:
            return httpx.Response(self.chat_status, json={"error": "boom"})
        return httpx.Response(200, text=_sse(self.reply), headers={"content-type": "text/event-stream"})


# ---------------------------------------------------------------------------
# Detection
# ---------------------------------------------------------------------------


def test_detect_servers_probes_in_parallel_and_ignores_silence():
    fake = FakeServers({
        "http://127.0.0.1:11434": ["qwen3:8b", "llama3.2"],
        "http://127.0.0.1:8080": ["local"],
    })
    found = onboarding.detect_servers(transport=fake.transport)
    assert [server.label for server in found] == ["Ollama", "llama.cpp / PolyStrata"]
    assert [server.base_url for server in found] == [
        "http://127.0.0.1:11434/v1",
        "http://127.0.0.1:8080/v1",
    ]
    assert found[0].models == ["qwen3:8b", "llama3.2"]
    assert len(found[1].models) == 1
    assert not any(server.needs_key for server in found)


def test_detect_servers_marks_401_as_found_but_locked():
    fake = FakeServers({}, locked={"http://127.0.0.1:1234": ["local-model"]})
    found = onboarding.detect_servers(transport=fake.transport)
    assert len(found) == 1
    assert found[0].label == "LM Studio" and found[0].needs_key
    assert found[0].models == []


@pytest.mark.parametrize(
    ("typed", "expected"),
    [
        ("http://127.0.0.1:11434", "http://127.0.0.1:11434/v1"),
        ("http://127.0.0.1:11434/", "http://127.0.0.1:11434/v1"),
        ("http://127.0.0.1:11434/v1", "http://127.0.0.1:11434/v1"),
        ("http://127.0.0.1:11434/v1/models", "http://127.0.0.1:11434/v1"),
        ("192.168.1.20:8080", "http://192.168.1.20:8080/v1"),
        ("https://models.example.com", "https://models.example.com/v1"),
        ("", ""),
    ],
)
def test_normalize_base(typed, expected):
    assert onboarding.normalize_base(typed) == expected


def test_probe_returns_none_when_nothing_answers():
    fake = FakeServers({})
    assert onboarding.probe_url("http://127.0.0.1:9999", transport=fake.transport) is None


def test_probe_tolerates_a_malformed_url():
    assert onboarding.probe_url("http://host:abc") is None


# ---------------------------------------------------------------------------
# needs_setup
# ---------------------------------------------------------------------------


def test_needs_setup_false_when_active_model_answers(home):
    cfg = load_config()
    cfg.models[:] = [ModelEntry("ollama", "http://127.0.0.1:11434/v1", "qwen3")]
    cfg.active = "ollama"
    fake = FakeServers({"http://127.0.0.1:11434": ["qwen3"]})
    assert onboarding.needs_setup(cfg, transport=fake.transport) is False


def test_needs_setup_true_when_unreachable_empty_or_locked(home):
    cfg = load_config()
    fake = FakeServers({"http://127.0.0.1:11434": ["qwen3"]}, locked={"http://127.0.0.1:1234": ["m"]})

    cfg.models[:] = [ModelEntry("gone", "http://127.0.0.1:9999/v1", "x")]
    cfg.active = "gone"
    assert onboarding.needs_setup(cfg, transport=fake.transport) is True

    cfg.models[:] = []
    assert onboarding.needs_setup(cfg, transport=fake.transport) is True

    cfg.models[:] = [ModelEntry("locked", "http://127.0.0.1:1234/v1", "m")]
    cfg.active = "locked"
    assert onboarding.needs_setup(cfg, transport=fake.transport) is True


def test_needs_setup_true_when_active_name_is_missing(home):
    cfg = load_config()
    cfg.models[:] = []
    cfg.active = "nope"
    assert onboarding.needs_setup(cfg) is True


# ---------------------------------------------------------------------------
# Interactive wizard
# ---------------------------------------------------------------------------


def test_wizard_picks_server_and_model_and_saves(home, monkeypatch, no_live, capsys):
    fake = FakeServers({"http://127.0.0.1:11434": ["qwen3:8b", "llama3.2"]})
    feed(monkeypatch, "1", "2", "", "")

    onboarding.run_setup(load_config(), transport=fake.transport)

    saved = load_config()
    assert saved.active == "ollama"
    entry = saved.active_model()
    assert entry.name == "ollama"
    assert entry.model == "llama3.2"
    assert entry.base_url == "http://127.0.0.1:11434/v1"
    assert entry.api_key is None
    assert fake.chat_requests[0]["model"] == "llama3.2"

    out = capsys.readouterr().out
    assert "Model servers found" in out
    assert "Ollama" in out and "11434" in out
    assert "It works!" in out
    assert "You're set. Run: kova" in out


def test_wizard_enter_picks_first_model(home, monkeypatch, no_live):
    fake = FakeServers({"http://127.0.0.1:11434": ["qwen3:8b", "llama3.2"]})
    feed(monkeypatch, "1", "", "", "")

    onboarding.run_setup(load_config(), transport=fake.transport)

    assert load_config().active_model().model == "qwen3:8b"


def test_wizard_accepts_typed_url_when_nothing_found(home, monkeypatch, no_live, capsys):
    fake = FakeServers({"http://192.168.1.20:8080": ["local"]})
    feed(monkeypatch, "http://192.168.1.20:8080", "", "", "")

    onboarding.run_setup(load_config(), transport=fake.transport)

    saved = load_config()
    assert saved.active_model().model == "local"
    assert saved.active_model().base_url == "http://192.168.1.20:8080/v1"
    assert saved.active_model().name == "192-168-1-20-8080"
    out = capsys.readouterr().out
    assert "No model server found on this computer." in out
    assert "http://192.168.1.20:8080" in out


def test_wizard_retries_a_bad_typed_url(home, monkeypatch, no_live, capsys):
    fake = FakeServers({"http://192.168.1.20:8080": ["local"]})
    feed(monkeypatch, "http://127.0.0.1:9999", "http://192.168.1.20:8080", "", "")

    onboarding.run_setup(load_config(), transport=fake.transport)

    out = capsys.readouterr().out
    assert "Couldn't reach a model server at http://127.0.0.1:9999/v1/models." in out
    assert load_config().active_model().base_url == "http://192.168.1.20:8080/v1"


def test_wizard_asks_for_key_only_after_401(home, monkeypatch, no_live):
    fake = FakeServers({}, locked={"http://127.0.0.1:1234": ["local-model"]})
    feed(monkeypatch, "1", "sk-secret", "", "", "")

    onboarding.run_setup(load_config(), transport=fake.transport)

    entry = load_config().active_model()
    assert entry.name == "lmstudio"
    assert entry.model == "local-model"
    assert entry.api_key == "sk-secret"
    assert "Bearer sk-secret" in fake.auth


def test_wizard_sets_obsidian_memory_folder(home, monkeypatch, no_live, tmp_path):
    vault = tmp_path / "MyVault"
    vault.mkdir()
    fake = FakeServers({"http://127.0.0.1:11434": ["qwen3"]})
    feed(monkeypatch, "1", "", "", "2", str(vault))

    onboarding.run_setup(load_config(), transport=fake.transport)

    saved = load_config()
    assert saved.memory_path == vault
    assert saved.memory_write_subdir == "Kova"


def test_wizard_keeps_default_memory(home, monkeypatch, no_live):
    fake = FakeServers({"http://127.0.0.1:11434": ["qwen3"]})
    feed(monkeypatch, "1", "", "", "")

    onboarding.run_setup(load_config(), transport=fake.transport)

    saved = load_config()
    assert saved.memory_path == home / "memory"
    assert saved.memory_write_subdir is None


def test_wizard_can_save_after_a_failed_test(home, monkeypatch, no_live):
    fake = FakeServers({"http://127.0.0.1:11434": ["qwen3"]}, chat_status=500)
    feed(monkeypatch, "1", "", "", "y", "")

    onboarding.run_setup(load_config(), transport=fake.transport)

    saved = load_config()
    assert saved.active == "ollama"
    assert saved.active_model().model == "qwen3"


def test_wizard_declining_a_failed_test_saves_nothing(home, monkeypatch, no_live):
    fake = FakeServers({"http://127.0.0.1:11434": ["qwen3"]}, chat_status=500)
    feed(monkeypatch, "1", "", "", "n")

    with pytest.raises(onboarding.SetupError, match="Nothing saved"):
        onboarding.run_setup(load_config(), transport=fake.transport)

    assert load_config().active == "local"


# ---------------------------------------------------------------------------
# Non-interactive flags
# ---------------------------------------------------------------------------


def test_flags_do_it_without_prompts(home, monkeypatch, no_live, capsys):
    no_input(monkeypatch)
    fake = FakeServers({"http://192.168.1.20:8080": ["local"]})

    onboarding.run_setup(
        load_config(),
        url="http://192.168.1.20:8080",
        model="local",
        name="homelab",
        api_key="sk-x",
        yes=True,
        transport=fake.transport,
    )

    saved = load_config()
    assert saved.active == "homelab"
    entry = saved.active_model()
    assert (entry.base_url, entry.model, entry.api_key) == (
        "http://192.168.1.20:8080/v1",
        "local",
        "sk-x",
    )
    assert fake.chat_requests[0]["model"] == "local"
    assert "Bearer sk-x" in fake.auth
    assert "You're set. Run: kova" in capsys.readouterr().out


def test_flags_use_first_model_when_model_is_omitted(home, monkeypatch, no_live):
    no_input(monkeypatch)
    fake = FakeServers({"http://127.0.0.1:11434": ["qwen3:8b", "llama3.2"]})

    onboarding.run_setup(load_config(), url="http://127.0.0.1:11434", transport=fake.transport)

    saved = load_config()
    assert saved.active == "ollama"
    assert saved.active_model().model == "qwen3:8b"


def test_flags_need_a_key_when_server_is_locked(home, monkeypatch, no_live):
    no_input(monkeypatch)
    fake = FakeServers({}, locked={"http://127.0.0.1:1234": ["m"]})

    with pytest.raises(onboarding.SetupError, match="--key"):
        onboarding.run_setup(load_config(), url="http://127.0.0.1:1234", model="m", transport=fake.transport)

    onboarding.run_setup(
        load_config(),
        url="http://127.0.0.1:1234",
        model="m",
        api_key="sk-ok",
        yes=True,
        transport=fake.transport,
    )
    assert load_config().active_model().api_key == "sk-ok"


def test_flags_fail_clearly_when_nothing_answers(home, no_live):
    fake = FakeServers({})

    with pytest.raises(onboarding.SetupError, match="Couldn't reach"):
        onboarding.run_setup(load_config(), url="http://192.168.1.20:8080", model="local", transport=fake.transport)


def test_flags_need_model_when_server_lists_none(home, no_live):
    fake = FakeServers({"http://127.0.0.1:11434": []})

    with pytest.raises(onboarding.SetupError, match="--model"):
        onboarding.run_setup(load_config(), url="http://127.0.0.1:11434", transport=fake.transport)


def test_flags_refuse_to_save_a_failing_test_without_yes(home, no_live):
    fake = FakeServers({"http://127.0.0.1:11434": ["qwen3"]}, chat_status=500)

    with pytest.raises(onboarding.SetupError, match="--yes"):
        onboarding.run_setup(load_config(), url="http://127.0.0.1:11434", model="qwen3", transport=fake.transport)

    assert load_config().active == "local"


def test_flags_save_a_failing_test_with_yes(home, monkeypatch, no_live, capsys):
    fake = FakeServers({"http://127.0.0.1:11434": ["qwen3"]}, chat_status=500)

    onboarding.run_setup(
        load_config(), url="http://127.0.0.1:11434", model="qwen3", yes=True, transport=fake.transport
    )

    assert load_config().active == "ollama"
    out = capsys.readouterr().out
    assert "The test message failed" in out
    assert "Traceback" not in out


def test_flags_reject_model_or_name_without_url(home, no_live):
    with pytest.raises(onboarding.SetupError, match="--url"):
        onboarding.run_setup(load_config(), model="local")
    with pytest.raises(onboarding.SetupError, match="--url"):
        onboarding.run_setup(load_config(), name="homelab")


# ---------------------------------------------------------------------------
# Saving through a live server
# ---------------------------------------------------------------------------


class FakeLiveClient:
    def __init__(self):
        self.added = None
        self.used = None
        self.memory = None
        self.http = type("Http", (), {"close": lambda self: None})()

    def model_add(self, *args):
        self.added = args

    def model_use(self, name):
        self.used = name

    def memory_use(self, path, obsidian):
        self.memory = (path, obsidian)


def test_live_server_takes_the_change_without_rewriting_config(home, monkeypatch):
    live = FakeLiveClient()
    monkeypatch.setattr(onboarding, "_running_client", lambda cfg: live)
    fake = FakeServers({"http://127.0.0.1:11434": ["qwen3"]})

    onboarding.run_setup(
        load_config(), url="http://127.0.0.1:11434", model="qwen3", yes=True, transport=fake.transport
    )

    assert live.added == ("ollama", "http://127.0.0.1:11434/v1", "qwen3", None)
    assert live.used == "ollama"
    assert live.memory is None
    assert load_config().active == "local"  # the live server owns the config, not this process


def test_live_server_gets_the_obsidian_folder(home, monkeypatch, tmp_path):
    live = FakeLiveClient()
    monkeypatch.setattr(onboarding, "_running_client", lambda cfg: live)
    vault = tmp_path / "Vault"
    vault.mkdir()
    fake = FakeServers({"http://127.0.0.1:11434": ["qwen3"]})
    feed(monkeypatch, "1", "", "", "2", str(vault))

    onboarding.run_setup(load_config(), transport=fake.transport)

    assert live.memory == (str(vault.resolve()), True)


# ---------------------------------------------------------------------------
# CLI wiring
# ---------------------------------------------------------------------------


def test_cli_setup_forwards_flags(home, monkeypatch, no_live, capsys):
    fake = FakeServers({"http://192.168.1.20:8080": ["local"]})
    calls = {}
    real = onboarding.run_setup

    def spy(cfg, **kwargs):
        calls.update(kwargs)
        real(cfg, transport=fake.transport, **kwargs)

    monkeypatch.setattr(onboarding, "run_setup", spy)
    result = CliRunner().invoke(
        cli.app,
        ["setup", "--url", "http://192.168.1.20:8080", "--model", "local",
         "--name", "homelab", "--key", "sk-x", "--yes"],
    )

    assert result.exit_code == 0, result.output
    assert calls == {
        "url": "http://192.168.1.20:8080",
        "model": "local",
        "name": "homelab",
        "api_key": "sk-x",
        "yes": True,
    }
    assert load_config().active == "homelab"


def test_cli_setup_shows_a_friendly_error(home, monkeypatch):
    def explode(cfg, **kwargs):
        raise onboarding.SetupError("Couldn't reach a model server.")

    monkeypatch.setattr(onboarding, "run_setup", explode)
    result = CliRunner().invoke(cli.app, ["setup", "--url", "http://192.168.1.20:8080", "--model", "local"])

    assert result.exit_code == 1, result.output
    assert result.output.strip() == "Couldn't reach a model server."
    assert "Traceback" not in result.output


def test_cli_setup_runs_the_wizard(home, monkeypatch, no_live):
    fake = FakeServers({"http://127.0.0.1:11434": ["qwen3"]})
    real = onboarding.run_setup
    monkeypatch.setattr(
        onboarding, "run_setup", lambda cfg, **kwargs: real(cfg, transport=fake.transport, **kwargs)
    )
    feed(monkeypatch, "1", "", "", "")

    result = CliRunner().invoke(cli.app, ["setup"])

    assert result.exit_code == 0, result.output
    assert "You're set. Run: kova" in result.output
    assert load_config().active == "ollama"
