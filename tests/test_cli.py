"""CLI tests: ask, model, memory, reminders, ensure_server.

The client under test is a KovaClient whose http transport is a FastAPI
TestClient over create_app with a scripted FakeModel, so no network is needed.
"""

import json
import socket

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
from tests.fake_model import FakeModel, text, tool


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


def _ask_tools(fake, request=0):
    """The tool names of one request the model received."""
    return [t["function"]["name"] for t in fake.requests[request].get("tools", [])]


def test_ask_defaults_to_work_mode(runner):
    cli_runner, client, cfg, fake = runner
    result = cli_runner.invoke(cli.app, ["ask", "hi"])
    assert result.exit_code == 0, result.output
    chat_id = [c["id"] for c in client.list_chats(include_ask=True)][0]
    assert client.get_chat(chat_id)["mode"] == "work"
    assert "memory_save" in _ask_tools(fake) and "web_search" in _ask_tools(fake)


def test_ask_mode_chat_limits_tools(runner):
    cli_runner, client, cfg, fake = runner
    result = cli_runner.invoke(cli.app, ["ask", "hi", "--mode", "chat", "--json"])
    assert result.exit_code == 0, result.output
    assert _ask_tools(fake) == ["web_search", "web_fetch"]
    chat_id = json.loads(result.stdout)["chat_id"]
    assert client.get_chat(chat_id)["mode"] == "chat"
    assert "Current mode: chat" in fake.requests[0]["messages"][0]["content"]


def test_ask_mode_applies_to_an_existing_chat(runner):
    cli_runner, client, cfg, fake = runner
    chat_id = client.create_chat("Ongoing")["id"]
    result = cli_runner.invoke(cli.app, ["ask", "hi", "--chat", chat_id, "--mode", "chat"])
    assert result.exit_code == 0, result.output
    assert client.get_chat(chat_id)["mode"] == "chat"
    assert _ask_tools(fake) == ["web_search", "web_fetch"]


def test_ask_unknown_mode_is_a_usage_error(runner):
    cli_runner, client, cfg, fake = runner
    result = cli_runner.invoke(cli.app, ["ask", "hi", "--mode", "code"])
    assert result.exit_code == 2
    assert not fake.requests


def test_model_list_and_use(runner):
    cli_runner, client, cfg, fake = runner
    listed = cli_runner.invoke(cli.app, ["model", "list"])
    assert listed.exit_code == 0, listed.output
    assert "local" in listed.output

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
    assert "local" in result.output
    assert "qwen3" in result.output
    assert "/help" in result.output and "/think" in result.output
    assert "Show the chat mode" in result.output  # /mode and /web are in /help
    assert "Show web search state" in result.output


def test_chat_mode_and_web_commands(runner):
    cli_runner, client, cfg, fake = runner
    result = cli_runner.invoke(cli.app, ["chat", "--new", "--mode", "chat"],
                               input="/mode\nhi\n/mode work\n/mode bogus\n/web off\n/web\n/mode\n/exit\n")
    assert result.exit_code == 0, result.output
    assert "mode chat" in result.output  # the header shows the chat's mode
    assert "Current mode: chat" in result.output
    assert "Current mode: work" in result.output
    assert 'Unknown mode "bogus"' in result.output
    assert "Web search is off" in result.output
    chat = client.get_chat(client.list_chats()[0]["id"])
    assert (chat["mode"], chat["web"]) == ("work", False)
    assert [t["function"]["name"] for t in fake.requests[0]["tools"]] == ["web_search", "web_fetch"]


def test_chat_no_web_flag(runner):
    cli_runner, client, cfg, fake = runner
    result = cli_runner.invoke(cli.app, ["chat", "--new", "--no-web"], input="hi\n/exit\n")
    assert result.exit_code == 0, result.output
    chat = client.get_chat(client.list_chats()[0]["id"])
    assert chat["web"] is False
    assert "web off" in result.output
    names = [t["function"]["name"] for t in fake.requests[0]["tools"]]
    assert "web_search" not in names and "web_fetch" not in names
    assert "memory_save" in names


