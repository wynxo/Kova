"""Backup export/import: contents, secrets, Obsidian scope and round-trips."""

import json
import os
import stat
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from kova import backup as backup_module
from kova import bots as bot_store
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


def _seed_project(cfg, *, folder=None, name="Research"):
    """A project with instructions, a look, a default model and one grouped chat."""
    store = Store(cfg.home / "kova.db")
    folder = folder if folder is not None else cfg.home / "project"
    folder.mkdir(parents=True, exist_ok=True)
    project = store.create_project(name, "Cite sources.", str(folder),
                                   colour="info", icon="brain", model="local")
    chat = store.create_chat(f"{name} chat", project_id=project["id"])
    store.add_message(chat["id"], "user", "Start the paper.")
    return store, project, chat["id"]


def _rewrite_archive(source, target, *, drop=(), replace=None):
    """Copy a backup, dropping members and swapping members for raw bytes."""
    replace = replace or {}
    with zipfile.ZipFile(source) as src, zipfile.ZipFile(target, "w") as dst:
        for item in src.infolist():
            if item.filename in drop:
                continue
            data = replace.get(item.filename, src.read(item.filename))
            dst.writestr(item, data)


def test_round_trip_keeps_a_projects_definition_and_its_chats(tmp_path, monkeypatch):
    """Projects and chat membership survive a backup (#196)."""
    monkeypatch.setattr(backup_module, "_home_folder", lambda: tmp_path)
    source = _home(tmp_path, "source")
    store, project, chat_id = _seed_project(source)
    target = tmp_path / "backup.zip"

    stats = export_archive(source, target, store=store)
    assert stats["projects"] == 1
    with zipfile.ZipFile(target) as archive:
        payload = json.loads(archive.read("projects.json"))
        assert payload == [{
            "id": project["id"], "name": "Research",
            "instructions": "Cite sources.", "cwd": "~/source/project",
            "created": project["created"], "updated": project["updated"],
            "colour": "info", "icon": "brain", "model": "local", "files": [],
            "memory_scope": False,
        }]
        assert json.loads(archive.read("manifest.json"))["projects"] == 1
        assert json.loads(archive.read(f"chats/{chat_id}.json"))["project_id"] == project["id"]
        assert "1 projects" in archive.read("README.txt").decode()

    target_cfg = _home(tmp_path, "target")
    restored_stats = import_archive(target_cfg, target)
    assert (restored_stats["projects"], restored_stats["chats"]) == (1, 1)
    assert restored_stats["projects_skipped"] == 0

    restored = Store(target_cfg.home / "kova.db")
    assert restored.get_project(project["id"]) == project
    assert restored.get_chat(chat_id)["project_id"] == project["id"]

    again = import_archive(target_cfg, target)
    assert (again["projects"], again["projects_skipped"]) == (0, 1)
    assert len(restored.list_projects()) == 1


def test_merge_skips_a_project_renamed_since_the_backup(tmp_path):
    """Same id and creation time is the same project, even after a local rename."""
    source = _home(tmp_path, "source")
    store, project, _ = _seed_project(source)
    target = tmp_path / "backup.zip"
    export_archive(source, target, store=store)

    target_cfg = _home(tmp_path, "target")
    import_archive(target_cfg, target)
    restored = Store(target_cfg.home / "kova.db")
    restored.update_project(project["id"], name="Renamed locally", instructions="Mine now.")

    stats = import_archive(target_cfg, target)
    assert (stats["projects"], stats["projects_skipped"], stats["projects_remapped"]) == (0, 1, 0)
    saved = restored.get_project(project["id"])
    assert (saved["name"], saved["instructions"]) == ("Renamed locally", "Mine now.")


def test_merge_imports_a_reused_project_id_under_a_new_id(tmp_path):
    """An unrelated local project keeps its id; the archive's is remapped."""
    source = _home(tmp_path, "source")
    store, project, chat_id = _seed_project(source)
    target = tmp_path / "backup.zip"
    export_archive(source, target, store=store)

    target_cfg = _home(tmp_path, "target")
    target_store = Store(target_cfg.home / "kova.db")
    local = target_store.create_project("Local", "Keep me.")
    target_store._conn.execute("UPDATE projects SET id = ? WHERE id = ?",
                               (project["id"], local["id"]))
    target_store._conn.commit()

    stats = import_archive(target_cfg, target)
    assert (stats["projects"], stats["projects_remapped"]) == (1, 1)
    kept = target_store.get_project(project["id"])
    assert (kept["name"], kept["instructions"]) == ("Local", "Keep me.")
    restored_chat = target_store.get_chat(chat_id)
    assert restored_chat["project_id"] not in (None, project["id"])
    moved = target_store.get_project(restored_chat["project_id"])
    assert (moved["name"], moved["instructions"]) == ("Research", "Cite sources.")


