"""CLI tests: ask, model, memory, reminders, ensure_server.

The client under test is a KovaClient whose http transport is a FastAPI
TestClient over create_app with a scripted FakeModel, so no network is needed.
"""

import httpx
import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

import kova.cli as cli
import kova.client as client_mod
import kova.ui as ui
from kova.client import KovaClient, KovaError, ensure_server
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


@pytest.mark.parametrize("obsidian", [False, True])
def test_memory_use_updates_running_server(runner, tmp_path, monkeypatch, obsidian):
    cli_runner, client, cfg, fake = runner
    monkeypatch.setattr(cli, "KovaClient", lambda *args: client)
    folder = tmp_path / "new-memory"
    args = ["memory", "use", str(folder)] + (["--obsidian"] if obsidian else [])
    result = cli_runner.invoke(cli.app, args)
    assert result.exit_code == 0, result.output
    assert load_config().memory_path == folder
    assert cli_runner.invoke(cli.app, ["memory", "add", "New note", "new body"]).exit_code == 0
    target = folder / "Kova" if obsidian else folder
    assert (target / "new-note.md").is_file()
    app = client.http.app
    app.state.agent.tools["memory_save"].run({"title": "Tool note", "body": "tool body"})
    assert (target / "tool-note.md").is_file()
    assert app.state.agent.memory.search("Tool note")[0].title == "Tool note"


def test_memory_use_offline(tmp_path, monkeypatch):
    from kova.client import KovaError

    monkeypatch.setenv("KOVA_HOME", str(tmp_path))
    def offline(*args):
        raise KovaError(None, "offline")
    monkeypatch.setattr(KovaClient, "memory_use", offline)
    folder = tmp_path / "vault"
    result = CliRunner().invoke(cli.app, ["memory", "use", str(folder), "--obsidian"])
    assert result.exit_code == 0, result.output
    cfg = load_config()
    assert cfg.memory_path == folder
    assert cfg.memory_write_subdir == "Kova"


def test_chat_model_commands_and_help(runner):
    cli_runner, client, cfg, fake = runner
    client.model_add("other", "http://localhost:11434/v1", "qwen3")
    result = cli_runner.invoke(cli.app, ["chat", "--new"],
                               input="/model\n/model other\n/model missing\n/model\n/help\n/exit\n")
    assert result.exit_code == 0, result.output
    assert 'No model named "missing"' in result.output
    assert cfg.active == "other"
    assert "tiel" in result.output
    assert "qwen3" in result.output
    assert "/help" in result.output and "/think" in result.output


def test_reminders_add_list(runner):
    cli_runner, client, cfg, fake = runner
    added = cli_runner.invoke(cli.app, ["reminders", "add", "2030-01-01T10:00", "Buy milk"])
    assert added.exit_code == 0, added.output

    listed = cli_runner.invoke(cli.app, ["reminders", "list"])
    assert listed.exit_code == 0, listed.output
    assert "Buy milk" in listed.output


@pytest.mark.parametrize(
    ("args", "message"),
    [
        (["memory", "rm", "nope"], 'No note named "nope".'),
        (["memory", "show", "nope"], 'No note named "nope".'),
        (["reminders", "rm", "999"], "No reminder #999."),
        (["model", "use", "nope"], 'No model named "nope". Run: kova model list'),
    ],
)
def test_missing_item_shows_friendly_error(runner, args, message):
    cli_runner, client, cfg, fake = runner
    result = cli_runner.invoke(cli.app, args)
    assert result.exit_code == 1, result.output
    assert result.output.strip() == message
    assert "Traceback" not in result.output
    assert isinstance(result.exception, SystemExit)


def test_client_http_errors_and_missing_deletes(runner):
    cli_runner, client, cfg, fake = runner
    with pytest.raises(KovaError) as exc:
        client.model_use("nope")
    assert exc.value.status == 404
    assert exc.value.message == "unknown model 'nope'"
    assert client.memory_delete("nope") is False
    assert client.reminder_delete(999) is False
    assert client.delete_chat("nope") is False


def _refuse_connection(request):
    raise httpx.ConnectError("Connection refused", request=request)


def test_unreachable_server_shows_friendly_error(runner, monkeypatch):
    cli_runner, client, cfg, fake = runner
    with httpx.Client(base_url=client.base_url, transport=httpx.MockTransport(_refuse_connection)) as http:
        monkeypatch.setattr(client, "http", http)
        result = cli_runner.invoke(cli.app, ["memory", "list"])
    assert result.exit_code == 1, result.output
    assert result.output.strip() == f"Cannot reach Kova server at {client.base_url}."
    assert "Traceback" not in result.output
    assert isinstance(result.exception, SystemExit)


