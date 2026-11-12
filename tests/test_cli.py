"""CLI tests: ask, model, memory, reminders, ensure_server.

The client under test is a KovaClient whose http transport is a FastAPI
TestClient over create_app with a scripted FakeModel, so no network is needed.
"""

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

import kova.cli as cli
import kova.client as client_mod
from kova.client import KovaClient, ensure_server
from kova.config import ensure_token, load_config
from kova.server import create_app
from tests.fake_model import FakeModel, text


@pytest.fixture
def runner(tmp_path, monkeypatch):
    """A CliRunner wired to a server-backed KovaClient."""
    monkeypatch.setenv("KOVA_HOME", str(tmp_path))
    cfg = load_config()
    fake = FakeModel([text("Hello there!"), text("Second")])
    testapp = create_app(cfg, transport=fake.transport)
    cli_runner = CliRunner()
    with TestClient(testapp) as client:
        kova_client = KovaClient("http://127.0.0.1:7700", ensure_token(cfg.home), http=client)
        monkeypatch.setattr(cli, "get_client", lambda: kova_client)
        yield cli_runner, kova_client, cfg, fake


def test_ask_prints_answer(runner):
    cli_runner, client, cfg, fake = runner
    result = cli_runner.invoke(cli.app, ["ask", "hi"])
    assert result.exit_code == 0, result.output
    assert "Hello there!" in result.output
    assert len(fake.requests) == 1


def test_model_list_and_use(runner):
    cli_runner, client, cfg, fake = runner
    listed = cli_runner.invoke(cli.app, ["model", "list"])
    assert listed.exit_code == 0, listed.output
    assert "tiel" in listed.output

    added = cli_runner.invoke(cli.app, ["model", "add", "ollama", "http://localhost:11434/v1", "qwen3"])
    assert added.exit_code == 0, added.output

    used = cli_runner.invoke(cli.app, ["model", "use", "ollama"])
    assert used.exit_code == 0, used.output
    assert cfg.active == "ollama"


def test_memory_add_list_rm(runner):
    cli_runner, client, cfg, fake = runner
    added = cli_runner.invoke(cli.app, ["memory", "add", "Dog", "Rex is a good dog"])
    assert added.exit_code == 0, added.output

    listed = cli_runner.invoke(cli.app, ["memory", "list"])
    assert listed.exit_code == 0, listed.output
    assert "Dog" in listed.output

    removed = cli_runner.invoke(cli.app, ["memory", "rm", "dog"])
    assert removed.exit_code == 0, removed.output


def test_reminders_add_list(runner):
    cli_runner, client, cfg, fake = runner
    added = cli_runner.invoke(cli.app, ["reminders", "add", "2030-01-01T10:00", "Buy milk"])
    assert added.exit_code == 0, added.output

    listed = cli_runner.invoke(cli.app, ["reminders", "list"])
    assert listed.exit_code == 0, listed.output
    assert "Buy milk" in listed.output


def test_ensure_server_timeout_message(tmp_path, monkeypatch):
    monkeypatch.setenv("KOVA_HOME", str(tmp_path))
    cfg = load_config()
    monkeypatch.setattr(client_mod, "_spawn_server", lambda cfg: None)
    with pytest.raises(RuntimeError) as exc:
        ensure_server(cfg, timeout=0.5)
    assert "server.log" in str(exc.value)


def test_chat_streams_reply(runner):
    cli_runner, client, cfg, fake = runner
    result = cli_runner.invoke(cli.app, ["chat", "--new"], input="hi\n/exit\n")
    assert result.exit_code == 0, result.output
    assert "Hello there!" in result.output