def test_restore_projects_keeps_first_duplicate_source_id(tmp_path):
    store = Store(tmp_path / "kova.db")
    first = store.create_project("Original", "Keep me.")
    duplicate = {**first, "name": "Duplicate", "created": "2000-01-01T00:00:00Z"}
    mapping, counts = backup_module._restore_projects(store, [first, duplicate])
    assert mapping[first["id"]] == first["id"]
    assert counts == {"imported": 0, "skipped": 2, "remapped": 0}
    assert len(store.list_projects()) == 1


def test_replace_drops_local_projects_and_restores_the_archives(tmp_path):
    source = _home(tmp_path, "source")
    store, project, chat_id = _seed_project(source)
    target = tmp_path / "backup.zip"
    export_archive(source, target, store=store)

    target_cfg = _home(tmp_path, "target")
    target_store = Store(target_cfg.home / "kova.db")
    target_store.create_project("Local only", "Delete me.")

    stats = import_archive(target_cfg, target, merge=False)
    assert stats["projects"] == 1
    assert [row["id"] for row in target_store.list_projects()] == [project["id"]]
    assert target_store.get_chat(chat_id)["project_id"] == project["id"]


def test_an_older_archive_imports_its_chats_ungrouped(tmp_path):
    """Backups written before projects existed still restore, without links."""
    source = _home(tmp_path, "source")
    store, project, chat_id = _seed_project(source)
    target = tmp_path / "backup.zip"
    export_archive(source, target, store=store)
    legacy = tmp_path / "legacy.zip"
    _rewrite_archive(target, legacy, drop=("projects.json",))

    target_cfg = _home(tmp_path, "target")
    target_store = Store(target_cfg.home / "kova.db")
    target_store.create_project("Local only", "Delete me with replace.")
    stats = import_archive(target_cfg, legacy)
    assert stats["projects"] == 0
    assert target_store.get_chat(chat_id)["project_id"] is None
    assert [row["name"] for row in target_store.list_projects()] == ["Local only"]

    replacing = _home(tmp_path, "replacement")
    imported = import_archive(replacing, legacy, merge=False)
    assert (imported["projects"], imported["chats"]) == (0, 1)
    assert Store(replacing.home / "kova.db").list_projects() == []


def test_project_folder_below_home_is_written_without_the_account_name(tmp_path, monkeypatch):
    """The archive stores ``~/...`` and expands it on the restoring machine."""
    home = tmp_path / "home"
    folder = home / "projects" / "demo"
    folder.mkdir(parents=True)
    cfg = _home(tmp_path, "source")
    store = Store(cfg.home / "kova.db")
    project = store.create_project("Demo", "Shared notes.", str(folder))
    monkeypatch.setattr(backup_module, "_home_folder", lambda: home)
    target = tmp_path / "backup.zip"
    export_archive(cfg, target, store=store)

    with zipfile.ZipFile(target) as archive:
        text = archive.read("projects.json").decode()
        assert json.loads(text)[0]["cwd"] == "~/projects/demo"
        assert str(home) not in text

    target_cfg = _home(tmp_path, "target")
    import_archive(target_cfg, target)
    restored = Store(target_cfg.home / "kova.db").get_project(project["id"])
    assert restored["cwd"] == str(folder.resolve())


def test_project_folder_missing_here_is_dropped_not_created(tmp_path, monkeypatch):
    """A folder from another machine never becomes a broken default."""
    home = tmp_path / "machine-a"
    folder = home / "projects" / "demo"
    folder.mkdir(parents=True)
    cfg = _home(tmp_path, "source")
    store, project, chat_id = _seed_project(cfg, folder=folder)
    monkeypatch.setattr(backup_module, "_home_folder", lambda: home)
    target = tmp_path / "backup.zip"
    export_archive(cfg, target, store=store)

    monkeypatch.setattr(backup_module, "_home_folder", lambda: tmp_path / "machine-b")
    target_cfg = _home(tmp_path, "target")
    import_archive(target_cfg, target)

    restored = Store(target_cfg.home / "kova.db")
    assert restored.get_project(project["id"])["cwd"] is None
    assert not (tmp_path / "machine-b" / "projects" / "demo").exists()
    # The chat keeps the folder it saved at the time; only the default is gone.
    assert restored.get_chat(chat_id)["cwd"] == str(folder)


