"""Projects (#196): colour, icon, a default model, pinned files and chat moves."""
from __future__ import annotations

import sqlite3

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from kova import cli
from kova.client import KovaClient
from kova.config import ModelEntry, ensure_token, load_config
from kova.server import create_app
from kova.store import Store
from tests.fake_model import FakeModel, text


@pytest.fixture
def server(tmp_path, monkeypatch):
    monkeypatch.setenv("KOVA_HOME", str(tmp_path))
    cfg = load_config()
    cfg.models[0].model = "demo-local"
    cfg.models.append(ModelEntry("second", "http://127.0.0.1:9/v1", "demo-second"))
    fake = FakeModel([text("One"), text("Two"), text("Three")])
    headers = {"Authorization": f"Bearer {ensure_token(cfg.home)}"}
    with TestClient(create_app(cfg, transport=fake.transport)) as client:
        yield client, headers, cfg, fake


# ------------------------------------------------------------------- store
def test_older_project_tables_gain_colour_icon_model_and_pinned_files(tmp_path):
    path = tmp_path / "kova.db"
    old = Store(path)
    project = old.create_project("Research", "Cite sources.")
    old._conn.close()
    with sqlite3.connect(path) as db:
        for column in ("colour", "icon", "model", "files"):
            db.execute(f"ALTER TABLE projects DROP COLUMN {column}")
    migrated = Store(path)
    saved = migrated.get_project(project["id"])
    assert (saved["name"], saved["instructions"]) == ("Research", "Cite sources.")
    assert (saved["colour"], saved["icon"], saved["model"]) == (None, None, None)
    assert saved["files"] == []
    updated = migrated.update_project(project["id"], colour="info", icon="brain", model="second",
                                      files=["notes/a.md"])
    assert (updated["colour"], updated["icon"], updated["model"]) == ("info", "brain", "second")
    assert updated["files"] == ["notes/a.md"]
    cleared = migrated.update_project(project["id"], colour=None, model="", files=[])
    assert (cleared["colour"], cleared["icon"], cleared["model"]) == (None, "brain", None)
    assert cleared["files"] == []


def test_store_moves_chats_without_touching_their_folder(tmp_path):
    store = Store(tmp_path / "kova.db")
    project = store.create_project("Research", cwd=str(tmp_path / "project"))
    chat = store.create_chat("Loose", cwd=str(tmp_path / "own"))
    assert store.move_chat(chat["id"], project["id"])
    saved = store.get_chat(chat["id"])
    assert saved["project_id"] == project["id"] and saved["cwd"] == str(tmp_path / "own")
    assert store.move_chat(chat["id"], None)
    assert store.get_chat(chat["id"])["project_id"] is None
    assert not store.move_chat("missing", None)
    with pytest.raises(LookupError):
        store.move_chat(chat["id"], "missing")
    incognito = store.create_chat(ephemeral=True)
    with pytest.raises(ValueError):
        store.move_chat(incognito["id"], project["id"])


def test_store_imports_a_project_and_chat_membership(tmp_path):
    """A restore reuses the same import path as chats, keeping ids and links."""
    store = Store(tmp_path / "kova.db")
    saved = {
        "id": "project-1", "name": "Research", "instructions": "Cite sources.",
        "cwd": str(tmp_path), "created": "2026-01-01T00:00:00+00:00",
        "updated": "2026-01-02T00:00:00+00:00", "colour": "warning",
        "icon": "sun", "model": "local", "files": ["notes/b.md", "notes/a.md"],
    }
    assert store.import_project(saved) is True
    assert store.import_project(saved) is False
    assert store.get_project(saved["id"]) == saved

    assert store.import_chat({"id": "chat-one", "title": "Grouped",
                              "project_id": saved["id"], "messages": []})
    assert store.get_chat("chat-one")["project_id"] == saved["id"]
    # A link with no project here is dropped instead of failing the foreign key.
    assert store.import_chat({"id": "chat-two", "project_id": "missing", "messages": []})
    assert store.get_chat("chat-two")["project_id"] is None
    assert store.import_chat({"id": "chat-three", "project_id": 7, "messages": []})
    assert store.get_chat("chat-three")["project_id"] is None


