"""Project-scoped memory (#196): opt-in, isolated and backed up like global notes."""

from __future__ import annotations

import json
import zipfile

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from kova import cli
from kova.backup import export_archive, import_archive
from kova.client import KovaClient
from kova.config import ModelEntry, ensure_token, load_config
from kova.memory import Memory, MemoryStoreError
from kova.review import REVIEW_KIND, record_revision, undo
from kova.server import create_app
from kova.store import Store
from tests.fake_model import FakeModel, text, tool


@pytest.fixture
def server(tmp_path, monkeypatch):
    monkeypatch.setenv("KOVA_HOME", str(tmp_path))
    cfg = load_config()
    cfg.models[0].model = "demo-local"
    cfg.models.append(ModelEntry("second", "http://127.0.0.1:9/v1", "demo-second"))
    fake = FakeModel([text("One"), text("Two"), text("Three"), text("Four"),
                      text("Five"), text("Six"), text("Seven"), text("Eight")])
    headers = {"Authorization": f"Bearer {ensure_token(cfg.home)}"}
    with TestClient(create_app(cfg, transport=fake.transport)) as client:
        yield client, headers, cfg, fake


def _project(client, headers, name, memory=True):
    body = {"name": name, "memory_scope": memory}
    response = client.post("/projects", headers=headers, json=body)
    assert response.status_code == 200, response.text
    return response.json()


# ------------------------------------------------------------------ memory
def test_project_scope_is_hidden_from_global_memory(tmp_path):
    global_memory = Memory(tmp_path / "memory")
    global_memory.save("Owner", "Ada")
    alpha = global_memory.project_scope("alpha")
    beta = global_memory.project_scope("beta")
    alpha.save("Notes", "Alpha uses metric units")
    beta.save("Notes", "Beta uses imperial units")

    assert [note.slug for note in global_memory.list()] == ["owner"]
    assert global_memory.search("Alpha") == []
    assert global_memory.get("notes") is None
    assert [note.body for note in alpha.list()] == ["Alpha uses metric units"]
    assert [note.body for note in beta.list()] == ["Beta uses imperial units"]
    assert alpha.search("Beta") == []
    assert alpha.scope_id == "alpha" and global_memory.scope_id == ""


def test_project_scope_ids_cannot_escape_the_memory_folder(tmp_path):
    memory = Memory(tmp_path / "memory")
    for bad in ("", "..", "../home", "a/b", ".hidden"):
        with pytest.raises(MemoryStoreError):
            memory.project_scope(bad)
        assert memory.remove_scope(bad) is False
    assert not (tmp_path / "memory" / ".kova-projects").exists()


def test_project_revision_undo_returns_the_scoped_note(tmp_path):
    store = Store(tmp_path / "kova.db")
    global_memory = Memory(tmp_path / "memory")
    global_memory.save("Rules", "global text")
    scope = global_memory.project_scope("alpha")
    scope.save("Rules", "old project text")
    before = (scope.write_dir / "rules.md").read_text()
    scope.save("Rules", "new project text")
    revision = record_revision(store, scope, "rules", REVIEW_KIND, before=before,
                               scope="alpha")
    assert undo(global_memory, store, revision_id=revision) == ["rules"]
    assert scope.get("rules").body == "old project text"
    assert global_memory.get("rules").body == "global text"


# ------------------------------------------------------------------- store
def test_existing_project_tables_gain_an_opt_in_memory_flag(tmp_path):
    path = tmp_path / "kova.db"
    old = Store(path)
    project = old.create_project("Research")
    assert project["memory_scope"] is False
    old._conn.close()
    import sqlite3
    with sqlite3.connect(path) as db:
        db.execute("ALTER TABLE projects DROP COLUMN memory_scope")
    migrated = Store(path)
    saved = migrated.get_project(project["id"])
    assert saved["memory_scope"] is False
    assert migrated.update_project(project["id"], memory_scope=True)["memory_scope"] is True
    assert migrated.update_project(project["id"], memory_scope=False)["memory_scope"] is False