@pytest.mark.parametrize("payload", [b"{not json", b'{"id": "p"}'])
def test_an_unreadable_projects_json_is_rejected(tmp_path, payload):
    source = _home(tmp_path, "source")
    _seed(source)
    target = tmp_path / "backup.zip"
    export_archive(source, target)
    broken = tmp_path / "broken.zip"
    _rewrite_archive(target, broken, replace={"projects.json": payload})
    with pytest.raises(BackupError, match="projects.json"):
        import_archive(_home(tmp_path, "target"), broken)


def test_junk_project_entries_are_skipped_but_the_rest_restores(tmp_path):
    source = _home(tmp_path, "source")
    _seed(source)
    store = Store(source.home / "kova.db")
    project = store.create_project("Kept", "Still here.")
    chat = store.create_chat("Grouped", project_id=project["id"])
    target = tmp_path / "backup.zip"
    export_archive(source, target, store=store)

    payload = [None, 7, {"name": ""}, {"id": "other", "name": "Kept", "instructions": "Still here."}]
    edited = tmp_path / "edited.zip"
    _rewrite_archive(target, edited, replace={"projects.json": json.dumps(payload).encode()})
    target_cfg = _home(tmp_path, "target")
    stats = import_archive(target_cfg, edited)
    # The real project is missing from this archive, so its chat is ungrouped.
    assert (stats["projects"], stats["projects_skipped"]) == (1, 3)
    restored = Store(target_cfg.home / "kova.db")
    assert [row["name"] for row in restored.list_projects()] == ["Kept"]
    assert restored.get_chat(chat["id"])["project_id"] is None


def test_pinned_files_travel_as_references_only(tmp_path, monkeypatch):
    """Pins round-trip with their project, but the files themselves do not."""
    monkeypatch.setattr(backup_module, "_home_folder", lambda: tmp_path)
    source = _home(tmp_path, "source")
    store, project, _ = _seed_project(source)
    (Path(project["cwd"]) / "notes").mkdir()
    (Path(project["cwd"]) / "notes" / "a.md").write_text("PRIVATE NOTE BODY\n", encoding="utf-8")
    store.update_project(project["id"], files=["notes/a.md"])
    target = tmp_path / "backup.zip"
    export_archive(source, target, store=store)

    with zipfile.ZipFile(target) as archive:
        payload = json.loads(archive.read("projects.json"))
        assert payload[0]["files"] == ["notes/a.md"]
        assert all("PRIVATE NOTE BODY" not in archive.read(name).decode("utf-8", "ignore")
                   for name in archive.namelist())

    target_cfg = _home(tmp_path, "target")
    import_archive(target_cfg, target)
    assert Store(target_cfg.home / "kova.db").get_project(project["id"])["files"] == ["notes/a.md"]


def test_pins_are_dropped_when_the_folder_does_not_exist_here(tmp_path, monkeypatch):
    """A reference with no base folder would mean nothing, so the restore drops it."""
    home = tmp_path / "machine-a"
    folder = home / "projects" / "demo"
    folder.mkdir(parents=True)
    source = _home(tmp_path, "source")
    store, project, _ = _seed_project(source, folder=folder)
    store.update_project(project["id"], files=["notes/a.md"])
    monkeypatch.setattr(backup_module, "_home_folder", lambda: home)
    target = tmp_path / "backup.zip"
    export_archive(source, target, store=store)

    monkeypatch.setattr(backup_module, "_home_folder", lambda: tmp_path / "machine-b")
    target_cfg = _home(tmp_path, "target")
    import_archive(target_cfg, target)
    restored = Store(target_cfg.home / "kova.db").get_project(project["id"])
    assert restored["cwd"] is None and restored["files"] == []


def test_an_archive_without_pinned_files_restores_the_project(tmp_path, monkeypatch):
    """projects.json written before pins existed keeps its project, with no pins."""
    monkeypatch.setattr(backup_module, "_home_folder", lambda: tmp_path)
    source = _home(tmp_path, "source")
    store, project, _ = _seed_project(source)
    target = tmp_path / "backup.zip"
    export_archive(source, target, store=store)
    payload = json.loads(zipfile.ZipFile(target).read("projects.json"))
    for entry in payload:
        entry.pop("files", None)
    older = tmp_path / "older.zip"
    _rewrite_archive(target, older, replace={"projects.json": json.dumps(payload).encode()})

    target_cfg = _home(tmp_path, "target")
    assert import_archive(target_cfg, older)["projects"] == 1
    restored = Store(target_cfg.home / "kova.db").get_project(project["id"])
    assert restored["name"] == "Research" and restored["files"] == []


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