def test_store_import_keeps_a_hand_edited_project_readable(tmp_path):
    """Unknown look and missing fields fall back instead of failing a restore."""
    store = Store(tmp_path / "kova.db")
    assert store.import_project({"id": "p", "name": "  Notes  ", "colour": "#ff0000"})
    saved = store.get_project("p")
    assert (saved["name"], saved["instructions"], saved["cwd"]) == ("Notes", "", None)
    assert (saved["colour"], saved["icon"], saved["model"]) == (None, None, None)
    assert saved["files"] == []
    assert saved["created"] and saved["updated"]
    with pytest.raises(ValueError):
        store.import_project({"id": "q", "name": "   "})


def test_store_import_salvages_pinned_paths_only_with_a_folder(tmp_path):
    """A damaged archive narrows the pins instead of failing the restore."""
    store = Store(tmp_path / "kova.db")
    assert store.import_project({
        "id": "pinned", "name": "Pinned", "cwd": str(tmp_path),
        "files": ["notes/a.md", "/etc/passwd", "../out.md", 7, "notes/a.md"],
    })
    assert store.get_project("pinned")["files"] == ["notes/a.md"]
    # Without a restored folder the references have no base, so they are dropped.
    assert store.import_project({"id": "lost", "name": "Lost", "files": ["notes/a.md"]})
    assert store.get_project("lost")["files"] == []
    assert store.import_project({"id": "odd", "name": "Odd", "cwd": str(tmp_path), "files": "a.md"})
    assert store.get_project("odd")["files"] == []


# ------------------------------------------------------------------ server
def test_project_colour_icon_and_model_are_validated(server):
    client, headers, _, _ = server
    for body in ({"name": "X", "colour": "#ff0000"}, {"name": "X", "icon": "rocket"}):
        assert client.post("/projects", headers=headers, json=body).status_code == 422
    missing = client.post("/projects", headers=headers, json={"name": "X", "model": "nowhere"})
    assert missing.status_code == 404 and "not configured" in missing.json()["detail"]
    project = client.post("/projects", headers=headers, json={
        "name": "Research", "colour": "Success", "icon": "brain", "model": "second",
    }).json()
    assert (project["colour"], project["icon"], project["model"]) == ("success", "brain", "second")
    path = f"/projects/{project['id']}"
    cleared = client.patch(path, headers=headers, json={"colour": None, "model": ""}).json()
    assert (cleared["colour"], cleared["icon"], cleared["model"]) == (None, "brain", None)
    assert client.patch(path, headers=headers, json={"model": "nowhere"}).status_code == 404
    assert client.get(path, headers=headers).json()["model"] is None


def test_new_project_chats_inherit_the_default_model(server):
    client, headers, cfg, _ = server
    project = client.post("/projects", headers=headers, json={"name": "R", "model": "second"}).json()
    chat = client.post("/chats", headers=headers, json={"project_id": project["id"]}).json()
    assert chat["model"] == "second"
    explicit = client.post("/chats", headers=headers,
                           json={"project_id": project["id"], "model": "local"}).json()
    assert explicit["model"] == "local"
    loose = client.post("/chats", headers=headers, json={}).json()
    assert loose["model"] is None
    # A default whose model was removed later is skipped rather than failing the chat.
    cfg.models[:] = [entry for entry in cfg.models if entry.name != "second"]
    fallback = client.post("/chats", headers=headers, json={"project_id": project["id"]})
    assert fallback.status_code == 200 and fallback.json()["model"] is None


