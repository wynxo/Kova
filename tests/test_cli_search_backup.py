"""CLI coverage for chat search, per-chat export and full backup/restore."""

import json
import zipfile
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

import kova.cli as cli
from kova.client import KovaClient
from kova.config import ensure_token, load_config, save_config
from kova.memory import Memory
from kova.server import create_app
from kova.store import Store
from tests.fake_model import FakeModel, text


@pytest.fixture
def runner(tmp_path, monkeypatch):
    """A CLI wired to a TestClient-backed server over a temp KOVA_HOME."""
    monkeypatch.setenv("KOVA_HOME", str(tmp_path / "home"))
    cfg = load_config()
    fake = FakeModel([text("Hello there!")])
    testapp = create_app(cfg, transport=fake.transport)
    cli_runner = CliRunner()
    with TestClient(testapp) as http:
        kova_client = KovaClient("http://127.0.0.1:7700", ensure_token(cfg.home), http=http)
        monkeypatch.setattr(cli, "get_client", lambda: kova_client)
        yield cli_runner, kova_client, cfg, fake, testapp


def test_chats_search_highlights_and_numbers_match_chats_list(runner):
    cli_runner, client, cfg, fake, app = runner
    store = app.state.store
    older = store.create_chat("Zebra notes")
    store.add_message(older["id"], "user", "the zebra crossing is near the needle shop")
    newer = store.create_chat("Apple notes")
    store.add_message(newer["id"], "assistant", "an apple a day keeps the needle away")
    store.create_chat("Unrelated")

    listed = client.list_chats()
    number = next(i for i, chat in enumerate(listed, start=1) if chat["id"] == older["id"])

    result = cli_runner.invoke(cli.app, ["chats", "search", "needle"])
    assert result.exit_code == 0, result.output
    assert "Zebra notes" in result.output and "Apple notes" in result.output
    assert "Unrelated" not in result.output
    assert "needle" in result.output
    assert str(number) in result.output

    shown = cli_runner.invoke(cli.app, ["chats", "show", str(number)])
    assert shown.exit_code == 0, shown.output
    assert "zebra crossing" in shown.output

    empty = cli_runner.invoke(cli.app, ["chats", "search", "nothingmatches"])
    assert empty.exit_code == 0, empty.output
    assert "No chats match" in empty.output


def test_chats_export_prints_markdown_or_json(runner):
    cli_runner, client, cfg, fake, app = runner
    store = app.state.store
    chat = store.create_chat("Exported chat")
    store.add_message(chat["id"], "user", "hello export")
    store.add_message(chat["id"], "assistant", "here is the answer")

    markdown = cli_runner.invoke(cli.app, ["chats", "export", "1"])
    assert markdown.exit_code == 0, markdown.output
    assert "# Exported chat" in markdown.stdout
    assert "## You" in markdown.stdout and "here is the answer" in markdown.stdout

    as_json = cli_runner.invoke(cli.app, ["chats", "export", chat["id"], "--json"])
    assert as_json.exit_code == 0, as_json.output
    data = json.loads(as_json.stdout)
    assert data["id"] == chat["id"]
    assert data["messages"][1]["content"] == "here is the answer"

    both = cli_runner.invoke(cli.app, ["chats", "export", "1", "--md", "--json"])
    assert both.exit_code == 1
    assert "Choose either" in both.output


def test_export_and_import_commands(tmp_path, monkeypatch):
    source_home = tmp_path / "source"
    monkeypatch.setenv("KOVA_HOME", str(source_home))
    cfg = load_config()
    cfg.models[0].api_key = "secret-key"
    save_config(cfg)
    ensure_token(cfg.home)
    store = Store(cfg.home / "kova.db")
    chat = store.create_chat("Command chat")
    store.add_message(chat["id"], "user", "backup me")
    Memory(cfg.memory_path).save("Dog", "Rex")
    store.add_reminder(datetime.now(timezone.utc) + timedelta(days=1), "Buy milk")
    target = tmp_path / "cli-backup.zip"

    exported = CliRunner().invoke(cli.app, ["export", str(target)])
    assert exported.exit_code == 0, exported.output
    assert str(target) in exported.output.replace("\n", "")
    assert "API keys were left out" in exported.output
    with zipfile.ZipFile(target) as archive:
        names = set(archive.namelist())
        assert "token" not in names
        assert "api_key" not in archive.read("config.toml").decode()
        assert f"chats/{chat['id']}.json" in names

    monkeypatch.setenv("KOVA_HOME", str(tmp_path / "restored"))
    imported = CliRunner().invoke(cli.app, ["import", str(target)])
    assert imported.exit_code == 0, imported.output
    assert "Imported 1 chats" in imported.output

    restored = load_config()
    restored_store = Store(restored.home / "kova.db")
    assert [c["title"] for c in restored_store.list_chats()] == ["Command chat"]
    assert [note.title for note in Memory(restored.memory_path).list()] == ["Dog"]
    assert [r["text"] for r in restored_store.list_reminders()] == ["Buy milk"]
    assert not (restored.home / "token").exists()

    # Replace asks first, and --yes skips the question.
    extra = restored_store.create_chat("Extra")
    restored_store.add_message(extra["id"], "user", "extra")
    cancelled = CliRunner().invoke(cli.app, ["import", str(target), "--replace"], input="n\n")
    assert cancelled.exit_code == 0, cancelled.output
    assert "Cancelled" in cancelled.output
    assert len(restored_store.list_chats()) == 2

    replaced = CliRunner().invoke(cli.app, ["import", str(target), "--replace", "--yes"])
    assert replaced.exit_code == 0, replaced.output
    assert [c["title"] for c in restored_store.list_chats()] == ["Command chat"]
