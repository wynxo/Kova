"""Per-hunk diff view and undo for a change set's files (issue #190)."""

import hashlib
import os

import pytest
from fastapi.testclient import TestClient

from kova import hunks
from kova.checkpoints import BlobStore, line_counts
from kova.client import KovaClient
from kova.config import ensure_token, load_config
from kova.server import create_app
from tests.fake_model import FakeModel

BEFORE = "".join(f"line {n}\n" for n in range(1, 21))
AFTER = BEFORE.replace("line 3\n", "LINE 3\n").replace("line 15\n", "line 15\nadded\n")


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def entry_for(blobs: BlobStore, before: str | None, after: str | None) -> dict:
    additions, deletions = line_counts(before or "", after or "")
    return {"before": None if before is None else blobs.put(before.encode()),
            "after": None if after is None else blobs.put(after.encode()),
            "status": "modified", "additions": additions, "deletions": deletions}


def hunk_segments(view: dict) -> list[dict]:
    return [segment for segment in view["segments"] if segment["type"] == "hunk"]


# Files are written and read as bytes: text mode would turn "\n" into "\r\n"
# on Windows, and these tests check exact contents.

@pytest.fixture
def workspace(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    (root / "notes.txt").write_bytes(AFTER.encode())
    blobs = BlobStore(tmp_path / "blobs")
    return root, blobs, entry_for(blobs, BEFORE, AFTER)


# -- the view ---------------------------------------------------------------------

def test_view_splits_a_change_into_hunks_and_unchanged_runs(workspace):
    root, blobs, entry = workspace
    view = hunks.view(blobs, root, "notes.txt", entry)
    assert view["supported"] is True
    assert view["current_hash"] == sha(AFTER.encode())
    kinds = [segment["type"] for segment in view["segments"]]
    assert kinds == ["same", "hunk", "same", "hunk", "same"]
    first, second = hunk_segments(view)
    assert first["header"] == "@@ -3,1 +3,1 @@"
    assert first["removed"] == ["line 3"] and first["added"] == ["LINE 3"]
    assert second["removed"] == [] and second["added"] == ["added"]
    assert {first["state"], second["state"]} == {"applied"}
    assert first["id"] != second["id"]
    # Unchanged runs carry their lines, so the viewer can expand them.
    assert view["segments"][0]["lines"] == ["line 1", "line 2"]
    assert view["segments"][-1]["new"] == 17


def test_hunk_ids_do_not_depend_on_line_numbers(tmp_path):
    blobs = BlobStore(tmp_path / "blobs")
    root = tmp_path / "project"
    root.mkdir()
    (root / "a.txt").write_bytes("x\nnew\n".encode())
    (root / "b.txt").write_bytes("padding\nmore\nx\nnew\n".encode())
    first = hunks.view(blobs, root, "a.txt", entry_for(blobs, "x\nold\n", "x\nnew\n"))
    second = hunks.view(blobs, root, "b.txt", entry_for(blobs, "padding\nmore\nx\nold\n",
                                                          "padding\nmore\nx\nnew\n"))
    assert hunk_segments(first)[0]["id"] == hunk_segments(second)[0]["id"]


def test_a_binary_file_has_no_line_view(tmp_path):
    blobs = BlobStore(tmp_path / "blobs")
    root = tmp_path / "project"
    root.mkdir()
    (root / "image.bin").write_bytes(b"\xff\xfe\x00")
    entry = {"before": blobs.put(b"\xff\x00"), "after": blobs.put(b"\xff\xfe\x00"), "status": "modified"}
    view = hunks.view(blobs, root, "image.bin", entry)
    assert view["supported"] is False and "not text" in view["reason"]


# -- undo ----------------------------------------------------------------------------

def test_undo_reverts_one_hunk_and_keeps_the_rest(workspace):
    root, blobs, entry = workspace
    view = hunks.view(blobs, root, "notes.txt", entry)
    first, second = hunk_segments(view)
    change = hunks.undo(blobs, root, "notes.txt", entry, first["id"], view["current_hash"])
    text = (root / "notes.txt").read_bytes().decode()
    assert "line 3\n" in text and "LINE 3" not in text
    assert "line 15\nadded\n" in text
    assert change["hash"] == sha(text.encode())
    assert blobs.get(change["before"]) == AFTER.encode()
    assert (change["additions"], change["deletions"]) == (1, 1)

    again = hunks.view(blobs, root, "notes.txt", entry)
    states = {segment["id"]: segment["state"] for segment in hunk_segments(again)}
    assert states == {first["id"]: "undone", second["id"]: "applied"}

    # The second hunk still undoes against the new hash, at its new place.
    hunks.undo(blobs, root, "notes.txt", entry, second["id"], again["current_hash"])
    assert (root / "notes.txt").read_bytes().decode() == BEFORE


def test_undo_finds_its_hunk_after_unrelated_edits_moved_it(workspace):
    root, blobs, entry = workspace
    edited = "header 1\nheader 2\n" + AFTER + "footer\n"
    (root / "notes.txt").write_bytes(edited.encode())
    view = hunks.view(blobs, root, "notes.txt", entry)
    first, second = hunk_segments(view)
    assert first["state"] == second["state"] == "applied"
    hunks.undo(blobs, root, "notes.txt", entry, second["id"], view["current_hash"])
    assert (root / "notes.txt").read_bytes().decode() == edited.replace("line 15\nadded\n", "line 15\n")


def test_undo_keeps_line_endings_and_a_missing_final_newline(tmp_path):
    blobs = BlobStore(tmp_path / "blobs")
    root = tmp_path / "project"
    root.mkdir()
    before, after = b"a\r\nb\r\nc", b"a\r\nB\r\nc"
    (root / "crlf.txt").write_bytes(after)
    entry = {"before": blobs.put(before), "after": blobs.put(after), "status": "modified"}
    view = hunks.view(blobs, root, "crlf.txt", entry)
    hunks.undo(blobs, root, "crlf.txt", entry, hunk_segments(view)[0]["id"], view["current_hash"])
    assert (root / "crlf.txt").read_bytes() == before


def crlf(text: str) -> str:
    return text.replace("\n", "\r\n")


def test_a_crlf_file_undoes_one_hunk_and_keeps_every_other_byte(tmp_path):
    blobs = BlobStore(tmp_path / "blobs")
    root = tmp_path / "project"
    root.mkdir()
    (root / "notes.txt").write_bytes(crlf(AFTER).encode())
    entry = entry_for(blobs, crlf(BEFORE), crlf(AFTER))
    view = hunks.view(blobs, root, "notes.txt", entry)
    assert view["current_hash"] == sha(crlf(AFTER).encode())
    first, second = hunk_segments(view)
    assert first["state"] == second["state"] == "applied"
    assert first["removed"] == ["line 3"] and first["added"] == ["LINE 3"]
    hunks.undo(blobs, root, "notes.txt", entry, second["id"], view["current_hash"])
    assert (root / "notes.txt").read_bytes() == crlf(AFTER.replace("line 15\nadded\n", "line 15\n")).encode()


def test_undo_follows_line_endings_rewritten_after_the_turn(tmp_path):
    # The turn wrote "\n"; an editor or git's autocrlf then rewrote the file with
    # "\r\n". Its hunks are still the turn's, and the line put back takes "\r\n".
    blobs = BlobStore(tmp_path / "blobs")
    root = tmp_path / "project"
    root.mkdir()
    (root / "notes.txt").write_bytes(crlf(AFTER).encode())
    entry = entry_for(blobs, BEFORE, AFTER)
    view = hunks.view(blobs, root, "notes.txt", entry)
    first, second = hunk_segments(view)
    assert first["state"] == second["state"] == "applied"
    hunks.undo(blobs, root, "notes.txt", entry, first["id"], view["current_hash"])
    assert (root / "notes.txt").read_bytes() == crlf(AFTER.replace("LINE 3\n", "line 3\n")).encode()
    again = {segment["id"]: segment["state"] for segment in hunk_segments(hunks.view(blobs, root, "notes.txt", entry))}
    assert again == {first["id"]: "undone", second["id"]: "applied"}


def test_a_form_feed_does_not_split_a_line(tmp_path):
    blobs = BlobStore(tmp_path / "blobs")
    root = tmp_path / "project"
    root.mkdir()
    before, after = "a\fb\nc\n", "a\fb\nC\n"
    (root / "page.txt").write_bytes(after.encode())
    view = hunks.view(blobs, root, "page.txt", entry_for(blobs, before, after))
    assert hunk_segments(view)[0]["header"] == "@@ -2,1 +2,1 @@"


def test_undo_refuses_when_the_file_changed_since_it_was_shown(workspace):
    root, blobs, entry = workspace
    view = hunks.view(blobs, root, "notes.txt", entry)
    (root / "notes.txt").write_bytes((AFTER + "typed by hand\n").encode())
    with pytest.raises(hunks.HunkError) as caught:
        hunks.undo(blobs, root, "notes.txt", entry, hunk_segments(view)[0]["id"], view["current_hash"])
    assert caught.value.status == 409
    assert (root / "notes.txt").read_bytes().decode() == AFTER + "typed by hand\n"


def test_a_hunk_edited_since_cannot_be_undone(workspace):
    root, blobs, entry = workspace
    (root / "notes.txt").write_bytes(AFTER.replace("LINE 3\n", "LINE THREE\n").encode())
    view = hunks.view(blobs, root, "notes.txt", entry)
    first, second = hunk_segments(view)
    assert first["state"] == "changed" and second["state"] == "applied"
    with pytest.raises(hunks.HunkError) as caught:
        hunks.undo(blobs, root, "notes.txt", entry, first["id"], view["current_hash"])
    assert caught.value.status == 409


def test_an_unknown_hunk_is_not_found(workspace):
    root, blobs, entry = workspace
    view = hunks.view(blobs, root, "notes.txt", entry)
    with pytest.raises(hunks.HunkError) as caught:
        hunks.undo(blobs, root, "notes.txt", entry, "0" * 16, view["current_hash"])
    assert caught.value.status == 404


@pytest.mark.parametrize("path", ["../outside.txt", "sub/../../outside.txt", "/etc/hostname", ""])
def test_paths_outside_the_working_folder_are_refused(workspace, path):
    root, blobs, entry = workspace
    (root.parent / "outside.txt").write_bytes(AFTER.encode())
    with pytest.raises(hunks.HunkError) as caught:
        hunks.undo(blobs, root, path, entry, "0" * 16, sha(AFTER.encode()))
    assert caught.value.status in (400, 403)
    assert (root.parent / "outside.txt").read_bytes().decode() == AFTER


@pytest.mark.skipif(not hasattr(os, "symlink"), reason="no symbolic links here")
def test_a_symbolic_link_is_never_followed(workspace):
    root, blobs, entry = workspace
    outside = root.parent / "secret"
    outside.mkdir()
    (outside / "notes.txt").write_bytes(AFTER.encode())
    try:
        (root / "linked.txt").symlink_to(outside / "notes.txt")
        (root / "dir").symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("cannot create symbolic links here")
    for path in ("linked.txt", "dir/notes.txt"):
        with pytest.raises(hunks.HunkError) as caught:
            hunks.view(blobs, root, path, entry)
        assert caught.value.status == 403
        with pytest.raises(hunks.HunkError):
            hunks.undo(blobs, root, path, entry, "0" * 16, sha(AFTER.encode()))
    assert (outside / "notes.txt").read_bytes().decode() == AFTER


# -- undoing the whole file ----------------------------------------------------

def test_undo_file_reverts_every_hunk_and_keeps_later_edits_elsewhere(workspace):
    root, blobs, entry = workspace
    (root / "other.txt").write_bytes(b"not part of this change set\n")
    edited = "typed at the top\n" + AFTER + "typed at the end\n"
    (root / "notes.txt").write_bytes(edited.encode())
    view = hunks.view(blobs, root, "notes.txt", entry)
    assert [segment["state"] for segment in hunk_segments(view)] == ["applied", "applied"]
    change = hunks.undo_file(blobs, root, "notes.txt", entry, view["current_hash"])
    assert (root / "notes.txt").read_bytes().decode() == \
        "typed at the top\n" + BEFORE + "typed at the end\n"
    assert change["status"] == "modified" and change["deleted"] is False
    assert blobs.get(change["before"]) == edited.encode()
    assert (root / "other.txt").read_bytes() == b"not part of this change set\n"

    again = hunks.view(blobs, root, "notes.txt", entry)
    assert {segment["state"] for segment in hunk_segments(again)} == {"undone"}


def test_undo_file_refuses_when_any_hunk_was_edited_since(workspace):
    root, blobs, entry = workspace
    edited = AFTER.replace("LINE 3\n", "LINE THREE\n")
    (root / "notes.txt").write_bytes(edited.encode())
    view = hunks.view(blobs, root, "notes.txt", entry)
    with pytest.raises(hunks.HunkError) as caught:
        hunks.undo_file(blobs, root, "notes.txt", entry, view["current_hash"])
    assert caught.value.status == 409
    assert (root / "notes.txt").read_bytes().decode() == edited


def test_undo_file_refuses_a_stale_hash(workspace):
    root, blobs, entry = workspace
    view = hunks.view(blobs, root, "notes.txt", entry)
    (root / "notes.txt").write_bytes((AFTER + "typed by hand\n").encode())
    with pytest.raises(hunks.HunkError) as caught:
        hunks.undo_file(blobs, root, "notes.txt", entry, view["current_hash"])
    assert caught.value.status == 409
    assert (root / "notes.txt").read_bytes().decode() == AFTER + "typed by hand\n"


def test_undo_file_refuses_when_nothing_is_left_to_undo(workspace):
    root, blobs, entry = workspace
    view = hunks.view(blobs, root, "notes.txt", entry)
    (root / "notes.txt").write_bytes(BEFORE.encode())
    current = hunks.view(blobs, root, "notes.txt", entry)
    assert {segment["state"] for segment in hunk_segments(current)} == {"undone"}
    with pytest.raises(hunks.HunkError) as caught:
        hunks.undo_file(blobs, root, "notes.txt", entry, current["current_hash"])
    assert caught.value.status == 409
    assert (root / "notes.txt").read_bytes().decode() == BEFORE
    assert view["current_hash"] != current["current_hash"]


def test_undo_file_removes_a_file_the_change_set_created(tmp_path):
    blobs = BlobStore(tmp_path / "blobs")
    root = tmp_path / "project"
    root.mkdir()
    created = "one\nnew file\n"
    (root / "created.txt").write_bytes(created.encode())
    entry = entry_for(blobs, None, created)
    view = hunks.view(blobs, root, "created.txt", entry)
    change = hunks.undo_file(blobs, root, "created.txt", entry, view["current_hash"])
    assert not (root / "created.txt").exists()
    assert change["status"] == "deleted" and change["deleted"] is True and change["after"] is None
    assert blobs.get(change["before"]) == created.encode()


def test_undo_file_refuses_a_missing_file(workspace):
    root, blobs, entry = workspace
    (root / "notes.txt").unlink()
    with pytest.raises(hunks.HunkError) as caught:
        hunks.undo_file(blobs, root, "notes.txt", entry, sha(AFTER.encode()))
    assert caught.value.status == 409


def test_undo_file_follows_line_endings_rewritten_after_the_turn(tmp_path):
    blobs = BlobStore(tmp_path / "blobs")
    root = tmp_path / "project"
    root.mkdir()
    (root / "notes.txt").write_bytes(crlf(AFTER).encode())
    entry = entry_for(blobs, BEFORE, AFTER)
    view = hunks.view(blobs, root, "notes.txt", entry)
    hunks.undo_file(blobs, root, "notes.txt", entry, view["current_hash"])
    assert (root / "notes.txt").read_bytes() == crlf(BEFORE).encode()


# -- the routes ----------------------------------------------------------------------

@pytest.fixture
def server(tmp_path, monkeypatch):
    monkeypatch.setenv("KOVA_HOME", str(tmp_path / "home"))
    project = tmp_path / "project"
    project.mkdir()
    (project / "notes.txt").write_bytes(AFTER.encode())
    cfg = load_config()
    cfg.files_path = project
    app = create_app(cfg, transport=FakeModel([]).transport)
    with TestClient(app) as http:
        client = KovaClient("http://testserver", ensure_token(cfg.home), http=http)
        chat = client.create_chat(cwd=str(project), permission="auto")
        blobs = app.state.agent.blobs
        checkpoint = app.state.store.add_checkpoint(
            chat["id"], str(project), {"notes.txt": entry_for(blobs, BEFORE, AFTER)}, "edit notes")
        yield client, app, project, chat["id"], checkpoint


def call(client, method, path, **kwargs):
    return client.http.request(method, path, headers={"Authorization": f"Bearer {client.token}"}, **kwargs)


def test_routes_show_and_undo_a_hunk_and_record_it(server):
    client, app, project, chat_id, checkpoint = server
    base = f"/chats/{chat_id}/checkpoints/{checkpoint}/hunks"
    view = call(client, "GET", base, params={"path": "notes.txt"}).json()
    first = hunk_segments(view)[0]
    response = call(client, "POST", f"{base}/undo", json={
        "path": "notes.txt", "hunk": first["id"], "expected_hash": view["current_hash"]})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["current_hash"] == sha((project / "notes.txt").read_bytes())
    assert hunk_segments(body["view"])[0]["state"] == "undone"

    # The undo is a change set of its own, so it can be reviewed and restored.
    recorded = app.state.store.get_checkpoint(chat_id, body["checkpoint_id"])
    assert recorded["label"] == "Undid one change in notes.txt"
    assert recorded["files"]["notes.txt"]["undo_of"] == {"checkpoint": checkpoint, "hunk": first["id"]}
    client.restore_checkpoint(chat_id, body["checkpoint_id"])
    assert (project / "notes.txt").read_bytes().decode() == AFTER


def test_route_refuses_a_stale_hash(server):
    client, _, project, chat_id, checkpoint = server
    base = f"/chats/{chat_id}/checkpoints/{checkpoint}/hunks"
    view = call(client, "GET", base, params={"path": "notes.txt"}).json()
    (project / "notes.txt").write_bytes((AFTER + "more\n").encode())
    response = call(client, "POST", f"{base}/undo", json={
        "path": "notes.txt", "hunk": hunk_segments(view)[0]["id"], "expected_hash": view["current_hash"]})
    assert response.status_code == 409
    assert "changed since" in response.json()["detail"]


def test_route_only_knows_files_in_the_change_set(server):
    client, _, project, chat_id, checkpoint = server
    (project.parent / "outside.txt").write_bytes(AFTER.encode())
    base = f"/chats/{chat_id}/checkpoints/{checkpoint}/hunks"
    for path in ("../outside.txt", "other.txt"):
        assert call(client, "GET", base, params={"path": path}).status_code == 404
        response = call(client, "POST", f"{base}/undo",
                        json={"path": path, "hunk": "0" * 16, "expected_hash": "0" * 64})
        assert response.status_code == 404
    assert (project.parent / "outside.txt").read_bytes().decode() == AFTER


def test_route_refuses_when_the_chat_left_that_folder(server, tmp_path):
    client, _, project, chat_id, checkpoint = server
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    response = call(client, "PATCH", f"/chats/{chat_id}", json={"cwd": str(elsewhere)})
    if response.status_code >= 400:
        pytest.skip("this build cannot move a chat to another folder")
    base = f"/chats/{chat_id}/checkpoints/{checkpoint}/hunks"
    assert call(client, "GET", base, params={"path": "notes.txt"}).status_code == 409


def test_route_refuses_while_a_turn_is_running(server):
    client, app, _, chat_id, checkpoint = server

    class Running:
        def done(self):
            return False

    app.state.chat_turns[chat_id] = {Running()}
    base = f"/chats/{chat_id}/checkpoints/{checkpoint}/hunks"
    view = call(client, "GET", base, params={"path": "notes.txt"}).json()
    response = call(client, "POST", f"{base}/undo", json={
        "path": "notes.txt", "hunk": hunk_segments(view)[0]["id"], "expected_hash": view["current_hash"]})
    assert response.status_code == 409
    app.state.chat_turns[chat_id] = set()


def file_undo_url(chat_id: int | str, checkpoint: int) -> str:
    return f"/chats/{chat_id}/checkpoints/{checkpoint}/file/undo"


def test_route_undoes_the_whole_file_and_records_it(server):
    client, app, project, chat_id, checkpoint = server
    (project / "sibling.txt").write_bytes(b"not ours to undo\n")
    view = call(client, "GET", f"/chats/{chat_id}/checkpoints/{checkpoint}/hunks",
                params={"path": "notes.txt"}).json()
    response = call(client, "POST", file_undo_url(chat_id, checkpoint), json={
        "path": "notes.txt", "expected_hash": view["current_hash"]})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["ok"] is True and body["deleted"] is False
    assert (project / "notes.txt").read_bytes().decode() == BEFORE
    assert (project / "sibling.txt").read_bytes() == b"not ours to undo\n"

    recorded = app.state.store.get_checkpoint(chat_id, body["checkpoint_id"])
    assert recorded["label"] == "Undid all changes in notes.txt"
    assert recorded["files"]["notes.txt"]["undo_of"] == {"checkpoint": checkpoint, "file": True}
    client.restore_checkpoint(chat_id, body["checkpoint_id"])
    assert (project / "notes.txt").read_bytes().decode() == AFTER


def test_route_refuses_a_whole_file_undo_after_a_later_edit(server):
    client, _, project, chat_id, checkpoint = server
    (project / "notes.txt").write_bytes(AFTER.replace("added\n", "edited by hand\n").encode())
    view = call(client, "GET", f"/chats/{chat_id}/checkpoints/{checkpoint}/hunks",
                params={"path": "notes.txt"}).json()
    response = call(client, "POST", file_undo_url(chat_id, checkpoint), json={
        "path": "notes.txt", "expected_hash": view["current_hash"]})
    assert response.status_code == 409
    assert "edited since" in response.json()["detail"]
    assert (project / "notes.txt").read_bytes().decode() == AFTER.replace("added\n", "edited by hand\n")


def test_route_whole_file_undo_only_knows_files_in_the_change_set(server):
    client, _, project, chat_id, checkpoint = server
    (project.parent / "outside.txt").write_bytes(AFTER.encode())
    response = call(client, "POST", file_undo_url(chat_id, checkpoint), json={
        "path": "../outside.txt", "expected_hash": sha(AFTER.encode())})
    assert response.status_code == 404
    assert (project.parent / "outside.txt").read_bytes().decode() == AFTER
