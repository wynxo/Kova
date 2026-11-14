"""Backup export/import: contents, secrets, Obsidian scope and round-trips."""

import json
import zipfile
from datetime import datetime, timedelta, timezone

import pytest

from kova.backup import BackupError, export_archive, import_archive
from kova.config import ensure_token, load_config, save_config
from kova.memory import Memory
from kova.store import Store


def _home(tmp_path, name="home"):
    cfg = load_config(tmp_path / name)
    cfg.models[0].api_key = "top-secret-key"
    save_config(cfg)
    ensure_token(cfg.home)
    return cfg


def _seed(cfg):
    store = Store(cfg.home / "kova.db")
    chat = store.create_chat("Apple pie")
    store.add_message(chat["id"], "user", "How do I bake an apple pie?")
    store.add_message(chat["id"], "assistant", "Use apples, flour and butter.")
    Memory(cfg.memory_path).save("Dog", "Rex is a good dog")
    store.add_reminder(datetime.now(timezone.utc) + timedelta(days=1), "Buy milk")
    return store, chat["id"]


def test_export_contains_chats_notes_reminders_and_safe_config(tmp_path):
    cfg = _home(tmp_path)
    _, chat_id = _seed(cfg)
    target = tmp_path / "backup.zip"

    stats = export_archive(cfg, target)

    assert stats["chats"] == 1 and stats["notes"] == 1 and stats["reminders"] == 1
    with zipfile.ZipFile(target) as archive:
        names = set(archive.namelist())
        assert "token" not in names
        assert f"chats/{chat_id}.json" in names
        assert f"chats/{chat_id}.md" in names
        assert "memory/dog.md" in names
        assert "reminders.json" in names
        assert "config.toml" in names and "manifest.json" in names

        config_text = archive.read("config.toml").decode()
        assert "top-secret-key" not in config_text
        assert "api_key" not in config_text
        assert "local" in config_text

        readme = archive.read("README.txt").decode()
        assert "without API keys" in readme
        assert "token" in readme

        chat = json.loads(archive.read(f"chats/{chat_id}.json"))
        assert chat["title"] == "Apple pie"
        assert [m["role"] for m in chat["messages"]] == ["user", "assistant"]
        assert "created" in chat["messages"][0]

        markdown = archive.read(f"chats/{chat_id}.md").decode()
        assert "# Apple pie" in markdown
        assert "## You" in markdown and "## Kova" in markdown
        assert "Use apples, flour and butter." in markdown

        manifest = json.loads(archive.read("manifest.json"))
        assert manifest["format"] == "kova-backup"
        assert manifest["api_keys"] == "omitted"
        assert manifest["token"] == "omitted"


def test_export_in_obsidian_mode_only_includes_kova_folder(tmp_path):
    cfg = load_config(tmp_path / "home")
    vault = tmp_path / "vault"
    cfg.memory_path = vault
    cfg.memory_write_subdir = "Kova"
    save_config(cfg)
    Memory(vault, "Kova").save("Kova note", "mine")
    (vault / "Personal.md").write_text("# Personal\nnot Kova's\n")

    target = tmp_path / "backup.zip"
    export_archive(cfg, target)

    with zipfile.ZipFile(target) as archive:
        names = set(archive.namelist())
    assert "memory/kova-note.md" in names
    assert "memory/personal.md" not in names
    assert all("Personal" not in name for name in names)


def test_import_round_trip_and_merge_skips_existing(tmp_path):
    source = _home(tmp_path, "source")
    _, chat_id = _seed(source)
    backup = tmp_path / "backup.zip"
    export_archive(source, backup)

    target = load_config(tmp_path / "target")
    assert not (target.home / "token").exists()
    stats = import_archive(target, backup)
    assert (stats["chats"], stats["notes"], stats["reminders"]) == (1, 1, 1)

    store = Store(target.home / "kova.db")
    chat = store.get_chat(chat_id)
    assert chat["title"] == "Apple pie"
    assert [m["content"] for m in chat["messages"]] == [
        "How do I bake an apple pie?",
        "Use apples, flour and butter.",
    ]
    notes = Memory(target.memory_path).list()
    assert [note.slug for note in notes] == ["dog"]
    assert [reminder["text"] for reminder in store.list_reminders()] == ["Buy milk"]
    assert not (target.home / "token").exists()
    assert target.models[0].api_key is None
    assert [result["id"] for result in store.search_chats("apple")] == [chat_id]

    again = import_archive(target, backup)
    assert (again["chats"], again["notes"], again["reminders"]) == (0, 0, 0)
    assert (again["chats_skipped"], again["notes_skipped"], again["reminders_skipped"]) == (1, 1, 1)
    assert len(store.list_chats()) == 1
    assert len(Memory(target.memory_path).list()) == 1
    assert len(store.list_reminders()) == 1


