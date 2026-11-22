"""User-level installation lifecycle, without touching a real uv tool or server."""

import json
import signal
import shutil
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import Mock

import httpx
import pytest
from typer.testing import CliRunner

from kova import build, desktop_integration, updater
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
    monkeypatch.setattr(lifecycle.egress, "get", Mock(side_effect=httpx.ConnectError("offline")))
    monkeypatch.setattr(updater.runtime, "running_server", lambda cfg: None)
    monkeypatch.setattr(updater.runtime, "running_desktop", lambda home: None)
    monkeypatch.setattr(updater.autostart, "status", lambda **_: updater.autostart.Status("linux", "desktop", False))
    monkeypatch.setattr(desktop_integration, "sys", SimpleNamespace(platform="linux"))
    monkeypatch.setattr(lifecycle, "sys", SimpleNamespace(platform="linux", stdin=sys.stdin))
    return lifecycle


def install_metadata(lifecycle, monkeypatch, commit=None):
    origin = {"url": "https://github.com/wynxo/Kova"}
    if commit:
        origin["vcs_info"] = {"vcs": "git", "commit_id": commit}
    dist = Mock(version="0.1.0")
    dist.read_text.side_effect = lambda file: json.dumps(origin) if file == "direct_url.json" else None
    monkeypatch.setattr(build.metadata, "distribution", lambda name: dist)


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
    assert not lifecycle.kova_home().exists()


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


def write_tool(version, *, source_commit=None):
    tool = build.tool_dir()
    dist = tool / f"lib/python3.12/site-packages/kova_ai-{version}.dist-info"
    dist.mkdir(parents=True, exist_ok=True)
    (dist / "METADATA").write_text(f"Name: kova-ai\nVersion: {version}\n")
    (dist / "RECORD").write_text(f"kova/cli.py,sha256={version},1\n")
    if source_commit:
        (dist / "kova-source-commit").write_text(source_commit)
    return dist


def fake_install(lifecycle, monkeypatch):
    """Record commands, have uv create tool metadata and make CLI report it."""
    calls = []

    def run(args, **options):
        calls.append(([str(arg) for arg in args], options))
        if args[0] == "/test/uv":
            # uv --force replaces the whole tool environment; never leave the old dist-info beside the new one.
            shutil.rmtree(build.tool_dir() / "lib", ignore_errors=True)
            write_tool("0.2.0")
            launcher = updater.Path(options["env"]["UV_TOOL_BIN_DIR"]) / ("kova.exe" if updater.os.name == "nt" else "kova")
            launcher.parent.mkdir(parents=True, exist_ok=True)
            launcher.write_text("new launcher")
        output = "Kova v0.2.0" if args[-1] == "--version" else ""
        return subprocess.CompletedProcess(args, 0, output)

    monkeypatch.setattr(updater.subprocess, "run", run)
    return calls


@pytest.mark.parametrize("git", [True, False])
def test_update_uses_selected_ref_and_archive_without_git(lifecycle, monkeypatch, git):
    monkeypatch.setattr(lifecycle.shutil, "which", lambda name: "/test/git" if git else None)
    calls = fake_install(lifecycle, monkeypatch)
    result = CliRunner().invoke(app, ["update", "--ref", "v0.2.0"])
    assert result.exit_code == 0, result.output
    args, opts = calls[0]
    assert args[:4] == ["/test/uv", "tool", "install", "--force"]
    assert args[-1] == ("kova-ai @ git+https://github.com/wynxo/Kova@v0.2.0" if git else
                        "kova-ai @ https://github.com/wynxo/Kova/archive/v0.2.0.tar.gz")
    assert args[4:7] == ["--refresh", "--upgrade-package", "kova-ai"]
    assert opts["capture_output"] is True
    assert not lifecycle.kova_home().exists()


def test_update_preview_shows_short_commit_subjects(lifecycle, monkeypatch):
    write_tool("0.1.0", source_commit="a" * 40)
    remote(lifecycle, monkeypatch, version="0.2.0")
    get = lifecycle.egress.get
    def request(url, **kwargs):
        if "/compare/" in url:
            assert ("a" * 40 + "..." + "b" * 40) in url
            return httpx.Response(200, json={"commits": [{"commit": {"message": "Improve update\n\nLong body"}},
                                                          {"commit": {"message": "Refresh desktop"}}]},
                                  request=httpx.Request("GET", url))
        return get(url, **kwargs)
    monkeypatch.setattr(lifecycle.egress, "get", request)
    calls = fake_install(lifecycle, monkeypatch)
    result = CliRunner().invoke(app, ["update", "--verbose"])
    assert result.exit_code == 0, result.output
    assert "0.1.0 (aaaaaaaaaaaa) → 0.2.0 (bbbbbbbbbbbb)" in result.output
    assert "Improve update" in result.output and "Refresh desktop" in result.output
    assert "Long body" not in result.output
    assert calls[0][1]["capture_output"] is False


