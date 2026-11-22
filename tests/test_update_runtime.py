"""Updater integration on fake processes, launchers, and an in-process server."""

import plistlib
import sqlite3
import subprocess
import time
from types import SimpleNamespace
from unittest.mock import Mock

import psutil
import pytest
from fastapi.testclient import TestClient

from kova import build, desktop_integration as integration, server, update_runtime as runtime, updater
from kova.app import DesktopError, refresh_desktop
from kova.config import load_config


def test_fresh_desktop_rolls_back_failed_npm_and_replaces_stale_tree(tmp_path, monkeypatch):
    from kova import app

    source = tmp_path / "package"
    source.mkdir()
    (source / "package-lock.json").write_text("new lock")
    (source / "package.json").write_text("new manifest")
    home = tmp_path / "data"
    old = home / "desktop"
    old.mkdir(parents=True)
    (old / "main.js").write_text("stale")
    (old / "launch").write_text("legacy launcher")
    (home / "desktop-data").mkdir()
    monkeypatch.setattr(app, "_bundled_desktop", lambda: source)
    install = Mock(return_value=1)
    monkeypatch.setattr(app, "_run", install)

    with pytest.raises(DesktopError, match="npm ci failed"):
        refresh_desktop(home, "/test/npm", fresh=True)
    assert (old / "main.js").read_text() == "stale"
    assert (old / "launch").read_text() == "legacy launcher"

    def npm_ci(command, candidate):
        assert "ci" in command
        assert candidate != old
        binary = app._electron_binary(candidate)
        binary.parent.mkdir(parents=True)
        binary.write_text("new dependency")
        return 0

    install.side_effect = npm_ci
    assert refresh_desktop(home, "/test/npm", fresh=True) == old
    assert not (old / "main.js").exists()
    assert (old / "package.json").read_text() == "new manifest"
    assert app._electron_binary(old).read_text() == "new dependency"
    assert (old / "launch").read_text() == "legacy launcher"
    assert (home / "desktop-data").is_dir()


@pytest.mark.parametrize("platform", ["linux", "darwin", "win32"])
def test_refresh_desktop_registration_updates_native_icons_and_launcher(tmp_path, monkeypatch, platform):
    monkeypatch.setattr(integration, "sys", SimpleNamespace(platform=platform))
    monkeypatch.setattr(integration.Path, "home", lambda: tmp_path)
    monkeypatch.setenv("APPDATA", str(tmp_path / "app-data"))
    project = tmp_path / "Kova Data" / "desktop"
    assets = project / "assets"
    assets.mkdir(parents=True)
    for size, icon in integration.ICON_FILES.items():
        (assets / icon).write_bytes(f"test-icon-{size}".encode())
    (assets / "icon.svg").write_text("<svg/>")
    binary = project / "electron"
    binary.touch()
    monkeypatch.setattr(integration, "_electron_binary", lambda project: binary)
    launcher = tmp_path / "Kova Tools" / "kova"
    fake_subprocess = Mock(return_value=subprocess.CompletedProcess([], 0))
    monkeypatch.setattr(integration.subprocess, "run", fake_subprocess)
    monkeypatch.setattr(integration.shutil, "which", lambda tool: tool)
    menu = integration.menu_path()

    if platform == "linux":
        menu.parent.mkdir(parents=True)
        menu.write_text("[Desktop Entry]\nExec=kova-app\n")

    integration.refresh_desktop_integration(project.parent, launcher)
    if platform != "win32":
        assert (project / "launch").is_file()
        assert str(launcher) in (project / "launch").read_text()
    if platform != "win32":
        integration.verify_desktop_integration(project.parent, launcher)
    if platform == "linux":
        entry = menu.read_text()
        assert f'Exec="{project / "launch"}"' in entry
        assert "kova-app" not in entry
        assert fake_subprocess.call_count == 2
    elif platform == "darwin":
        contents = menu / "Contents"
        assert plistlib.loads((contents / "Info.plist").read_bytes())["CFBundleName"] == "Kova"
        assert b"test-icon-512" in (contents / "Resources/kova.icns").read_bytes()
        assert fake_subprocess.call_count == 1
    else:
        # Windows PowerShell is simulated; the generated ICO is real.
        assert b"test-icon-256" in (assets / "kova.ico").read_bytes()
        link_code = fake_subprocess.call_args.args[0][-1]
        assert f"--kova-home={project.parent}" in link_code
        assert f"--kova-cli={launcher}" in link_code
        assert "IconLocation" in link_code
        menu.parent.mkdir(parents=True, exist_ok=True)
        menu.touch()
        integration.verify_desktop_integration(project.parent, launcher)