def test_import_project_preserves_membership_and_scope(tmp_path):
    source = Store(tmp_path / "source.db")
    project = source.create_project("Research", "Cite sources.", memory_scope=True)
    chat = source.create_chat("Paper", project_id=project["id"])
    exported = {**source.get_project(project["id"]), "memory_scope": True}

    target = Store(tmp_path / "target.db")
    assert target.import_project(exported) is True
    assert target.import_project(exported) is False  # ids are never overwritten
    restored = target.get_project(project["id"])
    assert restored is not None and restored["memory_scope"] is True
    assert target.import_chat(source.get_chat(chat["id"])) is True
    assert target.get_chat(chat["id"])["project_id"] == project["id"]


# ------------------------------------------------------------------ server
def test_project_memory_api_is_opt_in_and_isolated(server):
    client, headers, cfg, _ = server
    alpha = _project(client, headers, "Alpha")
    beta = _project(client, headers, "Beta")
    plain = _project(client, headers, "Plain", memory=False)

    assert client.post("/memory", headers=headers,
                       json={"title": "Owner", "body": "Ada"}).status_code == 200
    response = client.post("/memory", headers=headers, json={
        "title": "Units", "body": "Alpha uses metric", "project_id": alpha["id"]})
    assert response.status_code == 200, response.text
    client.post("/memory", headers=headers, json={
        "title": "Units", "body": "Beta uses imperial", "project_id": beta["id"]})

    global_notes = client.get("/memory", headers=headers).json()
    assert [note["slug"] for note in global_notes] == ["owner"]
    alpha_notes = client.get("/memory", headers=headers,
                             params={"project_id": alpha["id"]}).json()
    assert [note["body"] for note in alpha_notes] == ["Alpha uses metric"]
    found = client.get("/memory", headers=headers,
                       params={"q": "Alpha", "project_id": alpha["id"]}).json()
    assert [note["slug"] for note in found] == ["units"]
    assert client.get("/memory", headers=headers,
                      params={"q": "Beta", "project_id": alpha["id"]}).json() == []

    # Editing and deleting a project note cannot reach the global one.
    assert client.patch("/memory/units", headers=headers,
                        params={"project_id": alpha["id"]},
                        json={"body": "Alpha uses SI"}).json()["body"] == "Alpha uses SI"
    assert client.delete("/memory/units", headers=headers,
                         params={"project_id": alpha["id"]}).json() == {"ok": True}
    assert client.get("/memory", headers=headers).json()[0]["body"] == "Ada"
    assert client.get("/memory", headers=headers,
                      params={"project_id": beta["id"]}).json()[0]["body"] == "Beta uses imperial"

    assert client.get("/memory", headers=headers,
                      params={"project_id": plain["id"]}).status_code == 422
    assert client.post("/memory", headers=headers,
                       json={"title": "X", "body": "Y", "project_id": "missing"}).status_code == 404
    assert client.get("/memory", headers=headers,
                      params={"project_id": "missing"}).status_code == 404

    # Removing a project removes the scope it owns.
    client.post("/memory", headers=headers, json={
        "title": "Keep", "body": "not after deletion", "project_id": beta["id"]})
    assert client.delete(f"/projects/{beta['id']}", headers=headers).status_code == 200
    assert Memory(cfg.memory_path).project_scope(beta["id"]).list() == []


def test_project_turn_reads_only_its_scope_and_writes_there(server):
    client, headers, cfg, fake = server
    alpha = _project(client, headers, "Alpha")
    beta = _project(client, headers, "Beta")
    Memory(cfg.memory_path).save("Owner", "Ada")
    Memory(cfg.memory_path).project_scope(alpha["id"]).save("Rules", "Alpha uses metric")
    Memory(cfg.memory_path).project_scope(beta["id"]).save("Rules", "Beta uses imperial")

    chat = client.post("/chats", headers=headers, json={"project_id": alpha["id"]}).json()
    fake.scripts[:] = [tool("memory_add", {"title": "Decision", "body": "Use SI units"}),
                       text("Noted")]
    response = client.post(f"/chats/{chat['id']}/messages", headers=headers,
                           json={"text": "Ada, what units do we use?"})
    assert response.status_code == 200, response.text
    prompt = fake.requests[0]["messages"][0]["content"]
    assert 'What you remember about the user' in prompt and "Ada" in prompt
    assert 'What this project remembers for project "Alpha"' in prompt
    assert "Alpha uses metric" in prompt
    assert "Beta uses imperial" not in prompt

    scope = Memory(cfg.memory_path).project_scope(alpha["id"])
    assert scope.get("decision").body == "Use SI units"
    assert Memory(cfg.memory_path).get("decision") is None
    assert Memory(cfg.memory_path).project_scope(beta["id"]).get("decision") is None
    tools_sent = [row["function"]["name"] for row in fake.requests[0]["tools"]]
    assert "memory_add" in tools_sent