def test_update_uv_failure_rolls_back_tool_and_does_not_interrupt_old_server(lifecycle, monkeypatch):
    monkeypatch.setattr(updater, "sys", SimpleNamespace(platform="linux"))
    dist = write_tool("0.1.0")
    bin_dir = updater.Path(lifecycle.tool_env()["UV_TOOL_BIN_DIR"])
    bin_dir.mkdir(parents=True)
    launcher = bin_dir / ("kova.exe" if updater.os.name == "nt" else "kova")
    launcher.write_text("old launcher")
    stop = Mock()
    monkeypatch.setattr(updater.runtime, "running_server", lambda cfg: object())
    monkeypatch.setattr(updater.runtime, "stop", stop)
    def fail(args, **options):
        (dist / "METADATA").unlink()
        launcher.write_text("broken launcher")
        raise subprocess.CalledProcessError(1, "uv", stderr="network failed")
    monkeypatch.setattr(lifecycle.subprocess, "run", fail)
    result = CliRunner().invoke(app, ["update"])
    assert result.exit_code == 1
    assert "network failed" in result.output and "Run: kova update" in result.output
    assert build.tool_build().version == "0.1.0"
    assert launcher.read_text() == "old launcher"
    stop.assert_not_called()


def test_windows_worker_recovers_the_running_install_when_uv_fails(lifecycle, monkeypatch):
    monkeypatch.setattr(updater, "sys", SimpleNamespace(platform="win32"))
    monkeypatch.setattr(updater, "_check_windows_tool_idle", lambda: None)
    monkeypatch.setattr(build, "managed", lambda: False)
    old = write_tool("0.1.0")
    server, desktop = object(), object()
    monkeypatch.setattr(updater.runtime, "running_server", lambda cfg: server)
    monkeypatch.setattr(updater.runtime, "running_desktop", lambda home: desktop)
    stop, start_server, start_desktop = Mock(), Mock(), Mock()
    monkeypatch.setattr(updater.runtime, "stop", stop)
    monkeypatch.setattr(updater.runtime, "start_server", start_server)
    monkeypatch.setattr(updater.runtime, "start_desktop", start_desktop)

    def fail(*args, **kwargs):
        (old / "METADATA").unlink()
        raise subprocess.CalledProcessError(1, "uv", stderr="replacement failed")

    monkeypatch.setattr(lifecycle.subprocess, "run", fail)
    result = CliRunner().invoke(app, ["update"])
    assert result.exit_code == 1, result.output
    assert "replacement failed" in result.output
    assert [call.args[0] for call in stop.call_args_list] == [server, desktop]
    assert start_server.call_args.args[1].version == "0.1.0"
    start_desktop.assert_called_once()
    assert build.tool_build().version == "0.1.0"


def test_windows_installed_cli_defers_update_until_launcher_exits(lifecycle, monkeypatch):
    monkeypatch.setattr(updater, "sys", SimpleNamespace(platform="win32"))
    monkeypatch.setattr(build, "managed", lambda: True)
    defer = Mock()
    monkeypatch.setattr(updater, "_defer_windows", defer)
    result = CliRunner().invoke(app, ["update", "--verbose"])
    assert result.exit_code == 0, result.output
    defer.assert_called_once_with("main", True)
    assert "separate console" in result.output


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
    icons = tmp_path / ".local/share/icons/hicolor"
    installed = (entry, icons / "512x512/apps/kova.png", icons / "16x16/apps/kova.png",
                 icons / "scalable/apps/kova.svg")
    for item in installed:
        item.parent.mkdir(parents=True, exist_ok=True)
        item.write_text("installed")
    monkeypatch.setattr(lifecycle.subprocess, "run", Mock(return_value=subprocess.CompletedProcess([], 0)))
    lifecycle.uninstall(purge=False, keep_data=True, confirm=lambda _: False)
    assert not any(item.exists() for item in installed)


