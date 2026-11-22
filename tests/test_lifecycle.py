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
    monkeypatch.setattr(lifecycle.egress, "get", get)


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
    monkeypatch.setattr(lifecycle.egress, "get", Mock(side_effect=httpx.ConnectError("offline")))
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
    assert args[4:7] == ["--refresh", "--upgrade-package", "kova-ai"]


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
    monkeypatch.setattr(lifecycle, "verified_server", lambda pid, home: SimpleNamespace(send_signal=lambda sig: lifecycle.os.kill(pid, sig)))
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


def test_cli_uninstall_removes_linux_menu_entry(lifecycle, monkeypatch, tmp_path):
    entry = tmp_path / ".local/share/applications/kova.desktop"
    icon = tmp_path / ".local/share/icons/hicolor/512x512/apps/kova.png"
    for item in (entry, icon):
        item.parent.mkdir(parents=True, exist_ok=True)
        item.write_text("installed")
    monkeypatch.setattr(lifecycle.subprocess, "run", Mock(return_value=subprocess.CompletedProcess([], 0)))
    lifecycle.uninstall(purge=False, keep_data=True, confirm=lambda _: False)
    assert not entry.exists()
    assert not icon.exists()


def test_cli_uninstall_removes_macos_launcher(lifecycle, monkeypatch, tmp_path):
    bundle = tmp_path / "Applications/Kova.app"
    (bundle / "Contents/MacOS").mkdir(parents=True)
    (bundle / "Contents/Info.plist").write_text("launcher")
    monkeypatch.setattr(lifecycle, "sys", SimpleNamespace(platform="darwin", stdin=sys.stdin))
    monkeypatch.setattr(lifecycle.subprocess, "run", Mock(return_value=subprocess.CompletedProcess([], 0)))
    lifecycle.uninstall(purge=False, keep_data=True, confirm=lambda _: False)
    assert not bundle.exists()


def test_windows_cli_uninstall_helper_removes_start_menu_shortcut(lifecycle, monkeypatch, tmp_path):
    import base64

    launch = Mock()
    monkeypatch.setattr(lifecycle.subprocess, "Popen", launch)
    monkeypatch.setattr(lifecycle.subprocess, "CREATE_NEW_PROCESS_GROUP", 0, raising=False)
    lifecycle._windows_uninstall("uv.exe", tmp_path / ".kova", False)
    encoded = launch.call_args.args[0][-1]
    script = base64.b64decode(encoded).decode("utf-16le")
    assert "Join-Path $env:APPDATA 'Microsoft\\Windows\\Start Menu\\Programs\\Kova.lnk'" in script
    assert "Remove-Item -LiteralPath" in script


def test_update_refreshes_existing_desktop_with_new_launcher(lifecycle, monkeypatch):
    desktop = lifecycle.kova_home() / "desktop"
    desktop.mkdir(parents=True)
    (desktop / "package.json").write_text("{}")
    run = Mock()
    monkeypatch.setattr(lifecycle.subprocess, "run", run)
    result = CliRunner().invoke(app, ["update"])
    assert result.exit_code == 0, result.output
    install, prepare = run.call_args_list
    assert install.args[0][:3] == ["/test/uv", "tool", "install"]
    launcher = "kova.exe" if lifecycle.os.name == "nt" else "kova"
    assert prepare.args[0] == [str(lifecycle.Path(prepare.kwargs["env"]["UV_TOOL_BIN_DIR"]) / launcher),
                               "app", "--prepare"]