def test_project_memory_stays_off_until_enabled(server):
    client, headers, cfg, fake = server
    project = _project(client, headers, "Plain", memory=False)
    Memory(cfg.memory_path).project_scope(project["id"]).save("Old", "stored while on")
    chat = client.post("/chats", headers=headers, json={"project_id": project["id"]}).json()
    client.post(f"/chats/{chat['id']}/messages", headers=headers, json={"text": "old?"})
    prompt = fake.requests[-1]["messages"][0]["content"]
    assert "What this project remembers" not in prompt
    assert "stored while on" not in prompt

    chat = client.post("/chats", headers=headers, json={}).json()
    client.post(f"/chats/{chat['id']}/messages", headers=headers, json={"text": "old?"})
    assert "stored while on" not in fake.requests[-1]["messages"][0]["content"]


# ------------------------------------------------------------------- backup
def test_backup_round_trips_projects_memory_and_membership(tmp_path):
    source = load_config(tmp_path / "source")
    store = Store(source.home / "kova.db")
    project = store.create_project("Research", "Cite sources.", memory_scope=True)
    chat = store.create_chat("Paper", project_id=project["id"])
    global_memory = Memory(source.memory_path)
    global_memory.save("Owner", "Ada")
    global_memory.project_scope(project["id"]).save("Rules", "Use metric units")
    target = tmp_path / "backup.zip"

    stats = export_archive(source, target, store=store)
    assert (stats["projects"], stats["project_notes"]) == (1, 1)
    with zipfile.ZipFile(target) as archive:
        assert "projects.json" in archive.namelist()
        assert f"project-memory/{project['id']}/rules.md" in archive.namelist()
        assert "memory/owner.md" in archive.namelist()
        manifest = json.loads(archive.read("manifest.json"))
        assert manifest["project_notes"] == 1

    restored = load_config(tmp_path / "restored")
    result = import_archive(restored, target)
    assert (result["projects"], result["project_notes"], result["chats"]) == (1, 1, 1)
    saved = Store(restored.home / "kova.db")
    assert saved.get_project(project["id"])["memory_scope"] is True
    assert saved.get_chat(chat["id"])["project_id"] == project["id"]
    assert Memory(restored.memory_path).project_scope(project["id"]).get("rules").body == \
        "Use metric units"
    assert Memory(restored.memory_path).get("rules") is None

    again = import_archive(restored, target)
    assert (again["projects"], again["project_notes"], again["chats"]) == (0, 0, 0)

    # A replacing restore clears project scopes the archive does not contain.
    Memory(restored.memory_path).project_scope("orphan").save("Orphan", "stale")
    import_archive(restored, target, merge=False)
    assert Memory(restored.memory_path).project_scope("orphan").list() == []


# --------------------------------------------------------------------- CLI
def test_cli_toggles_and_browses_project_memory(server, monkeypatch):
    client, headers, cfg, _ = server
    kova = KovaClient("http://127.0.0.1:7700", ensure_token(cfg.home), http=client)
    monkeypatch.setattr(cli, "get_client", lambda: kova)
    runner = CliRunner()

    created = runner.invoke(cli.app, ["projects", "create", "Research", "--memory"])
    assert created.exit_code == 0, created.output
    [project] = kova.list_projects()
    assert project["memory_scope"] is True
    shown = runner.invoke(cli.app, ["projects", "show", project["id"]])
    assert "Project memory: on (isolated)" in shown.output

    added = runner.invoke(cli.app, ["memory", "add", "Units", "Use metric",
                                    "--project", project["id"]])
    assert added.exit_code == 0, added.output
    listed = runner.invoke(cli.app, ["memory", "list", "--project", project["id"]])
    assert "Units" in listed.output
    assert kova.memory_list() == []

    edited = runner.invoke(cli.app, ["projects", "edit", project["id"], "--no-memory"])
    assert edited.exit_code == 0, edited.output
    assert kova.get_project(project["id"])["memory_scope"] is False
    shown = runner.invoke(cli.app, ["projects", "show", project["id"]])
    assert "Project memory: off (global memory)" in shown.output
