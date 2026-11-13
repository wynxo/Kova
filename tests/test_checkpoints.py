"""Per-turn change review and local checkpoints, independent of git (issue #52)."""

import shutil
import subprocess

import pytest
from fastapi.testclient import TestClient

from kova.checkpoints import BlobStore, ChangeTracker, apply_restore, preview, revert_plan
from kova.client import KovaClient, KovaError
from kova.config import ensure_token, load_config
from kova.server import create_app
from tests.fake_model import FakeModel, text, tool


@pytest.fixture
def project(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    (root / "notes.txt").write_text("one\ntwo\n")
    return root


@pytest.fixture
def server(tmp_path, project, monkeypatch):
    """A running app whose working folder is a real project directory."""
    monkeypatch.setenv("KOVA_HOME", str(tmp_path / "home"))
    cfg = load_config()
    cfg.files_path = project
    app = create_app(cfg, transport=FakeModel([]).transport)
    with TestClient(app) as http:
        yield KovaClient("http://testserver", ensure_token(cfg.home), http=http), cfg, app, project


def stream(client, app, chat_id, message, scripts):
    """Run one turn against the scripted model, returning its events."""
    app.state.agent.transport = FakeModel(scripts).transport
    return list(client.stream_message(chat_id, message))


def _changed_paths(project):
    """The paths git reports as changed, so checkpoint artifacts would show up."""
    status = subprocess.run(["git", "status", "--porcelain"], cwd=project,
                            capture_output=True, text=True, check=True).stdout
    return sorted(line[3:].strip() for line in status.splitlines())


def test_turn_edits_are_reviewable(server):
    client, cfg, app, project = server
    chat = client.create_chat(cwd=str(project), permission="auto")
    events = stream(client, app, chat["id"], "fix the notes", [
        tool("edit_file", {"path": "notes.txt", "old_text": "one", "new_text": "ONE"}),
        text("Done"),
    ])

    review = next(event for event in events if event["type"] == "review")
    assert review["checkpoint_id"]
    assert review["root"] == str(project)
    assert review["files"] == [{"path": "notes.txt", "status": "modified",
                                "additions": 1, "deletions": 1}]
    assert (project / "notes.txt").read_text() == "ONE\ntwo\n"

    checkpoints = client.list_checkpoints(chat["id"])
    assert [checkpoint["id"] for checkpoint in checkpoints] == [review["checkpoint_id"]]
    assert checkpoints[0]["label"] == "fix the notes"
    assert checkpoints[0]["root"] == str(project)
    assert client.get_chat(chat["id"])["messages"][-1]["role"] == "assistant"

    diff = client.checkpoint_diff(chat["id"], review["checkpoint_id"], "notes.txt")
    assert diff["status"] == "modified"
    assert "-one" in diff["diff"] and "+ONE" in diff["diff"]
    assert diff["before"] == "one\ntwo\n" and diff["after"] == "ONE\ntwo\n"


def test_restore_puts_files_back_and_never_touches_git(server):
    client, cfg, app, project = server
    if shutil.which("git") is None:
        pytest.skip("git is not installed")
    base = ["git", "-c", "user.email=t@example.com", "-c", "user.name=test"]
    subprocess.run(["git", "init", "-q"], cwd=project, check=True)
    subprocess.run([*base, "add", "."], cwd=project, check=True)
    subprocess.run([*base, "commit", "-qm", "base"], cwd=project, check=True)

    chat = client.create_chat(cwd=str(project), permission="auto")
    events = stream(client, app, chat["id"], "change the notes", [
        tool("edit_file", {"path": "notes.txt", "old_text": "two", "new_text": "TWO"}),
        tool("write_file", {"path": "new.txt", "content": "fresh\n"}),
        text("Done"),
    ])
    review = next(event for event in events if event["type"] == "review")
    assert {entry["path"]: entry["status"] for entry in review["files"]} == {
        "notes.txt": "modified", "new.txt": "added"}
    assert (project / "new.txt").exists()
    assert _changed_paths(project) == ["new.txt", "notes.txt"]

    result = client.restore_checkpoint(chat["id"], review["checkpoint_id"])
    assert result["ok"] is True
    assert result["restored"] == ["notes.txt"] and result["deleted"] == ["new.txt"]
    assert result["failed"] == []
    assert (project / "notes.txt").read_text() == "one\ntwo\n"
    assert not (project / "new.txt").exists()
    # The repository is exactly as committed: checkpoints live outside it.
    assert _changed_paths(project) == []
    assert (cfg.home / "checkpoints").is_dir()
    assert not (project / ".git" / "checkpoints").exists()


def test_checkpoints_are_scoped_to_one_chat(server):
    client, cfg, app, project = server
    chat = client.create_chat(cwd=str(project), permission="auto")
    other = client.create_chat(cwd=str(project), permission="auto")
    stream(client, app, chat["id"], "edit", [
        tool("edit_file", {"path": "notes.txt", "old_text": "one", "new_text": "ONE"}),
        text("Done"),
    ])

    assert len(client.list_checkpoints(chat["id"])) == 1
    assert client.list_checkpoints(other["id"]) == []

    client.delete_chat(chat["id"])
    with pytest.raises(KovaError) as gone:
        client.list_checkpoints(chat["id"])
    assert gone.value.status == 404
    assert app.state.store.list_checkpoints(chat["id"]) == []


def test_a_turn_without_file_changes_has_no_review(server):
    client, cfg, app, project = server
    chat = client.create_chat(cwd=str(project), permission="auto")
    events = stream(client, app, chat["id"], "just talk", [text("Hello")])
    assert not [event for event in events if event["type"] == "review"]
    assert client.list_checkpoints(chat["id"]) == []


def test_chat_mode_never_checkpoints(server):
    client, cfg, app, project = server
    chat = client.create_chat(mode="chat", cwd=str(project))
    events = stream(client, app, chat["id"], "hello", [text("Hi")])
    assert not [event for event in events if event["type"] == "review"]
    assert client.list_checkpoints(chat["id"]) == []


def test_restore_refuses_while_a_turn_is_running(server):
    client, cfg, app, project = server
    chat = client.create_chat(cwd=str(project), permission="auto")
    events = stream(client, app, chat["id"], "edit", [
        tool("edit_file", {"path": "notes.txt", "old_text": "one", "new_text": "ONE"}),
        text("Done"),
    ])
    checkpoint_id = next(event for event in events if event["type"] == "review")["checkpoint_id"]

    class Running:
        def done(self):
            return False

    app.state.chat_turns[chat["id"]] = {Running()}
    try:
        response = client.http.post(
            f"/chats/{chat['id']}/checkpoints/{checkpoint_id}/restore",
            headers={"Authorization": f"Bearer {client.token}"})
    finally:
        app.state.chat_turns.pop(chat["id"], None)
    assert response.status_code == 409
    assert (project / "notes.txt").read_text() == "ONE\ntwo\n"


def test_blob_store_deduplicates_and_round_trips(tmp_path):
    blobs = BlobStore(tmp_path / "blobs")
    digest = blobs.put(b"hello")
    assert digest == blobs.put(b"hello")
    assert blobs.get(digest) == b"hello"
    assert blobs.text(digest) == "hello"
    assert blobs.text(None) is None
    assert [path for path in (tmp_path / "blobs").rglob("*") if path.is_file()] == [blobs._path(digest)]


def test_change_tracker_keeps_the_first_state_of_a_turn(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    target = root / "a.txt"
    target.write_text("first")
    blobs = BlobStore(tmp_path / "blobs")
    tracker = ChangeTracker(blobs, root)

    tracker.note(target)
    target.write_text("second")
    tracker.note(target)  # a later write in the same turn must not overwrite the first state
    target.write_text("third")

    files, skipped = tracker.finish()
    assert skipped == [] and set(files) == {"a.txt"}
    assert files["a.txt"]["status"] == "modified"
    assert blobs.text(files["a.txt"]["before"]) == "first"
    assert blobs.text(files["a.txt"]["after"]) == "third"
    assert (files["a.txt"]["additions"], files["a.txt"]["deletions"]) == (1, 1)


def test_change_tracker_ignores_paths_outside_the_working_folder(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("x")
    tracker = ChangeTracker(BlobStore(tmp_path / "blobs"), root)
    tracker.note(outside)
    assert not tracker.touched


def test_change_tracker_skips_files_over_the_size_cap(tmp_path, monkeypatch):
    from kova import checkpoints

    monkeypatch.setattr(checkpoints, "MAX_FILE_BYTES", 4)
    root = tmp_path / "project"
    root.mkdir()
    target = root / "big.bin"
    target.write_bytes(b"123456789")
    tracker = ChangeTracker(BlobStore(tmp_path / "blobs"), root)
    tracker.note(target)
    files, skipped = tracker.finish()
    assert files == {} and skipped == ["big.bin"]


def test_revert_plan_uses_the_state_before_the_earliest_touch():
    rows = [
        {"id": 1, "files": {"a": {"before": None, "after": "x"}}},
        {"id": 2, "files": {"a": {"before": "x", "after": "y"}, "b": {"before": None, "after": "z"}}},
        {"id": 3, "files": {"a": {"before": "y", "after": "w"}}},
    ]
    assert revert_plan(rows, 2) == [("a", "x"), ("b", None)]
    assert revert_plan(rows, 1) == [("a", None), ("b", None)]


def test_apply_restore_writes_recorded_files_and_removes_created_ones(tmp_path):
    blobs = BlobStore(tmp_path / "blobs")
    digest = blobs.put(b"original")
    root = tmp_path / "project"
    root.mkdir()
    (root / "keep.txt").write_text("changed")
    (root / "created.txt").write_text("new")

    result = apply_restore(root, [("keep.txt", digest), ("created.txt", None)], blobs)
    assert result == {"restored": ["keep.txt"], "deleted": ["created.txt"], "failed": []}
    assert (root / "keep.txt").read_text() == "original"
    assert not (root / "created.txt").exists()


def test_preview_returns_a_unified_diff(tmp_path):
    blobs = BlobStore(tmp_path / "blobs")
    result = preview(blobs, "notes.txt", {"before": blobs.put(b"one\n"), "after": blobs.put(b"ONE\n"),
                                          "status": "modified", "additions": 1, "deletions": 1})
    assert "-one" in result["diff"] and "+ONE" in result["diff"]
    assert result["before"] == "one\n" and result["after"] == "ONE\n"
    assert result["truncated"] is False