def test_chat_unknown_mode_is_a_usage_error(runner):
    cli_runner, client, cfg, fake = runner
    result = cli_runner.invoke(cli.app, ["chat", "--mode", "code"])
    assert result.exit_code == 2
    assert not fake.requests


def test_chats_list_shows_mode_column(runner):
    cli_runner, client, cfg, fake = runner
    client.create_chat("Deeper work")
    client.create_chat("Small talk", mode="chat")
    result = cli_runner.invoke(cli.app, ["chats", "list"])
    assert result.exit_code == 0, result.output
    assert "Mode" in result.output
    assert result.output.index("chat") < result.output.index("work")  # the chat-mode chat is newest
    assert "Small talk" in result.output and "Deeper work" in result.output


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
        (["chats", "show", "99"], "No chat 99."),
        (["chats", "show", "nope"], 'No chat "nope".'),
        (["chats", "rename", "99", "x"], "No chat 99."),
        (["chats", "rm", "99", "--yes"], "No chat 99."),
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
    with socket.socket() as probe:  # a port nothing listens on, even on a machine
        probe.bind(("127.0.0.1", 0))  # running a real Kova server on the default one
        cfg.port = probe.getsockname()[1]
    monkeypatch.setattr(client_mod, "_spawn_server", lambda cfg: None)
    with pytest.raises(KovaError) as exc:
        ensure_server(cfg, timeout=0.5)
    assert "server.log" in str(exc.value)


def test_chat_streams_reply(runner):
    cli_runner, client, cfg, fake = runner
    result = cli_runner.invoke(cli.app, ["chat", "--new"], input="hi\n/exit\n")
    assert result.exit_code == 0, result.output
    assert "Hello there!" in result.output


def test_chats_list_show_rename_and_rm(runner):
    cli_runner, client, cfg, fake = runner
    older = client.create_chat()["id"]
    list(client.stream_message(older, "zebra question"))
    newer = client.create_chat()["id"]
    list(client.stream_message(newer, "apple question"))

    listed = cli_runner.invoke(cli.app, ["chats"])
    assert listed.exit_code == 0, listed.output
    assert "apple question" in listed.output and "zebra question" in listed.output
    assert "Just now".lower() in listed.output.lower()
    assert listed.output.index("apple question") < listed.output.index("zebra question")

    shown = cli_runner.invoke(cli.app, ["chats", "show", "2"])
    assert shown.exit_code == 0, shown.output
    assert "zebra question" in shown.output
    assert "Hello there!" in shown.output
    assert "You" in shown.output and "Kova" in shown.output

    renamed = cli_runner.invoke(cli.app, ["chats", "rename", "2", "Greeting"])
    assert renamed.exit_code == 0, renamed.output
    assert "Greeting" in cli_runner.invoke(cli.app, ["chats", "list"]).output

    removed = cli_runner.invoke(cli.app, ["chats", "rm", "2"], input="y\n")
    assert removed.exit_code == 0, removed.output
    assert "Deleted" in removed.output
    remaining = cli_runner.invoke(cli.app, ["chats", "list"]).output
    assert "Greeting" not in remaining and "apple question" in remaining


def test_chats_rm_can_be_cancelled_and_yes_skips_prompt(runner):
    cli_runner, client, cfg, fake = runner
    chat_id = client.create_chat()["id"]
    client.rename_chat(chat_id, "Keep me")

    cancelled = cli_runner.invoke(cli.app, ["chats", "rm", "1"], input="n\n")
    assert cancelled.exit_code == 0, cancelled.output
    assert "Cancelled" in cancelled.output
    assert [c["id"] for c in client.list_chats()] == [chat_id]

    removed = cli_runner.invoke(cli.app, ["chats", "rm", "1", "--yes"])
    assert removed.exit_code == 0, removed.output
    assert client.list_chats() == []


def test_chats_list_empty(runner):
    cli_runner, client, cfg, fake = runner
    result = cli_runner.invoke(cli.app, ["chats"])
    assert result.exit_code == 0, result.output
    assert "No chats yet" in result.output


