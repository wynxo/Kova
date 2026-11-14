"""User-level installation lifecycle, without touching a real uv tool or server."""

import json
import signal
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import Mock

import httpx
import pytest
from typer.testing import CliRunner

from kova.cli import app


def test_version_does_not_initialize_data(tmp_path, monkeypatch):
    home = tmp_path / "data"
    monkeypatch.setenv("KOVA_HOME", str(home))
    result = CliRunner().invoke(app, ["--version"])
    assert result.exit_code == 0, result.output
    assert "0.1.0" in result.output
    assert not home.exists()


@pytest.fixture
def lifecycle(tmp_path, monkeypatch):
    from kova import lifecycle

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setenv("KOVA_HOME", str(tmp_path / ".kova"))
    monkeypatch.delenv("KOVA_REF", raising=False)
    monkeypatch.delenv("KOVA_SOURCE", raising=False)
    monkeypatch.setattr(lifecycle, "find_uv", lambda: "/test/uv")
    monkeypatch.setattr(lifecycle, "sys", SimpleNamespace(platform="linux", stdin=sys.stdin))
    return lifecycle


def install_metadata(lifecycle, monkeypatch, commit=None):
    origin = {"url": "https://github.com/wynxo/Kova"}
    if commit:
        origin["vcs_info"] = {"vcs": "git", "commit_id": commit}
    dist = Mock(version="0.1.0")
    dist.read_text.return_value = json.dumps(origin)
    monkeypatch.setattr(lifecycle.metadata, "distribution", lambda name: dist)


def remote(lifecycle, monkeypatch, sha="b" * 40, version="0.1.0"):
    def get(url, **kwargs):
        request = httpx.Request("GET", url)
        if "/commits/" in url:
            return httpx.Response(200, json={"sha": sha}, request=request)
        return httpx.Response(200, text=f'[project]\nversion = "{version}"\n', request=request)
    monkeypatch.setattr(lifecycle.httpx, "get", get)


@pytest.mark.parametrize("same", [True, False])
def test_update_check_compares_commits_without_installing(lifecycle, monkeypatch, same):
    install_metadata(lifecycle, monkeypatch, "b" * 40 if same else "a" * 40)
    remote(lifecycle, monkeypatch)
    run = Mock(side_effect=AssertionError("--check must not run uv"))
    monkeypatch.setattr(lifecycle.subprocess, "run", run)
    result = CliRunner().invoke(app, ["update", "--check"])
    assert result.exit_code == 0, result.output
    assert ("Up to date" if same else "Update available") in result.output
    assert not run.called


def test_update_check_without_commit_does_not_claim_current(lifecycle, monkeypatch):
    install_metadata(lifecycle, monkeypatch)
    remote(lifecycle, monkeypatch)
    result = CliRunner().invoke(app, ["update", "--check", "--ref", "v0.2.0"])
    assert result.exit_code == 0, result.output
    assert "commit unavailable" in result.output
    assert "Up to date" not in result.output


def test_update_check_reports_version_difference(lifecycle, monkeypatch):
    install_metadata(lifecycle, monkeypatch)
    remote(lifecycle, monkeypatch, version="0.2.0")
    result = CliRunner().invoke(app, ["update", "--check"])
    assert result.exit_code == 0, result.output
    assert "Update available" in result.output
    assert "0.2.0" in result.output


def test_update_check_network_error(lifecycle, monkeypatch):
    monkeypatch.setattr(lifecycle.httpx, "get", Mock(side_effect=httpx.ConnectError("offline")))
    result = CliRunner().invoke(app, ["update", "--check"])
    assert result.exit_code == 1
    assert "Could not check" in result.output
    assert "Traceback" not in result.output


@pytest.mark.parametrize("git", [True, False])
def test_update_uses_selected_ref_and_archive_without_git(lifecycle, monkeypatch, git):
    monkeypatch.setattr(lifecycle.shutil, "which", lambda name: "/test/git" if git else None)
    run = Mock(return_value=subprocess.CompletedProcess([], 0))
    monkeypatch.setattr(lifecycle.subprocess, "run", run)
    result = CliRunner().invoke(app, ["update", "--ref", "v0.2.0"])
    assert result.exit_code == 0, result.output
    args = run.call_args.args[0]
    assert args[:4] == ["/test/uv", "tool", "install", "--force"]
    assert args[-1] == ("kova-ai @ git+https://github.com/wynxo/Kova@v0.2.0" if git else
                        "kova-ai @ https://github.com/wynxo/Kova/archive/v0.2.0.tar.gz")
    assert "--refresh" in args


def test_update_uv_failure_is_not_success(lifecycle, monkeypatch):
    monkeypatch.setattr(lifecycle.subprocess, "run", Mock(side_effect=subprocess.CalledProcessError(1, "uv")))
    result = CliRunner().invoke(app, ["update"])
    assert result.exit_code == 1
    assert "Updated" not in result.output


