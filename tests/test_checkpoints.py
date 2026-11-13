from kova.tools.checkpoints import Checkpoints


def test_turn_checkpoint_reviews_and_restores_files_without_touching_git(tmp_path):
    home = tmp_path / "home"
    work = tmp_path / "project"
    git = work / ".git"
    git.mkdir(parents=True)
    index = git / "index"
    index.write_bytes(b"index stays byte-for-byte unchanged")
    (work / "edited.txt").write_text("before\n", encoding="utf-8")
    (work / "removed.txt").write_text("remove me\n", encoding="utf-8")

    checkpoints = Checkpoints(home)
    snapshot = checkpoints.begin(work)
    (work / "edited.txt").write_text("after\n", encoding="utf-8")
    (work / "removed.txt").unlink()
    (work / "created.txt").write_text("new\n", encoding="utf-8")
    record = checkpoints.finish(snapshot, "chat-1")

    assert record is not None
    assert {item["status"] for item in record["files"]} == {"modified", "deleted", "created"}
    assert "-before" in record["diff"] and "+after" in record["diff"]
    assert index.read_bytes() == b"index stays byte-for-byte unchanged"
    assert not (work / ".git" / "checkpoints").exists()

    restored = checkpoints.restore(record["id"], work, chat_id="chat-1")
    assert set(restored) == {"edited.txt", "removed.txt", "created.txt"}
    assert (work / "edited.txt").read_text(encoding="utf-8") == "before\n"
    assert (work / "removed.txt").read_text(encoding="utf-8") == "remove me\n"
    assert not (work / "created.txt").exists()
    assert index.read_bytes() == b"index stays byte-for-byte unchanged"


def test_unchanged_turn_does_not_create_checkpoint(tmp_path):
    work = tmp_path / "project"
    work.mkdir()
    file = work / "same.txt"
    file.write_text("same\n", encoding="utf-8")

    checkpoints = Checkpoints(tmp_path / "home")
    assert checkpoints.finish(checkpoints.begin(work), "chat-1") is None