def test_backup_keeps_archived_alternatives_but_reads_the_active_one(tmp_path):
    source = _home(tmp_path, "source")
    store, chat_id = _seed(source)
    before = store.get_chat(chat_id)["messages"]
    turn_id, variant = store.start_alternative(chat_id)
    assert store.get_chat(chat_id)["messages"] == before
    store.add_message(chat_id, "user", "Try again", turn_id=turn_id, variant=variant)
    store.add_message(chat_id, "assistant", "Second answer", turn_id=turn_id, variant=variant)
    backup = tmp_path / "backup.zip"
    export_archive(source, backup, store=store)

    target = load_config(tmp_path / "target")
    import_archive(target, backup)
    restored = Store(target.home / "kova.db")
    chat = restored.get_chat(chat_id, include_inactive=True)
    assert [(m["content"], m["active"]) for m in chat["messages"]] == [
        ("How do I bake an apple pie?", False),
        ("Use apples, flour and butter.", False),
        ("Try again", True),
        ("Second answer", True),
    ]
    assert [m["content"] for m in restored.get_chat(chat_id)["messages"]] == [
        "Try again", "Second answer",
    ]
    with zipfile.ZipFile(backup) as archive:
        markdown = archive.read(f"chats/{chat_id}.md").decode()
    assert "Try again" in markdown and "Second answer" in markdown
    assert "How do I bake an apple pie?" not in markdown


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


def test_round_trip_keeps_a_chats_pin_colour_and_icon(tmp_path):
    """Appearance and the pin are cosmetic, but a restore keeps them (#213, #195)."""
    source = _home(tmp_path, "source")
    store, chat_id = _seed(source)
    store.update_chat(chat_id, pinned=True, colour="warning", icon="sun")
    backup = tmp_path / "backup.zip"
    export_archive(source, backup)

    target = load_config(tmp_path / "target")
    import_archive(target, backup)

    restored = Store(target.home / "kova.db").get_chat(chat_id)
    assert (restored["pinned"], restored["colour"], restored["icon"]) == (True, "warning", "sun")


def test_import_restores_message_attachment_names(tmp_path):
    """A reloaded transcript keeps the file label, not the extracted document text."""
    source = _home(tmp_path, "source")
    store = Store(source.home / "kova.db")
    chat = store.create_chat("Notes")
    message_id = store.add_message(chat["id"], "user", "")
    store.link_message_attachments(message_id, [{"id": "attachment-1", "name": "notes.txt"}])
    backup = tmp_path / "backup.zip"
    export_archive(source, backup, store=store)

    with zipfile.ZipFile(backup) as archive:
        payload = json.loads(archive.read(f"chats/{chat['id']}.json"))
        assert payload["messages"][0]["attachments"] == [{"id": "attachment-1", "name": "notes.txt"}]
        assert "_Attached: notes.txt_" in archive.read(f"chats/{chat['id']}.md").decode()

    target = load_config(tmp_path / "target")
    import_archive(target, backup)
    restored = Store(target.home / "kova.db").get_chat(chat["id"])
    assert restored["messages"][0]["content"] == ""
    assert restored["messages"][0]["attachments"] == [{"id": "attachment-1", "name": "notes.txt"}]


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


def _seed_bot(cfg, bot_id="researcher", persona="You research questions on the web.", avatar=False):
    """One saved bot, optionally with an avatar image and a memory note."""
    definition = bot_store.BotDefinition(id=bot_id, name=bot_id.replace("-", " ").title(),
                                         handle=bot_id)
    saved = bot_store.save_bot(cfg.home, definition, persona=persona)
    if avatar:
        saved = bot_store.save_avatar(saved, b"\x89PNG\r\n\x1a\n" + b"0" * 64, "image/png")
    return saved


def test_export_contains_bots_persona_avatar_and_memory(tmp_path):
    cfg = _home(tmp_path)
    _seed_bot(cfg, avatar=True)
    Memory(cfg.memory_path, cfg.memory_write_subdir).bot_scope("researcher").save(
        "Topic", "researcher remembers this")
    target = tmp_path / "backup.zip"

    stats = export_archive(cfg, target)

    assert stats["bots"] == 1 and stats["bot_notes"] == 1
    with zipfile.ZipFile(target) as archive:
        names = set(archive.namelist())
        assert "bots/researcher/bot.toml" in names
        assert "bots/researcher/persona.md" in names
        assert "bots/researcher/avatar.png" in names
        assert "bot-memory/researcher/topic.md" in names
        assert archive.read("bots/researcher/persona.md").decode() == \
            "You research questions on the web."
        assert archive.read("bots/researcher/avatar.png") == \
            (cfg.home / "bots" / "researcher" / "avatar.png").read_bytes()
        manifest = json.loads(archive.read("manifest.json"))
        assert manifest["bots"] == 1 and manifest["bot_notes"] == 1
        assert "bots" in archive.read("README.txt").decode()