@pytest.fixture
def installed_desktop(lifecycle, monkeypatch):
    from kova import app as launcher
    from kova import desktop_registration

    entry = lifecycle.Path.home() / ".local/share/applications/kova.desktop"
    entry.parent.mkdir(parents=True)
    entry.write_text("[Desktop Entry]\nName=Kova\nExec=/old/bin/kova-app\nType=Application\nStartupWMClass=Kova\n")
    desktop = lifecycle.kova_home() / "desktop"
    cli = lifecycle.Path(lifecycle.tool_env()["UV_TOOL_BIN_DIR"]) / (
        "kova.exe" if lifecycle.os.name == "nt" else "kova"
    )
    commands = []
    monkeypatch.setattr(desktop_registration, "sys", SimpleNamespace(platform="linux"))
    monkeypatch.setattr(launcher, "refresh_desktop", lambda home: desktop)
    monkeypatch.setenv("UV_TOOL_BIN_DIR", str(cli.parent))
    monkeypatch.setattr(lifecycle.shutil, "which", lambda name: f"/test/{name}")

    def run(command, **kwargs):
        commands.append(command)
        if command[0] == "/test/uv":
            cli.parent.mkdir(parents=True, exist_ok=True)
            cli.touch()
            (cli.parent / "kova").touch()  # the registration side simulates Linux
        elif command[0] == str(cli):
            launcher.main(command[2:])
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(lifecycle.subprocess, "run", run)
    return entry, cli, commands


def test_update_rewrites_menu_to_the_current_installed_command(installed_desktop):
    entry, cli, _ = installed_desktop
    result = CliRunner().invoke(app, ["update"])
    assert result.exit_code == 0, result.output
    assert cli.is_file()
    exec_line = next(line for line in entry.read_text().splitlines() if line.startswith("Exec="))
    # A path with spaces or Windows separators needs desktop-entry quoting.
    assert exec_line.replace('"', '').replace('\\\\', '\\') == f'Exec={cli.with_name("kova")} app'
    assert "StartupWMClass=kova\n" in entry.read_text()


@pytest.mark.skipif(sys.platform != "linux", reason="Linux desktop entry paths")
@pytest.mark.parametrize("legacy", [False, True])
def test_update_preserves_the_menu_data_home_without_an_env_override(
    installed_desktop, monkeypatch, tmp_path, legacy
):
    entry, cli, _ = installed_desktop
    data = tmp_path / "custom data"
    old_exec = f'"{data}/desktop/launch"' if legacy else f'{cli} app --data-home "{data}"'
    entry.write_text(f"[Desktop Entry]\nName=Kova\nExec={old_exec}\n")
    monkeypatch.delenv("KOVA_HOME")

    result = CliRunner().invoke(app, ["update"])

    assert result.exit_code == 0, result.output
    assert f'Exec={cli} app --data-home "{data}"' in entry.read_text()


def test_update_refreshes_all_installed_icons_and_caches(installed_desktop):
    from kova import app as launcher

    entry, _, commands = installed_desktop
    icons = entry.parent.parent / "icons/hicolor"
    current = launcher._bundled_desktop() / "assets"
    stale = icons / "512x512/apps/kova.png"
    stale.parent.mkdir(parents=True)
    stale.write_bytes(b"old icon")
    result = CliRunner().invoke(app, ["update"])
    assert result.exit_code == 0, result.output
    assert stale.read_bytes() == (current / "icon.png").read_bytes()
    assert (icons / "scalable/apps/kova.svg").read_bytes() == (current / "icon.svg").read_bytes()
    for size in (16, 22, 24, 32, 36, 48, 64, 72, 96, 128, 192, 256):
        installed = icons / f"{size}x{size}/apps/kova.png"
        assert installed.read_bytes() == (current / f"icon-{size}.png").read_bytes()
    assert ["/test/gtk-update-icon-cache", "-f", "-t", str(icons)] in commands
    assert ["/test/kbuildsycoca6"] in commands
    assert ["/test/xdg-desktop-menu", "forceupdate"] in commands


def test_kova_app_prepare_refreshes_desktop(monkeypatch):
    from kova import app as app_module
    called = []
    monkeypatch.setattr(app_module, "refresh_desktop", lambda home: called.append(home))
    result = CliRunner().invoke(app, ["app", "--prepare"])
    assert result.exit_code == 0, result.output
    assert len(called) == 1