def test_existing_config_and_database_are_migrated_losslessly(tmp_path):
    config = tmp_path / "config.toml"
    config.write_text("# keep my comment\nschema_version = 1\n[updates]\nhint = false\n[future_setting]\nkeep = true\n")
    db = tmp_path / "kova.db"
    with sqlite3.connect(db) as conn:
        conn.executescript(
            "CREATE TABLE chats (id TEXT PRIMARY KEY, title TEXT, created TEXT NOT NULL);"
            "INSERT INTO chats VALUES ('legacy', 'Old chat', '2026-01-01T00:00:00+00:00');"
        )
    updater._migrate(tmp_path)
    assert f"schema_version = {updater.CONFIG_SCHEMA}" in config.read_text()
    assert "# keep my comment" in config.read_text()
    assert "[future_setting]\nkeep = true" in config.read_text()
    assert not load_config(tmp_path, read_only=True).update_hint
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT title, mode, web FROM chats WHERE id = 'legacy'").fetchone() == ("Old chat", "work", 1)


def test_server_discovery_does_not_use_unowned_listener(tmp_path, monkeypatch):
    cfg = load_config(tmp_path)
    (tmp_path / "server.pid").write_text("200")
    other = SimpleNamespace(status=psutil.CONN_LISTEN, pid=220, laddr=SimpleNamespace(port=9000))
    match = SimpleNamespace(status=psutil.CONN_LISTEN, pid=240, laddr=SimpleNamespace(port=cfg.port))
    monkeypatch.setattr(runtime.psutil, "net_connections", lambda **_: [other, match])
    process = Mock()
    verify = Mock(side_effect=lambda pid, home: process if pid == 240 and home == tmp_path else None)
    monkeypatch.setattr(runtime.lifecycle, "verified_server", verify)
    assert runtime.running_server(cfg) is process
    assert [call.args[0] for call in verify.call_args_list] == [200, 240]


def test_server_restart_waits_until_new_build_reports_healthy(tmp_path, monkeypatch):
    cfg = load_config(tmp_path)
    expected = build.Build("0.1.0", fingerprint="new")
    unavailable = runtime.KovaError(None, "not listening")
    health = Mock(side_effect=[unavailable, unavailable, {"version": "0.1.0", "build": "new"}])
    spawn = Mock()
    monkeypatch.setattr(runtime, "health", health)
    monkeypatch.setattr(runtime, "_spawn_server", spawn)
    monkeypatch.setattr(runtime.time, "sleep", lambda _: None)
    runtime.start_server(cfg, expected)
    spawn.assert_called_once_with(cfg, python=build.tool_python())
    assert health.call_count == 3
    health.side_effect = None
    health.return_value = {"version": "0.1.0", "build": "old"}
    with pytest.raises(runtime.lifecycle.LifecycleError, match="old build"):
        runtime.verify_server(cfg, expected)


@pytest.mark.parametrize("busy, owned, expected", [(True, True, False), (False, False, False), (False, True, True)])
def test_desktop_start_restarts_only_an_idle_owned_server(tmp_path, monkeypatch, busy, owned, expected):
    monkeypatch.setattr(runtime.build, "managed", lambda: True)
    monkeypatch.setattr(runtime.build, "tool_dir", lambda: tmp_path / "tool")
    monkeypatch.setattr(runtime.build, "installed", lambda: build.Build("0.1.0", fingerprint="new"))
    monkeypatch.setattr(runtime, "load_config", lambda **_: load_config(tmp_path))
    monkeypatch.setattr(runtime, "health", lambda cfg: {"version": "0.1.0", "build": "old", "busy": busy})
    process = object()
    monkeypatch.setattr(runtime, "running_server", lambda cfg: process if owned else None)
    stop, start = Mock(), Mock()
    monkeypatch.setattr(runtime, "stop", stop)
    monkeypatch.setattr(runtime, "start_server", start)

    assert runtime.restart_stale_server() is expected
    if expected:
        stop.assert_called_once_with(process)
        assert start.call_args.args[1].fingerprint == "new"
    else:
        stop.assert_not_called()
        start.assert_not_called()


def test_background_server_detects_same_version_new_build_and_web_cache_is_versioned(tmp_path, monkeypatch):
    running = build.Build("0.1.0", fingerprint="a" * 64)
    latest = Mock(return_value=running)
    monkeypatch.setattr(server, "INSTALLED_BUILD_POLL_SECONDS", 0.01)
    monkeypatch.setattr(build, "installed", latest)
    monkeypatch.setattr(build, "tool_build", latest)
    monkeypatch.setattr(build, "managed", lambda: True)
    cfg = load_config(tmp_path)
    cfg.models.clear()  # Health must not start a live model probe in this test.

    with TestClient(server.create_app(cfg)) as client:
        before = client.get("/app/sw.js")
        assert "kova-shell-" + "a" * 24 in before.text
        assert before.headers["cache-control"] == "no-store"
        latest.return_value = build.Build("0.1.0", fingerprint="b" * 64)
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            status = client.get("/health?probe=false").json()
            if status["restart_available"]:
                break
            time.sleep(0.01)
        assert status["restart_available"] and status["build"] == running.fingerprint
        assert "busy" not in status

    with TestClient(server.create_app(cfg)) as client:
        assert "kova-shell-" + "b" * 24 in client.get("/app/sw.js").text
        assert not client.get("/health?probe=false").json()["restart_available"]
