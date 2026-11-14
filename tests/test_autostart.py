"""Start-at-login writers, faked per platform with disposable files and a registry."""

import os
import plistlib
import subprocess
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from typer.testing import CliRunner

import kova.autostart as autostart
import kova.cli as cli
import kova.lifecycle as lifecycle


@pytest.fixture(autouse=True)
def portable_platform_fakes(monkeypatch):
    # Exercise each writer independently of the host running this test.
    monkeypatch.setattr(autostart, "_python", lambda python=None: "/opt/python")
    monkeypatch.setattr(os, "getuid", lambda: 1000, raising=False)


class Runner:
    """Record user-level helper calls and answer with one exit code."""

    def __init__(self, code=0):
        self.code = code
        self.calls = []

    def __call__(self, command, **kwargs):
        self.calls.append(list(command))
        return SimpleNamespace(returncode=self.code, stdout="", stderr="")


def _systemd_enable(home, khome, run, which=lambda name: f"/usr/bin/{name}"):
    return autostart.enable("linux", home, kova_home=khome, python="/opt/python",
                            run=run, which=which)


def test_linux_prefers_a_systemd_user_unit(tmp_path):
    home, khome = tmp_path / "home", tmp_path / "data"
    run = Runner()
    state = _systemd_enable(home, khome, run)

    unit = home / ".config/systemd/user/kova.service"
    assert unit.read_text() == (
        "[Unit]\n"
        "Description=Kova local AI server\n"
        "After=network.target\n"
        "\n"
        "[Service]\n"
        "Type=simple\n"
        "ExecStart=/opt/python -m kova serve\n"
        "Restart=on-failure\n"
        "RestartSec=5\n"
        f"Environment={autostart._systemd_arg('KOVA_HOME=' + str(khome))}\n"
        f"StandardOutput=append:{str(khome / 'server.log')}\n"
        f"StandardError=append:{str(khome / 'server.log')}\n"
        "\n"
        "[Install]\n"
        "WantedBy=default.target\n"
    )
    assert state.mechanism == "systemd"
    assert state.installed and state.enabled
    assert state.path == str(unit)
    assert run.calls == [
        ["systemctl", "--user", "show-environment"],
        ["systemctl", "--user", "daemon-reload"],
        ["systemctl", "--user", "enable", "kova.service"],
    ]
    assert not (home / ".config/autostart/kova.desktop").exists()


def test_linux_falls_back_to_an_autostart_desktop_entry(tmp_path):
    home, khome = tmp_path / "home", tmp_path / "data"
    run = Runner()
    state = _systemd_enable(home, khome, run, which=lambda name: None)

    entry = home / ".config/autostart/kova.desktop"
    assert entry.read_text() == (
        "[Desktop Entry]\n"
        "Type=Application\n"
        "Name=Kova\n"
        "Comment=Keep Kova's server running for reminders and phone access\n"
        "Exec=/opt/python -m kova serve\n"
        "Terminal=false\n"
        "X-GNOME-Autostart-enabled=true\n"
    )
    assert state.mechanism == "desktop"
    assert state.installed and state.enabled
    assert run.calls == []


def test_linux_on_is_idempotent_and_removes_the_other_mechanism(tmp_path):
    home, khome = tmp_path / "home", tmp_path / "data"
    fallback = home / ".config/autostart/kova.desktop"
    fallback.parent.mkdir(parents=True)
    fallback.write_text("old fallback\n")

    first = _systemd_enable(home, khome, Runner())
    unit = home / ".config/systemd/user/kova.service"
    contents = unit.read_text()
    second = _systemd_enable(home, khome, Runner())
    assert first == second
    assert unit.read_text() == contents
    assert not fallback.exists()


def test_linux_off_removes_only_kova_entries(tmp_path):
    home, khome = tmp_path / "home", tmp_path / "data"
    _systemd_enable(home, khome, Runner())
    autostart_path = home / ".config/autostart"
    autostart_path.mkdir(parents=True)
    other = autostart_path / "other.desktop"
    other.write_text("someone else's entry\n")
    run = Runner()

    state = autostart.disable("linux", home, run=run,
                              which=lambda name: f"/usr/bin/{name}")

    assert not (home / ".config/systemd/user/kova.service").exists()
    assert not (autostart_path / "kova.desktop").exists()
    assert other.read_text() == "someone else's entry\n"
    assert state.installed is False
    assert run.calls == [
        ["systemctl", "--user", "show-environment"],
        ["systemctl", "--user", "disable", "--now", "kova.service"],
    ]