def test_chats_move_in_and_out_of_projects(server, tmp_path):
    client, headers, _, fake = server
    folder = tmp_path / "project"
    folder.mkdir()
    project = client.post("/projects", headers=headers, json={
        "name": "Research", "instructions": "Project rule: use metric units.", "cwd": str(folder),
    }).json()
    chat = client.post("/chats", headers=headers, json={}).json()
    path = f"/chats/{chat['id']}"

    moved = client.patch(path, headers=headers, json={"project_id": project["id"]})
    assert moved.status_code == 200, moved.text
    assert moved.json() == {"id": chat["id"], "project_id": project["id"]}
    saved = client.get(path, headers=headers).json()
    assert saved["project_id"] == project["id"] and saved["cwd"] == chat["cwd"]
    reply = client.post(f"{path}/messages", headers=headers, json={"text": "Hello"})
    assert reply.status_code == 200
    assert "Project rule: use metric units." in fake.requests[-1]["messages"][0]["content"]

    assert client.patch(path, headers=headers, json={"project_id": None}).json()["project_id"] is None
    assert client.get(path, headers=headers).json()["project_id"] is None

    assert client.patch(path, headers=headers, json={"project_id": "missing"}).status_code == 404
    assert client.patch("/chats/missing", headers=headers, json={"project_id": None}).status_code == 404
    both = client.patch(path, headers=headers, json={"project_id": project["id"], "title": "X"})
    assert both.status_code == 422
    incognito = client.post("/chats", headers=headers, json={"ephemeral": True}).json()
    refused = client.patch(f"/chats/{incognito['id']}", headers=headers,
                           json={"project_id": project["id"]})
    assert refused.status_code == 422


def test_project_pins_are_gated_and_only_reach_work_prompts(server, tmp_path):
    client, headers, _, fake = server
    folder = tmp_path / "project"
    folder.mkdir()
    elsewhere = tmp_path / "outside.md"
    elsewhere.write_text("OUTSIDE THE PROJECT", encoding="utf-8")

    # A reference needs a base folder and must stay inside it.
    no_folder = client.post("/projects", headers=headers, json={"name": "X", "files": ["notes.md"]})
    assert no_folder.status_code == 422 and "working folder" in no_folder.json()["detail"]
    escape = client.post("/projects", headers=headers, json={
        "name": "X", "cwd": str(folder), "files": ["../outside.md"],
    })
    assert escape.status_code == 422
    absolute = client.post("/projects", headers=headers, json={
        "name": "X", "cwd": str(folder), "files": [str(elsewhere)],
    })
    assert absolute.status_code == 422

    project = client.post("/projects", headers=headers, json={
        "name": "Pinned", "cwd": str(folder), "files": ["notes.md", "./notes.md"],
    }).json()
    assert project["files"] == ["notes.md"]
    path = f"/projects/{project['id']}"
    assert client.patch(path, headers=headers, json={"files": ["notes.md", "src/app.py"]}).json()["files"] == [
        "notes.md", "src/app.py"]
    assert client.patch(path, headers=headers, json={"files": ["/etc/passwd"]}).status_code == 422
    # Changing or clearing the base without a new list would silently retarget them.
    assert client.patch(path, headers=headers, json={"cwd": None}).status_code == 422
    other = tmp_path / "other"
    other.mkdir()
    assert client.patch(path, headers=headers, json={"cwd": str(other)}).status_code == 422
    assert client.patch(path, headers=headers, json={"cwd": str(other), "files": []}).json()["cwd"] == str(other)

    (folder / "notes.md").write_text("PROJECT FILE CONTENTS\n", encoding="utf-8")
    client.patch(path, headers=headers, json={"cwd": str(folder), "files": ["notes.md"]})
    work = client.post("/chats", headers=headers, json={"project_id": project["id"]}).json()
    chat_mode = client.post("/chats", headers=headers,
                            json={"project_id": project["id"], "mode": "chat"}).json()
    loose = client.post("/chats", headers=headers, json={"cwd": str(folder)}).json()

    def prompt(chat):
        result = client.post(f"/chats/{chat['id']}/messages", headers=headers, json={"text": "Hello"})
        assert result.status_code == 200 and '"type": "done"' in result.text
        return fake.requests[-1]["messages"][0]["content"]

    work_prompt = prompt(work)
    assert "Pinned project files" in work_prompt
    assert "notes.md" in work_prompt and str(folder) in work_prompt
    assert "PROJECT FILE CONTENTS" not in work_prompt, "contents are never included"
    assert "Pinned project files" not in prompt(chat_mode), "Chat mode has no file tools"
    assert "Pinned project files" not in prompt(loose)
    assert "PROJECT FILE CONTENTS" not in fake.requests[-1]["messages"][0]["content"]