def test_replace_clears_local_data_and_restores_config_keys(tmp_path):
    source = _home(tmp_path, "source")
    _, chat_id = _seed(source)
    backup = tmp_path / "backup.zip"
    export_archive(source, backup)

    target = load_config(tmp_path / "target")
    target.models[0].api_key = "keep-this-local-key"
    save_config(target)
    store = Store(target.home / "kova.db")
    extra = store.create_chat("extra")
    store.add_message(extra["id"], "user", "extra chat")
    Memory(target.memory_path).save("Extra", "delete me")
    store.add_reminder(datetime.now(timezone.utc) + timedelta(days=2), "extra reminder")

    stats = import_archive(target, backup, merge=False)
    assert stats["chats"] == 1 and stats["notes"] == 1 and stats["reminders"] == 1
    assert stats["config_restored"] is True

    assert [c["id"] for c in store.list_chats()] == [chat_id]
    assert [note.slug for note in Memory(target.memory_path).list()] == ["dog"]
    assert [reminder["text"] for reminder in store.list_reminders()] == ["Buy milk"]
    reloaded = load_config(target.home)
    assert reloaded.models[0].api_key == "keep-this-local-key"


def test_import_rejects_a_non_kova_zip(tmp_path):
    path = tmp_path / "random.zip"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("hello.txt", "not a backup")
    with pytest.raises(BackupError):
        import_archive(load_config(tmp_path / "home"), path)


@pytest.mark.skipif(__import__("os").name == "nt", reason="symlink permissions vary on Windows")
def test_backup_skips_symlink_notes_and_replace_preserves_them(tmp_path):
    cfg = _home(tmp_path)
    memory = Memory(cfg.memory_path)
    memory.write_dir.mkdir(parents=True, exist_ok=True)
    outside = tmp_path / "secret.md"
    outside.write_text("do not export")
    link = memory.write_dir / "linked.md"
    link.symlink_to(outside)
    memory.save("Normal", "safe note")
    target = tmp_path / "backup.zip"
    export_archive(cfg, target)
    with zipfile.ZipFile(target) as archive:
        assert "memory/linked.md" not in archive.namelist()
        assert archive.read("memory/normal.md")
    import_archive(cfg, target, merge=False)
    assert link.is_symlink()
    assert outside.read_text() == "do not export"


@pytest.mark.parametrize("change", ["base_url", "adapter", "kind"])
def test_restore_provider_id_collision_does_not_reuse_local_connection(tmp_path, change):
    source = _home(tmp_path, "source")
    source_provider = source.providers[0]
    setattr(source_provider, change, {
        "base_url": "https://different.example/v1",
        "adapter": "anthropic",
        "kind": "anthropic",
    }[change])
    save_config(source)
    backup = tmp_path / "backup.zip"
    export_archive(source, backup)
    target = _home(tmp_path, "target")
    original = target.providers[0]
    original.api_key = "local-private-key"
    original.headers = {"X-Private": "local-header"}
    original.auth = {"access_token": "local-token"}
    save_config(target)
    import_archive(target, backup, merge=False)
    restored = load_config(target.home)
    model = restored.active_model()
    assert model.provider != original.id
    assert getattr(model._provider, change) == getattr(source_provider, change)
    assert model.api_key is None
    assert model._provider.headers == {}
    assert model._provider.auth == {}
    kept = next(p for p in restored.providers if p.id == original.id)
    assert kept.api_key == "local-private-key"
    assert kept.headers == {"X-Private": "local-header"}
    assert kept.auth == {"access_token": "local-token"}


@pytest.mark.skipif(__import__("os").name == "nt", reason="Unix file modes")
def test_export_is_private_and_does_not_follow_predictable_temp_symlink(tmp_path):
    import os
    import stat

    cfg = _home(tmp_path)
    _seed(cfg)
    target = tmp_path / "private.zip"
    victim = tmp_path / "unrelated.txt"
    victim.write_text("keep me")
    target.with_name(target.name + ".tmp").symlink_to(victim)
    previous = os.umask(0o022)
    try:
        export_archive(cfg, target)
    finally:
        os.umask(previous)
    assert victim.read_text() == "keep me"
    assert stat.S_IMODE(target.stat().st_mode) == 0o600


def test_failed_export_keeps_previous_backup_and_removes_temp(tmp_path, monkeypatch):
    cfg = _home(tmp_path)
    target = tmp_path / "backup.zip"
    target.write_bytes(b"previous backup")
    def fail(*args, **kwargs):
        raise OSError("disk full")
    monkeypatch.setattr(zipfile.ZipFile, "writestr", fail)
    with pytest.raises(OSError, match="disk full"):
        export_archive(cfg, target)
    assert target.read_bytes() == b"previous backup"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["backup.zip", "home"]