def test_linux_status_reports_a_stale_unit_without_rewriting_it(tmp_path):
    home, khome = tmp_path / "home", tmp_path / "data"
    unit = home / ".config/systemd/user/kova.service"
    unit.parent.mkdir(parents=True)
    stale = "[Service]\nExecStart=/old/python -m kova serve\n"
    unit.write_text(stale)
    run = Runner()

    state = autostart.status("linux", home, kova_home=khome, python="/opt/python",
                             run=run, which=lambda name: f"/usr/bin/{name}")

    assert state.installed and state.stale and not state.enabled
    assert state.path == str(unit)
    assert unit.read_text() == stale
    assert run.calls == []


def test_linux_status_is_honest_when_nothing_is_installed(tmp_path):
    home, khome = tmp_path / "home", tmp_path / "data"
    run = Runner()
    state = autostart.status("linux", home, kova_home=khome, run=run,
                             which=lambda name: None)
    assert not state.installed and not state.stale
    assert state.mechanism == "desktop"
    assert "Start at login is off" in autostart.describe(state)


def test_macos_writes_a_launch_agent_plist(tmp_path):
    home, khome = tmp_path / "home", tmp_path / "data"
    run = Runner()
    state = autostart.enable("darwin", home, kova_home=khome, python="/opt/python",
                             run=run, which=lambda name: "/bin/launchctl")

    path = home / "Library/LaunchAgents/ai.kova.server.plist"
    data = plistlib.loads(path.read_bytes())
    assert data["Label"] == "ai.kova.server"
    assert data["ProgramArguments"] == ["/opt/python", "-m", "kova", "serve"]
    assert data["RunAtLoad"] is True
    assert data["EnvironmentVariables"] == {"KOVA_HOME": str(khome)}
    assert data["StandardOutPath"] == str(khome / "server.log")
    assert data["StandardErrorPath"] == str(khome / "server.log")
    assert run.calls == [
        ["launchctl", "bootstrap", f"gui/{os.getuid()}", str(path)],
    ]
    assert state.mechanism == "launchagent"
    assert state.installed and state.enabled


def test_macos_off_removes_the_launch_agent_and_nothing_else(tmp_path):
    home, khome = tmp_path / "home", tmp_path / "data"
    autostart.enable("darwin", home, kova_home=khome, python="/opt/python",
                     run=Runner(), which=lambda name: "/bin/launchctl")
    other = home / "Library/LaunchAgents/com.example.other.plist"
    other.write_text("other\n")
    run = Runner()

    state = autostart.disable("darwin", home, run=run,
                              which=lambda name: "/bin/launchctl")

    assert not (home / "Library/LaunchAgents/ai.kova.server.plist").exists()
    assert other.read_text() == "other\n"
    assert state.installed is False
    assert run.calls == [["launchctl", "bootout", f"gui/{os.getuid()}/ai.kova.server"]]


def test_macos_status_reports_a_stale_plist_without_rewriting_it(tmp_path):
    home, khome = tmp_path / "home", tmp_path / "data"
    path = home / "Library/LaunchAgents/ai.kova.server.plist"
    path.parent.mkdir(parents=True)
    stale = plistlib.dumps({"Label": "ai.kova.server", "RunAtLoad": True,
                            "ProgramArguments": ["/old/python", "-m", "kova", "serve"]})
    path.write_bytes(stale)
    run = Runner()

    state = autostart.status("macos", home, kova_home=khome, python="/opt/python",
                             run=run, which=lambda name: "/bin/launchctl")

    assert state.installed and state.stale and not state.enabled
    assert path.read_bytes() == stale
    assert run.calls == []


class _Key:
    def __init__(self):
        self.closed = False


class FakeRegistry:
    """The small part of winreg that autostart uses."""

    HKEY_CURRENT_USER = object()
    KEY_READ = 1
    KEY_SET_VALUE = 2
    REG_SZ = 1

    def __init__(self, values=None):
        self.values = dict(values or {})

    def OpenKey(self, root, path, reserved=0, access=0):
        assert root is self.HKEY_CURRENT_USER
        assert path == autostart.RUN_KEY
        return _Key()

    def CreateKeyEx(self, root, path, reserved=0, access=0):
        return self.OpenKey(root, path, reserved, access)

    def SetValueEx(self, key, name, reserved, kind, value):
        self.values[name] = (value, kind)

    def QueryValueEx(self, key, name):
        if name not in self.values:
            raise FileNotFoundError(name)
        return self.values[name]

    def DeleteValue(self, key, name):
        if name not in self.values:
            raise FileNotFoundError(name)
        del self.values[name]

    def CloseKey(self, key):
        key.closed = True


def test_windows_writes_the_per_user_run_key(tmp_path):
    registry = FakeRegistry()
    state = autostart.enable("win32", python="/opt/python", registry=registry)

    command = subprocess.list2cmdline(["/opt/python", "-m", "kova", "serve"])
    assert registry.values == {"Kova": (command, FakeRegistry.REG_SZ)}
    assert state.mechanism == "registry"
    assert state.installed and state.enabled
    assert state.path is None