def test_chat_resume_accepts_number_or_id(runner, monkeypatch):
    cli_runner, client, cfg, fake = runner
    chat_id = client.create_chat()["id"]
    client.create_chat()
    seen = []
    monkeypatch.setattr(cli, "_run_chat", lambda c, chat_id=None, **kw: seen.append(chat_id))

    by_number = cli_runner.invoke(cli.app, ["chat", "--resume", "2"])
    assert by_number.exit_code == 0, by_number.output
    by_id = cli_runner.invoke(cli.app, ["chat", "--resume", chat_id])
    assert by_id.exit_code == 0, by_id.output
    assert seen == [chat_id, chat_id]


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


# ---------------------------------------------------------------------------
# kova ask: piping, files, model override, JSON, chats, reasoning, errors
# ---------------------------------------------------------------------------


def _user_text(fake) -> str:
    """The latest user message the model received."""
    return [m for m in fake.requests[-1]["messages"] if m["role"] == "user"][-1]["content"]


def test_ask_sends_piped_stdin_with_question(runner):
    cli_runner, client, cfg, fake = runner
    result = cli_runner.invoke(cli.app, ["ask", "summarize this"], input="line one\nline two\n")
    assert result.exit_code == 0, result.output
    assert result.stdout == "Hello there!\n"
    sent = _user_text(fake)
    assert sent.startswith("summarize this\n\n<stdin>\nline one\nline two\n</stdin>")


def test_ask_uses_piped_stdin_as_question(runner):
    cli_runner, client, cfg, fake = runner
    result = cli_runner.invoke(cli.app, ["ask"], input="what is 2+2?\n")
    assert result.exit_code == 0, result.output
    assert _user_text(fake) == "what is 2+2?"


def test_ask_without_question_or_input_is_a_usage_error(runner):
    cli_runner, client, cfg, fake = runner
    result = cli_runner.invoke(cli.app, ["ask"])
    assert result.exit_code == 2
    assert "pipe some text in" in result.stderr
    assert not fake.requests


def test_ask_truncates_huge_input_with_warning(runner):
    cli_runner, client, cfg, fake = runner
    result = cli_runner.invoke(cli.app, ["ask", "count"], input="x" * (cli.ASK_INPUT_LIMIT + 5000))
    assert result.exit_code == 0, result.output
    assert "Warning: piped input is over 100 KB" in result.stderr
    assert "Warning" not in result.stdout
    assert _user_text(fake).count("x") == cli.ASK_INPUT_LIMIT


def test_ask_attaches_files(runner, tmp_path):
    cli_runner, client, cfg, fake = runner
    first, second = tmp_path / "notes.txt", tmp_path / "todo.md"
    first.write_text("buy milk")
    second.write_text("- ship kova")
    result = cli_runner.invoke(cli.app, ["ask", "what's in these?", "-f", str(first), "--file", str(second)])
    assert result.exit_code == 0, result.output
    sent = _user_text(fake)
    assert '<file name="notes.txt">\nbuy milk\n</file>' in sent
    assert '<file name="todo.md">\n- ship kova\n</file>' in sent
    assert sent.index("what's in these?") < sent.index("notes.txt") < sent.index("todo.md")


def test_ask_missing_file_is_a_usage_error(runner, tmp_path):
    cli_runner, client, cfg, fake = runner
    result = cli_runner.invoke(cli.app, ["ask", "hi", "-f", str(tmp_path / "nope.txt")])
    assert result.exit_code == 2
    assert not fake.requests


def test_ask_model_override_is_one_shot(runner):
    cli_runner, client, cfg, fake = runner
    client.model_add("other", "http://localhost:11434/v1", "qwen3")
    result = cli_runner.invoke(cli.app, ["ask", "hi", "--model", "other", "--json"])
    assert result.exit_code == 0, result.output
    assert fake.requests[0]["model"] == "qwen3"
    assert json.loads(result.stdout)["model"] == "other"
    assert cfg.active == "local"
    assert client.health()["model"] == "local"


