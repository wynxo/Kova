"""Server-side permission policy: a heuristic classifier and chat-scoped approvals."""

import pytest

from kova import permissions as perm


@pytest.mark.parametrize("command, expected", [
    ("rm -rf /tmp/kova", "destructive"),
    ("sudo apt install vim", "destructive"),
    ("git reset --hard", "destructive"),
    ("git push --force", "destructive"),
    ("dd if=/dev/sda of=/dev/sdb", "destructive"),
    ("> /dev/sda", "destructive"),
    ("shutdown -h now", "destructive"),
    (":(){ :|:&; }", "destructive"),  # the classic fork bomb
    ("echo hello world", "normal"),
    ("ls -la", "normal"),
    ("cat notes.txt", "normal"),
    ("git status", "normal"),
    ("", "normal"),
])
def test_command_risk_classifies(command, expected):
    assert perm.command_risk(command) == expected


def test_command_risk_normalises_none_to_empty():
    assert perm.command_risk(None) == "normal"


def test_full_permission_never_needs_approval(tmp_path):
    assert perm.needs_approval("run_command", {"command": "rm -rf /x"}, "full",
                               tmp_path, tmp_path) is False
    assert perm.needs_approval("write_file", {"path": "x"}, "full",
                               tmp_path, tmp_path) is False


def test_destructive_command_needs_approval_even_in_auto(tmp_path):
    assert perm.needs_approval("run_command", {"command": "rm -rf /x"}, "auto",
                               tmp_path, tmp_path) is True


def test_normal_command_in_auto_never_needs_approval(tmp_path):
    # auto trusts normal commands; only destructive ones break through.
    assert perm.needs_approval("run_command", {"command": "echo hi"}, "auto",
                               tmp_path, tmp_path) is False


def test_normal_command_in_manual_needs_approval(tmp_path):
    assert perm.needs_approval("run_command", {"command": "echo hi"}, "manual",
                               tmp_path, tmp_path) is True


def test_session_allowed_suppresses_manual_approval(tmp_path):
    assert perm.needs_approval("run_command", {"command": "echo hi"}, "manual",
                               tmp_path, tmp_path, session_allowed=True) is False


def test_write_inside_files_dir_needs_no_approval(tmp_path):
    # files is a subdir of cwd; writing inside it is session-trusted.
    files = tmp_path / "memory"
    assert perm.needs_approval("write_file", {"path": "memory/inside.txt"}, "manual",
                               tmp_path, files) is False


def test_write_outside_files_dir_needs_approval(tmp_path):
    files = tmp_path / "memory"
    assert perm.needs_approval("write_file", {"path": "outside.txt"}, "manual",
                               tmp_path, files) is True


def test_write_outside_files_dir_is_trusted_in_auto(tmp_path):
    files = tmp_path / "memory"
    assert perm.needs_approval("write_file", {"path": "outside.txt"}, "auto",
                               tmp_path, files) is False


def test_unknown_tool_needs_no_approval(tmp_path):
    assert perm.needs_approval("read_file", {"path": "x"}, "manual",
                               tmp_path, tmp_path) is False


async def test_approvals_allow_and_remove():
    approvals = perm.Approvals(timeout=1)
    created = approvals.create("chat1", "run_command", {"command": "echo hi"})
    approvals.answer("chat1", created["id"], "allow")
    result = await approvals.wait(created["id"])
    assert result["decision"] == "allow"
    # The pending entry is cleared once answered.
    assert created["id"] not in approvals.pending


async def test_create_reports_normal_risk_for_non_commands():
    approvals = perm.Approvals(timeout=1)
    created = approvals.create("chat1", "write_file", {"path": "notes.txt"})
    assert created["risk"] == "normal"
    assert created["summary"] == "write_file: notes.txt"


async def test_allow_session_for_non_destructive_tracks_session():
    approvals = perm.Approvals(timeout=1)
    created = approvals.create("chat1", "run_command", {"command": "echo hi"})
    approvals.answer("chat1", created["id"], "allow_session")
    result = await approvals.wait(created["id"])
    assert result["decision"] == "allow_session"
    assert "chat1" in approvals.sessions


async def test_allow_session_for_destructive_does_not_track_session():
    approvals = perm.Approvals(timeout=1)
    created = approvals.create("chat1", "run_command", {"command": "rm -rf /x"})
    approvals.answer("chat1", created["id"], "allow_session")
    result = await approvals.wait(created["id"])
    assert result["decision"] == "allow_session"
    assert "chat1" not in approvals.sessions


async def test_wait_times_out_and_denies():
    approvals = perm.Approvals(timeout=0.05)
    created = approvals.create("chat1", "run_command", {"command": "echo hi"})
    result = await approvals.wait(created["id"])
    assert result == {"decision": "deny", "message": "Approval timed out after 10 minutes"}
    assert created["id"] not in approvals.pending


async def test_answer_unknown_identifier_raises():
    approvals = perm.Approvals(timeout=1)
    with pytest.raises(KeyError):
        approvals.answer("chat1", "does-not-exist", "allow")


async def test_answer_wrong_chat_raises():
    approvals = perm.Approvals(timeout=1)
    created = approvals.create("chat1", "run_command", {"command": "echo hi"})
    with pytest.raises(KeyError):
        approvals.answer("chat2", created["id"], "allow")


def test_chown_on_any_absolute_path_and_system_file_redirects_are_destructive():
    from kova.permissions import command_risk
    for command in ("chown -R 0:0 /etc", "chgrp root /usr/bin/x", "echo x > /etc/passwd",
                    "echo AAA >> ~/.ssh/authorized_keys", "echo x >> $HOME/.bashrc"):
        assert command_risk(command) == "destructive", command
    for command in ("chown me file.txt", "echo hi > out.txt", "cat /etc/hosts"):
        assert command_risk(command) == "normal", command