# --------------------------------------------------------------------- CLI
@pytest.fixture
def runner(server, monkeypatch):
    client, headers, cfg, fake = server
    kova = KovaClient("http://127.0.0.1:7700", ensure_token(cfg.home), http=client)
    monkeypatch.setattr(cli, "get_client", lambda: kova)
    return CliRunner(), kova


def test_cli_pins_and_unpins_project_files(runner, tmp_path):
    cli_runner, kova = runner
    needs_folder = cli_runner.invoke(cli.app, ["projects", "create", "Pinned", "--pin", "notes/a.md"])
    assert needs_folder.exit_code == 1 and "--folder" in needs_folder.output

    created = cli_runner.invoke(cli.app, [
        "projects", "create", "Pinned", "--folder", str(tmp_path),
        "--pin", "notes/a.md", "--pin", "src/b.py",
    ])
    assert created.exit_code == 0, created.output
    [project] = kova.list_projects()
    assert project["files"] == ["notes/a.md", "src/b.py"]
    shown = cli_runner.invoke(cli.app, ["projects", "show", project["id"]])
    assert "Pinned files (references only)" in shown.output
    assert "notes/a.md" in shown.output.replace("\n", "")

    edited = cli_runner.invoke(cli.app, [
        "projects", "edit", project["id"], "--pin", "notes/c.md", "--unpin", "notes/a.md",
    ])
    assert edited.exit_code == 0, edited.output
    assert kova.get_project(project["id"])["files"] == ["src/b.py", "notes/c.md"]

    invalid = cli_runner.invoke(cli.app, ["projects", "edit", project["id"], "--pin", "../out.md"])
    assert invalid.exit_code == 1 and "inside the project folder" in invalid.output
    cleared = cli_runner.invoke(cli.app, ["projects", "edit", project["id"], "--clear-pins"])
    assert cleared.exit_code == 0, cleared.output
    assert kova.get_project(project["id"])["files"] == []


def test_cli_moves_chats_and_edits_projects(runner):
    cli_runner, kova = runner
    created = cli_runner.invoke(cli.app, ["projects", "create", "Research", "--colour", "info",
                                          "--icon", "brain", "--model", "second"])
    assert created.exit_code == 0, created.output
    [project] = kova.list_projects()
    assert (project["colour"], project["icon"], project["model"]) == ("info", "brain", "second")
    shown = cli_runner.invoke(cli.app, ["projects", "show", project["id"]])
    assert "Default model: second" in shown.output and "info · brain" in shown.output

    edited = cli_runner.invoke(cli.app, ["projects", "edit", project["id"], "--name", "Lab",
                                         "--colour", "none", "--model", "none"])
    assert edited.exit_code == 0, edited.output
    saved = kova.get_project(project["id"])
    assert (saved["name"], saved["colour"], saved["icon"], saved["model"]) == ("Lab", None, "brain", None)
    nothing = cli_runner.invoke(cli.app, ["projects", "edit", project["id"]])
    assert nothing.exit_code == 1 and "Nothing to change" in nothing.output

    chat = kova.create_chat(title="Loose notes")
    moved = cli_runner.invoke(cli.app, ["chats", "move", chat["id"], project["id"]])
    assert moved.exit_code == 0, moved.output
    assert 'Moved "Loose notes" to project "Lab"' in moved.output
    assert kova.get_chat(chat["id"])["project_id"] == project["id"]
    out = cli_runner.invoke(cli.app, ["chats", "move", chat["id"], "none"])
    assert out.exit_code == 0 and "out of its project" in out.output
    assert kova.get_chat(chat["id"])["project_id"] is None
    missing = cli_runner.invoke(cli.app, ["chats", "move", chat["id"], "nope"])
    assert missing.exit_code == 1 and 'No project "nope"' in missing.output
