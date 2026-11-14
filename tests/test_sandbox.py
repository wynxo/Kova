"""OS confinement for commands: Landlock detection, help, policy and behaviour (#42)."""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest

from kova import sandbox
from kova.permissions import needs_approval
from kova.tools.commands import run_command


def test_abi_and_availability_agree():
    assert sandbox.available() is (sandbox.abi_version() > 0)


def test_env_can_disable_the_sandbox(monkeypatch):
    monkeypatch.setattr(sandbox, "_abi", None)
    monkeypatch.setenv("KOVA_SANDBOX", "off")
    try:
        assert sandbox.abi_version() == 0
        assert sandbox.available() is False
        assert sandbox.wrap(["bash", "-c", "true"], Path("/tmp")) is None
    finally:
        monkeypatch.setattr(sandbox, "_abi", None)


def test_wrap_points_at_this_interpreter(monkeypatch):
    monkeypatch.setattr(sandbox, "available", lambda: True)
    wrapped = sandbox.wrap(["bash", "-c", "true"], Path("/tmp"))
    assert wrapped[:2] == [sandbox.sys.executable, "-m"]
    assert wrapped[-3:] == ["bash", "-c", "true"]


def test_writable_roots_include_the_working_folder(tmp_path):
    roots = sandbox.writable_roots(tmp_path)
    assert tmp_path.resolve() in roots


async def test_sandbox_confines_writes_to_the_working_folder(tmp_path):
    if not sandbox.available():
        pytest.skip("Landlock is not available on this kernel")
    work = tmp_path / "work"
    work.mkdir()
    private = Path.home() / f".kova-sandbox-probe-{uuid.uuid4().hex}"
    try:
        blocked = await run_command(f"echo nope > {private}", work, sandboxed=True)
        assert not blocked["ok"] and blocked["sandboxed"] is True
        assert not private.exists()

        allowed = await run_command("echo yes > inside.txt && cat inside.txt", work, sandboxed=True)
        assert allowed["ok"] and "yes" in allowed["output"]
        assert (work / "inside.txt").read_text().strip() == "yes"

        devnull = await run_command("echo quiet >/dev/null", work, sandboxed=True)
        assert devnull["ok"]
    finally:
        private.unlink(missing_ok=True)


async def test_unavailable_sandbox_refuses_to_run(tmp_path, monkeypatch):
    marker = tmp_path / "should-not-exist"
    monkeypatch.setattr(sandbox, "available", lambda: False)
    result = await run_command(f"touch {marker}", tmp_path, sandboxed=True)
    assert result["ok"] is False and result["sandboxed"] is False
    assert "sandbox" in result["error"].lower()
    assert not marker.exists()


@pytest.mark.parametrize("permission", ["manual", "auto"])
def test_a_confined_safe_command_skips_approval(permission, tmp_path):
    files = tmp_path / "files"
    assert needs_approval("run_command", {"command": "echo hi"}, permission,
                          tmp_path, files, sandboxed=True) is False


def test_a_confined_destructive_command_still_asks(tmp_path):
    # Containment stops system damage, not deleting the folder the user is in.
    assert needs_approval("run_command", {"command": "rm -rf ."}, "auto",
                          tmp_path, tmp_path / "files", sandboxed=True) is True


def test_data_loss_in_the_working_folder_still_asks_when_confined(tmp_path):
    assert needs_approval("run_command", {"command": "git reset --hard"}, "manual",
                          tmp_path, tmp_path / "files", sandboxed=True) is True


@pytest.mark.parametrize("permission", ["manual", "auto"])
def test_an_unavailable_sandbox_falls_back_to_asking(permission, tmp_path):
    files = tmp_path / "files"
    assert needs_approval("run_command", {"command": "echo hi"}, permission,
                          tmp_path, files, sandboxed=False) is True


def test_full_access_runs_unconfined_without_asking(tmp_path):
    assert needs_approval("run_command", {"command": "echo hi"}, "full",
                          tmp_path, tmp_path / "files", sandboxed=False) is False


def test_the_legacy_heuristic_is_unchanged(tmp_path):
    files = tmp_path / "files"
    assert needs_approval("run_command", {"command": "echo hi"}, "manual",
                          tmp_path, files) is True
    assert needs_approval("run_command", {"command": "echo hi"}, "auto",
                          tmp_path, files) is False
    assert needs_approval("run_command", {"command": "rm x"}, "auto",
                          tmp_path, files) is True


async def test_real_exit_125_is_not_setup_failure(tmp_path):
    if not sandbox.available():
        pytest.skip("Landlock unavailable")
    result = await run_command("echo ran; exit 125", tmp_path, sandboxed=True)
    assert result["exit_code"] == 125
    assert result["output"].strip() == "ran"
    assert result["error"] == "Command exited with status 125"


async def test_launcher_setup_failure_does_not_run_command(tmp_path, monkeypatch):
    # Parent sees availability, but the launcher genuinely cannot apply Landlock.
    monkeypatch.setattr(sandbox, "available", lambda: True)
    monkeypatch.setenv("KOVA_SANDBOX", "off")
    marker = tmp_path / "must-not-exist"
    result = await run_command(f"touch {marker}", tmp_path, sandboxed=True)
    assert result["exit_code"] == 125
    assert "could not be established" in result["error"]
    assert not marker.exists()