def test_import_into_empty_home_restores_bots(tmp_path):
    source = _home(tmp_path, "source")
    _seed_bot(source, avatar=True)
    Memory(source.memory_path, source.memory_write_subdir).bot_scope("researcher").save(
        "Topic", "researcher remembers this")
    backup = tmp_path / "backup.zip"
    export_archive(source, backup)

    target = _home(tmp_path, "target")
    stats = import_archive(target, backup)
    assert (stats["bots"], stats["bot_notes"]) == (1, 1)

    restored = bot_store.get_bot(target.home, "researcher")
    assert restored is not None and restored.handle == "researcher"
    assert (target.home / "bots" / "researcher" / "persona.md").read_text() == \
        "You research questions on the web."
    assert (target.home / "bots" / "researcher" / "avatar.png").read_bytes() == \
        (source.home / "bots" / "researcher" / "avatar.png").read_bytes()
    notes = Memory(target.memory_path, target.memory_write_subdir).bot_scope("researcher").list()
    assert [note.slug for note in notes] == ["topic"]
    if os.name != "nt":
        assert stat.S_IMODE((target.home / "bots" / "researcher" / "bot.toml").stat().st_mode) == 0o600
        assert stat.S_IMODE((target.home / "bots" / "researcher" / "persona.md").stat().st_mode) == 0o600

    again = import_archive(target, backup)
    assert (again["bots"], again["bots_skipped"]) == (0, 1)
    assert (again["bot_notes"], again["bot_notes_skipped"]) == (0, 1)


def test_merge_keeps_local_bot_unless_the_backup_is_newer(tmp_path):
    import time

    source = _home(tmp_path, "source")
    _seed_bot(source, persona="New persona from the backup.")
    backup = tmp_path / "backup.zip"
    export_archive(source, backup)

    target = _home(tmp_path, "target")
    _seed_bot(target, persona="Local persona wins.")
    local_toml = target.home / "bots" / "researcher" / "bot.toml"
    # The local file is newer than the backup, so the merge keeps it.
    newer = time.time() + 3600
    os.utime(local_toml, (newer, newer))
    kept = import_archive(target, backup)
    assert (kept["bots"], kept["bots_skipped"]) == (0, 1)
    assert (target.home / "bots" / "researcher" / "persona.md").read_text() == "Local persona wins."

    # The backup is newer than the local file, so it replaces it.
    older = time.time() - 86400 * 30
    os.utime(local_toml, (older, older))
    replaced = import_archive(target, backup)
    assert (replaced["bots"], replaced["bots_skipped"]) == (1, 0)
    assert (target.home / "bots" / "researcher" / "persona.md").read_text() == \
        "New persona from the backup."


def _rewrite_with_extra_member(source, target, member, data=b"evil", symlink=False):
    with zipfile.ZipFile(source) as src, zipfile.ZipFile(target, "w") as dst:
        for item in src.infolist():
            dst.writestr(item, src.read(item.filename))
        if symlink:
            info = zipfile.ZipInfo(member)
            info.create_system = 3
            info.external_attr = (0o120777 << 16)
            dst.writestr(info, data)
        else:
            dst.writestr(member, data)


@pytest.mark.parametrize("member,symlink", [
    ("bots/../evil.txt", False),
    ("bots/researcher/../../evil.txt", False),
    ("bot-memory/../evil.md", False),
    ("bots/researcher/evil.png", True),
])
def test_import_refuses_malicious_bot_paths(tmp_path, member, symlink):
    source = _home(tmp_path, "source")
    _seed_bot(source)
    backup = tmp_path / "backup.zip"
    export_archive(source, backup)
    booby_trapped = tmp_path / "evil.zip"
    _rewrite_with_extra_member(backup, booby_trapped, member, symlink=symlink)

    target = _home(tmp_path, "target")
    with pytest.raises(BackupError):
        import_archive(target, booby_trapped)
    assert not (target.home / "evil.txt").exists()
    assert not (tmp_path / "evil.txt").exists()


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