@pytest.mark.parametrize("args,answer,deleted", [
    (["--keep-data"], None, False), (["--purge"], None, True),
    ([], "y\n", True), ([], "n\n", False), ([], None, False),
])
def test_uninstall_data_choices(lifecycle, monkeypatch, tmp_path, args, answer, deleted):
    home = tmp_path / ".kova"
    home.mkdir()
    (home / "chat.db").write_text("chats")
    external = tmp_path / "vault"
    external.mkdir()
    (external / "note.md").write_text("untouched")
    (home / "config.toml").write_text(f'[memory]\npath = "{external.as_posix()}"\n')
    events = []
    (home / "server.pid").write_text("12345")
    monkeypatch.setattr(lifecycle.os, "kill", lambda pid, sig: events.append((pid, sig)))
    def run(cmd, **kwargs):
        events.append(cmd)
        return subprocess.CompletedProcess(cmd, 0)
    monkeypatch.setattr(lifecycle.subprocess, "run", run)
    monkeypatch.setattr(lifecycle, "sys", SimpleNamespace(
        platform="linux", stdin=SimpleNamespace(isatty=lambda: answer is not None),
    ))
    result = CliRunner().invoke(app, ["uninstall", *args], input=answer)
    assert result.exit_code == 0, result.output
    assert home.exists() is not deleted
    assert (external / "note.md").read_text() == "untouched"
    assert events[0] == (12345, signal.SIGTERM)
    assert events[1] == ["/test/uv", "tool", "uninstall", "kova-ai"]


def test_uninstall_keep_data_removes_only_the_staged_desktop_app(lifecycle, monkeypatch, tmp_path):
    home = tmp_path / ".kova"
    staged = home / "desktop"
    (staged / "node_modules").mkdir(parents=True)
    (staged / "package.json").write_text("{}")
    (home / "chat.db").write_text("chats")
    monkeypatch.setattr(lifecycle.subprocess, "run",
                        Mock(return_value=subprocess.CompletedProcess([], 0)))
    result = CliRunner().invoke(app, ["uninstall", "--keep-data"])
    assert result.exit_code == 0, result.output
    assert (home / "chat.db").is_file()
    assert not staged.exists()
    assert "Removed the desktop app files" in result.output


def test_uninstall_refuses_conflicting_flags(lifecycle, monkeypatch):
    run = Mock()
    monkeypatch.setattr(lifecycle.subprocess, "run", run)
    result = CliRunner().invoke(app, ["uninstall", "--purge", "--keep-data"])
    assert result.exit_code != 0
    assert not run.called


def test_uninstall_uv_failure_keeps_data(lifecycle, monkeypatch, tmp_path):
    home = tmp_path / ".kova"
    home.mkdir()
    monkeypatch.setattr(lifecycle.subprocess, "run", Mock(side_effect=subprocess.CalledProcessError(1, "uv")))
    result = CliRunner().invoke(app, ["uninstall", "--purge"])
    assert result.exit_code == 1
    assert home.is_dir()


@pytest.mark.parametrize("target", ["home", "root", "symlink"])
def test_uninstall_refuses_dangerous_purge(lifecycle, monkeypatch, tmp_path, target):
    path = tmp_path if target == "home" else tmp_path.anchor
    if target == "symlink":
        path = tmp_path / ".kova"
        path.symlink_to(tmp_path, target_is_directory=True)
    monkeypatch.setenv("KOVA_HOME", str(path))
    monkeypatch.setattr(lifecycle.subprocess, "run", Mock(return_value=subprocess.CompletedProcess([], 0)))
    result = CliRunner().invoke(app, ["uninstall", "--purge"])
    assert result.exit_code == 1
    assert "Refusing" in result.output
    assert tmp_path.is_dir()


@pytest.mark.parametrize('purge', [False, True])
def test_windows_uninstall_schedules_helper_without_running_uv(lifecycle, monkeypatch, tmp_path, purge):
    home = tmp_path / '.kova'
    home.mkdir()
    monkeypatch.setattr(lifecycle, 'sys', SimpleNamespace(platform='win32', stdin=SimpleNamespace(isatty=lambda: False)))
    scheduled = Mock()
    monkeypatch.setattr(lifecycle, '_windows_uninstall', scheduled)
    monkeypatch.setattr(lifecycle.subprocess, 'run', Mock(side_effect=AssertionError('must defer uv until exit')))
    lifecycle.uninstall(purge=purge, keep_data=not purge, confirm=lambda _: False, emit=lambda _: None)
    scheduled.assert_called_once_with('/test/uv', home.absolute(), purge)
    assert home.exists()