def test_cli_uninstall_removes_macos_launcher(lifecycle, monkeypatch, tmp_path):
    bundle = tmp_path / "Applications/Kova.app"
    (bundle / "Contents/MacOS").mkdir(parents=True)
    (bundle / "Contents/Info.plist").write_text("launcher")
    monkeypatch.setattr(desktop_integration, "sys", SimpleNamespace(platform="darwin"))
    monkeypatch.setattr(lifecycle, "sys", SimpleNamespace(platform="darwin", stdin=sys.stdin))
    monkeypatch.setattr(lifecycle.subprocess, "run", Mock(return_value=subprocess.CompletedProcess([], 0)))
    lifecycle.uninstall(purge=False, keep_data=True, confirm=lambda _: False)
    assert not bundle.exists()


def test_windows_cli_uninstall_helper_removes_start_menu_shortcut(lifecycle, monkeypatch, tmp_path):
    import base64

    monkeypatch.setattr(desktop_integration, "sys", SimpleNamespace(platform="win32"))
    monkeypatch.setenv("APPDATA", str(tmp_path / "Roaming"))
    launch = Mock()
    monkeypatch.setattr(lifecycle.subprocess, "Popen", launch)
    monkeypatch.setattr(lifecycle.subprocess, "CREATE_NEW_PROCESS_GROUP", 0, raising=False)
    lifecycle._windows_uninstall("uv.exe", tmp_path / ".kova", False)
    encoded = launch.call_args.args[0][-1]
    script = base64.b64decode(encoded).decode("utf-16le")
    assert f"Remove-Item -LiteralPath '{desktop_integration.menu_path()}'" in script


@pytest.mark.parametrize("evidence", ["stage directory", "desktop profile", "menu entry"])
def test_update_uses_new_package_even_if_the_desktop_manifest_is_missing(lifecycle, monkeypatch, evidence):
    home = lifecycle.kova_home()
    if evidence == "stage directory":
        (home / "desktop").mkdir(parents=True)
    elif evidence == "desktop profile":
        (home / "desktop-data").mkdir(parents=True)
    else:
        menu = desktop_integration.menu_path()
        menu.parent.mkdir(parents=True)
        menu.write_text("Exec=kova-app")
    calls = fake_install(lifecycle, monkeypatch)
    result = CliRunner().invoke(app, ["update"])
    assert result.exit_code == 0, result.output
    assert [args[-1] for args, _ in calls[1:]] == ["--version", "migrate", "stage", "register", "verify-desktop"]
    for args, options in calls[2:]:
        assert args[:3] == [str(build.tool_python()), "-m", "kova.updater"]
        assert options["env"]["KOVA_HOME"] == str(home)


@pytest.fixture
def legacy_desktop(lifecycle, monkeypatch):
    """The old updater runs the newly installed kova-app --prepare entry point."""
    from kova import app as launcher

    entry = desktop_integration.menu_path()
    entry.parent.mkdir(parents=True)
    entry.write_text("[Desktop Entry]\nName=Kova\nExec=/old/bin/kova-app\nType=Application\nStartupWMClass=Kova\n")
    bin_dir = lifecycle.Path(lifecycle.tool_env()["UV_TOOL_BIN_DIR"])
    bin_dir.mkdir(parents=True)
    cli = bin_dir / "kova"
    cli.touch()
    commands = []
    staged = []
    monkeypatch.setattr(launcher, "refresh_desktop", lambda home: staged.append(home))
    monkeypatch.setenv("UV_TOOL_BIN_DIR", str(bin_dir))
    monkeypatch.setattr(lifecycle.shutil, "which", lambda name: f"/test/{name}")

    def run(command, **kwargs):
        commands.append(command)
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(lifecycle.subprocess, "run", run)
    return entry, cli, commands, staged


@pytest.mark.skipif(sys.platform != "linux", reason="Linux desktop entry paths")
def test_old_updater_repairs_the_menu_and_refreshes_icons(legacy_desktop):
    from kova import app as launcher

    entry, cli, commands, staged = legacy_desktop
    icons = entry.parent.parent / "icons/hicolor"
    current = launcher._bundled_desktop() / "assets"
    stale = icons / "512x512/apps/kova.png"
    stale.parent.mkdir(parents=True)
    stale.write_bytes(b"old icon")
    launcher.main(["--prepare"])
    home, = staged
    script = home / "desktop/launch"
    assert f"Exec={script}" in entry.read_text()
    assert str(cli) in script.read_text()
    assert "StartupWMClass=kova\n" in entry.read_text()
    assert stale.read_bytes() == (current / "icon.png").read_bytes()
    assert (icons / "scalable/apps/kova.svg").read_bytes() == (current / "icon.svg").read_bytes()
    for size in (16, 22, 24, 32, 36, 48, 64, 72, 96, 128, 192, 256):
        installed = icons / f"{size}x{size}/apps/kova.png"
        assert installed.read_bytes() == (current / f"icon-{size}.png").read_bytes()
    assert ["/test/gtk-update-icon-cache", "-f", "-t", str(icons)] in commands
    assert ["/test/kbuildsycoca6"] in commands
    assert ["/test/xdg-desktop-menu", "forceupdate"] in commands