def test_server_start_failure_shows_friendly_error(tmp_path, monkeypatch):
    monkeypatch.setenv("KOVA_HOME", str(tmp_path))
    base = f"http://127.0.0.1:{load_config().port}"
    with httpx.Client(base_url=base, transport=httpx.MockTransport(_refuse_connection)) as http:
        client = KovaClient(base, "", http=http)
        monkeypatch.setattr(client_mod, "KovaClient", lambda *args: client)
        monkeypatch.setattr(client_mod, "_spawn_server", lambda cfg: None)
        monkeypatch.setattr(cli, "ensure_server", lambda cfg: ensure_server(cfg, timeout=0))
        result = CliRunner().invoke(cli.app, ["memory", "list"])
    assert result.exit_code == 1, result.output
    assert "Cannot reach Kova server" in result.output
    assert "server.log" in result.output
    assert len(result.output.splitlines()) == 1
    assert "Traceback" not in result.output
    assert isinstance(result.exception, SystemExit)


def test_ensure_server_timeout_message(tmp_path, monkeypatch):
    monkeypatch.setenv("KOVA_HOME", str(tmp_path))
    cfg = load_config()
    monkeypatch.setattr(client_mod, "_spawn_server", lambda cfg: None)
    with pytest.raises(KovaError) as exc:
        ensure_server(cfg, timeout=0.5)
    assert "server.log" in str(exc.value)


def test_chat_streams_reply(runner):
    cli_runner, client, cfg, fake = runner
    result = cli_runner.invoke(cli.app, ["chat", "--new"], input="hi\n/exit\n")
    assert result.exit_code == 0, result.output
    assert "Hello there!" in result.output


def test_render_turn_streams_reasoning_then_answer(tmp_path, monkeypatch):
    """Regression: reasoning used to crash (Console.print(dim=...)) and ran into the answer."""
    import io

    from rich.console import Console

    monkeypatch.setenv("KOVA_HOME", str(tmp_path))
    cfg = load_config()
    fake = FakeModel([[{"reasoning_content": "thinking hard"}, {"content": "The answer"}]])
    with TestClient(create_app(cfg, transport=fake.transport)) as http:
        client = KovaClient("http://127.0.0.1:7700", ensure_token(cfg.home), http=http)
        chat_id = client.create_chat()["id"]
        out = io.StringIO()
        ui._render_turn(client, chat_id, "zebra question", False, Console(file=out, width=200),
                        show_thinking=True)
    rendered = out.getvalue()
    assert rendered.index("thinking hard") < rendered.index("The answer")
    assert "zebra" not in out.getvalue()  # the user's line is not echoed back


def test_chat_think_toggle_and_no_think(runner):
    cli_runner, client, cfg, fake = runner
    fake.scripts = [
        [{"reasoning_content": "hidden first"}, {"content": "First reply"}],
        [{"reasoning_content": "visible middle"}, {"content": "Middle reply"}],
        [{"reasoning_content": "hidden last"}, {"content": "Last reply"}],
    ]
    result = cli_runner.invoke(cli.app, ["chat", "--new", "--no-think"],
                               input="first\n/think\nmiddle\n/think\nlast\n/exit\n")
    assert result.exit_code == 0, result.output
    assert "hidden first" not in result.output
    assert "visible middle" in result.output
    assert "hidden last" not in result.output
    assert all(reply in result.output for reply in ("First reply", "Middle reply", "Last reply"))


def test_chat_clear_preserves_conversation_and_new_resets_it(runner):
    cli_runner, client, cfg, fake = runner
    result = cli_runner.invoke(cli.app, ["chat", "--new"],
                               input="first\n/clear\nsecond\n/new\n/quit\n")
    assert result.exit_code == 0, result.output
    chats = client.list_chats()
    assert len(chats) == 2
    messages = [client.get_chat(chat["id"])["messages"] for chat in chats]
    assert sorted(len(items) for items in messages) == [0, 4]
    user_messages = [message["content"] for items in messages for message in items
                     if message["role"] == "user"]
    assert user_messages == ["first", "second"]
    assert "\x1b[" not in result.output  # /clear must not emit cursor controls to pipes.


def test_chat_interrupt_returns_to_prompt(runner, monkeypatch):
    cli_runner, client, cfg, fake = runner
    original = client.stream_message
    closed = []

    def stream(chat_id, message):
        if message == "interrupt":
            try:
                yield {"type": "text", "text": "Partial reply"}
                raise KeyboardInterrupt
            finally:
                closed.append(True)
        else:
            yield from original(chat_id, message)

    monkeypatch.setattr(client, "stream_message", stream)
    result = cli_runner.invoke(cli.app, ["chat", "--new"], input="interrupt\nhi\n/exit\n")
    assert result.exit_code == 0, result.output
    assert "Partial reply" in result.output
    assert "Hello there!" in result.output
    assert closed == [True]


def test_chat_unknown_command_is_not_sent_to_model(runner):
    cli_runner, client, cfg, fake = runner
    result = cli_runner.invoke(cli.app, ["chat"], input="/typo\n")
    assert result.exit_code == 0, result.output
    assert not fake.requests