def test_ask_unknown_model_fails_on_stderr(runner):
    cli_runner, client, cfg, fake = runner
    result = cli_runner.invoke(cli.app, ["ask", "hi", "-m", "missing"])
    assert result.exit_code == 1
    assert result.stdout == ""
    assert result.stderr.strip() == "Error: unknown model 'missing'"
    assert not fake.requests


def test_ask_json_output(runner):
    cli_runner, client, cfg, fake = runner
    fake.scripts = [tool("memory_search", {"query": "dog"}), text("Rex!")]
    result = cli_runner.invoke(cli.app, ["ask", "my dog?", "--json"])
    assert result.exit_code == 0, result.output
    data = json.loads(result.stdout)
    assert data["answer"] == "Rex!"
    assert data["model"] == "local"
    assert data["tools"] == ["memory_search"]
    assert client.get_chat(data["chat_id"]) is not None


def test_ask_marks_its_chat_and_continues_existing_ones(runner):
    cli_runner, client, cfg, fake = runner
    first = cli_runner.invoke(cli.app, ["ask", "remember 42", "--json"])
    chat_id = json.loads(first.stdout)["chat_id"]
    assert client.get_chat(chat_id)["title"] == "[ask] remember 42"

    second = cli_runner.invoke(cli.app, ["ask", "what number?", "--chat", chat_id, "--json"])
    assert second.exit_code == 0, second.output
    assert json.loads(second.stdout)["chat_id"] == chat_id
    assert len(client.list_chats(include_ask=True)) == 1
    users = [m["content"] for m in fake.requests[-1]["messages"] if m["role"] == "user"]
    assert users == ["remember 42", "what number?"]


def test_ask_unknown_chat_fails_on_stderr(runner):
    cli_runner, client, cfg, fake = runner
    result = cli_runner.invoke(cli.app, ["ask", "hi", "--chat", "nope"])
    assert result.exit_code == 1
    assert result.stderr.strip() == "Error: chat not found"


@pytest.mark.parametrize("think", [False, True])
def test_ask_think_prints_reasoning_to_stderr_only(runner, think):
    cli_runner, client, cfg, fake = runner
    fake.scripts = [[{"reasoning_content": "pondering"}, {"content": "The answer"}]]
    result = cli_runner.invoke(cli.app, ["ask", "q"] + (["--think"] if think else []))
    assert result.exit_code == 0, result.output
    assert result.stdout == "The answer\n"
    assert ("pondering" in result.stderr) is think


def test_ask_model_error_exits_1_on_stderr(runner, monkeypatch):
    cli_runner, client, cfg, fake = runner

    def broken(chat_id, text, model=None):
        yield {"type": "text", "text": "Part"}
        yield {"type": "error", "message": "model server said no"}

    monkeypatch.setattr(client, "stream_message", broken)
    result = cli_runner.invoke(cli.app, ["ask", "hi"])
    assert result.exit_code == 1
    assert result.stderr.strip() == "Error: model server said no"
    assert "Error" not in result.stdout


def test_ask_unreachable_server_exits_1_on_stderr(runner, monkeypatch):
    cli_runner, client, cfg, fake = runner
    with httpx.Client(base_url=client.base_url, transport=httpx.MockTransport(_refuse_connection)) as http:
        monkeypatch.setattr(client, "http", http)
        result = cli_runner.invoke(cli.app, ["ask", "hi"])
    assert result.exit_code == 1
    assert result.stdout == ""
    assert "Cannot reach Kova server" in result.stderr


def test_ask_chats_are_hidden_from_history_and_default_chat(runner):
    cli_runner, client, cfg, fake = runner
    real = client.create_chat()["id"]
    client.stream_message(real, "a real conversation").__next__()  # give it a message
    result = cli_runner.invoke(cli.app, ["ask", "throwaway"])
    assert result.exit_code == 0, result.output
    ids = [c["id"] for c in client.list_chats()]
    assert real in ids
    assert all(not (c.get("title") or "").startswith("[ask]") for c in client.list_chats())
    assert any((c.get("title") or "").startswith("[ask]") for c in client.list_chats(include_ask=True))