@pytest.mark.skipif(sys.platform != "linux", reason="Linux desktop entry paths")
@pytest.mark.parametrize("legacy", [False, True])
def test_update_preserves_the_menu_data_home_without_an_env_override(
    lifecycle, legacy_desktop, monkeypatch, tmp_path, legacy
):
    entry, cli, _, staged = legacy_desktop
    data = tmp_path / "custom data"
    old_exec = f'"{data}/desktop/launch"' if legacy else f'{cli} app --data-home "{data}"'
    entry.write_text(f"[Desktop Entry]\nName=Kova\nExec={old_exec}\n")
    monkeypatch.delenv("KOVA_HOME")

    from kova import app as launcher
    launcher.main(["--prepare"])
    assert staged == [data]
    assert f'Exec="{data}/desktop/launch"' in entry.read_text()

    calls = fake_install(lifecycle, monkeypatch)
    result = CliRunner().invoke(app, ["update"])
    assert result.exit_code == 0, result.output
    assert [args[-1] for args, _ in calls[2:]] == ["migrate", "stage", "register", "verify-desktop"]
    assert all(options["env"]["KOVA_HOME"] == str(data) for _, options in calls[2:])


def test_update_failure_in_desktop_still_refreshes_login_and_restarts(lifecycle, monkeypatch):
    server, desktop = object(), object()
    monkeypatch.setattr(updater.runtime, "running_server", lambda cfg: server)
    monkeypatch.setattr(updater.runtime, "running_desktop", lambda home: desktop)
    monkeypatch.setattr(updater.autostart, "status", lambda **_: updater.autostart.Status("linux", "desktop", True, enabled=True))
    calls = fake_install(lifecycle, monkeypatch)
    run = updater.subprocess.run
    def fail_stage(args, **kwargs):
        if args[-1] == "stage":
            raise subprocess.CalledProcessError(1, "prepare", stderr="npm ci failed")
        return run(args, **kwargs)
    monkeypatch.setattr(updater.subprocess, "run", fail_stage)
    stop, start_server, start_desktop, verify = Mock(), Mock(), Mock(), Mock()
    monkeypatch.setattr(updater.runtime, "stop", stop)
    monkeypatch.setattr(updater.runtime, "start_server", start_server)
    monkeypatch.setattr(updater.runtime, "start_desktop", start_desktop)
    monkeypatch.setattr(updater.runtime, "verify_server", verify)
    result = CliRunner().invoke(app, ["update"])
    assert result.exit_code == 1, result.output
    assert [call.args[0] for call in stop.call_args_list] == [server, desktop]
    assert start_server.call_args.args[1].version == "0.2.0"
    assert verify.call_args.args[1].fingerprint == start_server.call_args.args[1].fingerprint
    start_desktop.assert_called_once()
    assert [args[-1] for args, _ in calls[1:]] == ["--version", "migrate", "login", "verify-desktop"]
    assert "npm ci failed" in result.output and "previous desktop kept" in result.output
    assert "✓ Restart server and wait for /health" in result.output


def test_cached_hint_is_opt_out_and_only_once_a_day(lifecycle, monkeypatch):
    cfg = updater.load_config()
    monkeypatch.setattr(build, "managed", lambda: True)
    monkeypatch.setattr(build, "installed", lambda: build.Build("0.1.0", "a" * 40))
    look_up = Mock(return_value=build.Build("0.1.0", "b" * 40))
    monkeypatch.setattr(lifecycle, "remote_build", look_up)
    cfg.update_hint = False
    assert lifecycle.update_hint(cfg) is None
    look_up.assert_not_called()
    cfg.update_hint = True
    assert "Run: kova update" in lifecycle.update_hint(cfg)
    assert lifecycle.update_hint(cfg) is None
    look_up.assert_called_once()


def test_kova_app_prepare_refreshes_desktop(monkeypatch):
    from kova import app as app_module
    called = []
    monkeypatch.setattr(app_module, "refresh_desktop", lambda home: called.append(home))
    result = CliRunner().invoke(app, ["app", "--prepare"])
    assert result.exit_code == 0, result.output
    assert len(called) == 1