def test_windows_off_deletes_only_kovas_value(tmp_path):
    registry = FakeRegistry({"Other": ("keep me", FakeRegistry.REG_SZ)})
    autostart.enable("win32", python="/opt/python", registry=registry)
    state = autostart.disable("win32", registry=registry)

    assert registry.values == {"Other": ("keep me", FakeRegistry.REG_SZ)}
    assert state.installed is False
    assert state.path is None


def test_windows_status_reports_a_stale_value_without_rewriting_it(tmp_path):
    registry = FakeRegistry({"Kova": ("old-python -m kova serve", FakeRegistry.REG_SZ)})
    state = autostart.status("win32", python="/opt/python", registry=registry)

    assert state.installed and state.stale and not state.enabled
    assert registry.values == {"Kova": ("old-python -m kova serve", FakeRegistry.REG_SZ)}


@pytest.mark.skipif(autostart.winreg is not None, reason="winreg exists on this platform")
def test_windows_without_winreg_refuses_clearly():
    with pytest.raises(autostart.AutostartError) as exc:
        autostart.enable("win32", registry=None)
    assert "winreg" in str(exc.value)


def test_unsupported_platform_refuses_clearly(tmp_path):
    with pytest.raises(autostart.AutostartError) as exc:
        autostart.enable("freebsd", tmp_path)
    assert str(exc.value) == (
        "Start at login isn't supported on freebsd; use your system's startup settings."
    )


def test_cli_on_off_and_status(monkeypatch, tmp_path):
    path = tmp_path / "kova.service"
    monkeypatch.setattr(cli.autostart, "enable",
                        lambda: autostart.AutostartStatus("linux", "systemd", True,
                                                          enabled=True, path=str(path)))
    monkeypatch.setattr(cli.autostart, "disable",
                        lambda: autostart.AutostartStatus("linux", "systemd", False))
    monkeypatch.setattr(cli.autostart, "status",
                        lambda: autostart.AutostartStatus("linux", "systemd", True,
                                                          enabled=True, running=True,
                                                          path=str(path)))
    runner = CliRunner()
    monkeypatch.setenv("COLUMNS", "1000")

    on = runner.invoke(cli.app, ["autostart", "on"])
    assert on.exit_code == 0, on.output
    assert "Start at login is on (systemd user unit)." in on.output
    assert str(path) in on.output.replace("\n", "")
    assert "Kova uses a little memory while idle" in on.output

    off = runner.invoke(cli.app, ["autostart", "off"])
    assert off.exit_code == 0, off.output
    assert "Start at login is off" in off.output

    status = runner.invoke(cli.app, ["autostart", "status"])
    assert status.exit_code == 0, status.output
    assert "Start at login is on (systemd user unit, enabled, running now)." in status.output

    shown = runner.invoke(cli.app, ["autostart"])
    assert shown.exit_code == 0, shown.output
    assert "Start at login is on" in shown.output


def test_cli_status_reports_stale_without_rewriting(monkeypatch, tmp_path):
    path = tmp_path / "kova.service"
    monkeypatch.setattr(cli.autostart, "status",
                        lambda: autostart.AutostartStatus(
                            "linux", "systemd", True, stale=True, path=str(path),
                            detail="the unit does not match this Kova install"))
    result = CliRunner().invoke(cli.app, ["autostart", "status"])
    assert result.exit_code == 0, result.output
    assert "Start at login is stale" in result.output
    assert "Nothing was changed." in result.output


def test_uninstall_removes_the_login_entry(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setenv("KOVA_HOME", str(tmp_path / ".kova"))
    monkeypatch.setattr(lifecycle, "find_uv", lambda: "/test/uv")
    monkeypatch.setattr(lifecycle.subprocess, "run",
                        Mock(return_value=subprocess.CompletedProcess([], 0)))
    monkeypatch.setattr(autostart, "_systemctl_answers", lambda *_: False)
    monkeypatch.setattr(autostart, "_platform_name", lambda platform=None: "linux")
    unit = tmp_path / ".config/systemd/user/kova.service"
    unit.parent.mkdir(parents=True)
    unit.write_text("old unit\n")

    result = CliRunner().invoke(cli.app, ["uninstall", "--keep-data"])

    assert result.exit_code == 0, result.output
    assert not unit.exists()


def test_cli_failure_is_one_line_and_nonzero(monkeypatch):
    def unavailable():
        raise autostart.AutostartError(
            "Start at login isn't supported on plan9; use your system's startup settings."
        )
    monkeypatch.setattr(cli.autostart, "enable", unavailable)
    result = CliRunner().invoke(cli.app, ["autostart", "on"])
    assert result.exit_code == 1
    assert result.output.strip() == (
        "✗ Start at login isn't supported on plan9; use your system's startup settings."
    )
    assert "Traceback" not in result.output
