"""Find and restart only Kova processes owned by this data directory."""

from __future__ import annotations

import os
import signal
import subprocess
import time
from contextlib import suppress
from pathlib import Path

import psutil

from kova import build, lifecycle
from kova.app import _electron_binary
from kova.client import KovaClient, KovaError, _spawn_server
from kova.config import Config, load_config


def running_server(cfg: Config) -> psutil.Process | None:
    if not cfg.home.is_dir():
        return None
    pidfile = cfg.home / "server.pid"
    with suppress(OSError, ValueError):
        if process := lifecycle.verified_server(int(pidfile.read_text()), cfg.home):
            if alive(process):
                return process
    # Old desktop shells and start-at-login services didn't record a PID.
    # Require the process to own the configured listener as well as the home.
    try:
        for connection in psutil.net_connections(kind="tcp"):
            if (connection.status == psutil.CONN_LISTEN and connection.laddr.port == cfg.port
                    and connection.pid is not None):
                if process := lifecycle.verified_server(connection.pid, cfg.home):
                    return process
    except (OSError, psutil.AccessDenied):
        # macOS may prohibit the system-wide socket listing but allow queries
        # for processes owned by this user.
        for candidate in psutil.process_iter(["cmdline"]):
            if "serve" not in (candidate.info["cmdline"] or []):
                continue
            if process := lifecycle.verified_server(candidate.pid, cfg.home):
                with suppress(OSError, psutil.Error):
                    if any(conn.status == psutil.CONN_LISTEN and conn.laddr.port == cfg.port
                           for conn in process.net_connections(kind="tcp")):
                        return process
    return None


def running_desktop(home: Path) -> psutil.Process | None:
    project = home / "desktop"
    binary = _electron_binary(project).resolve()
    for process in psutil.process_iter(["exe", "cmdline", "cwd"]):
        info = process.info
        args = info["cmdline"] or []
        if not info["exe"] or Path(info["exe"]).resolve() != binary:
            continue
        # Match the Electron main process, not renderers or a different app.
        if any(arg.startswith("--type=") for arg in args[1:]):
            continue
        if (str(project) in args[1:] or (info["cwd"] == str(project) and "." in args[1:])):
            return process
    return None


def alive(process: psutil.Process) -> bool:
    try:
        return process.is_running() and process.status() != psutil.STATUS_ZOMBIE
    except psutil.NoSuchProcess:
        return False


def stop(process: psutil.Process) -> None:
    if not alive(process):
        return
    with suppress(psutil.NoSuchProcess):
        process.send_signal(signal.SIGTERM)
        _, remaining = psutil.wait_procs([process], timeout=10)
        if remaining and alive(process):
            raise lifecycle.LifecycleError("The old process did not exit; close it and retry.")


def health(cfg: Config) -> dict:
    token = (cfg.home / "token").read_text().strip()
    client = KovaClient(f"http://127.0.0.1:{cfg.port}", token)
    try:
        return client.health(probe=False)
    finally:
        client.http.close()


def verify_server(cfg: Config, expected: build.Build) -> None:
    status = health(cfg)
    if status["version"] != expected.version or (
        status.get("build") and expected.fingerprint and status["build"] != expected.fingerprint
    ):
        raise lifecycle.LifecycleError("The server answered on the old build.")


def start_server(cfg: Config, expected: build.Build, *, start=None) -> None:
    # If an OS service manager has already started the new build, keep it.
    with suppress(OSError, KovaError):
        verify_server(cfg, expected)
        return
    if start:
        start()
    else:
        _spawn_server(cfg, python=build.tool_python())
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        try:
            verify_server(cfg, expected)
            return
        except (OSError, KovaError) as exc:
            if isinstance(exc, KovaError) and exc.status not in (None, 502):
                raise
            time.sleep(0.1)
    raise lifecycle.LifecycleError("The server did not become healthy; see server.log.")


def start_desktop(home: Path, launcher: Path) -> None:
    project = home / "desktop"
    command = [_electron_binary(project), project]
    env = {**os.environ, "KOVA_HOME": str(home), "KOVA_DESKTOP_CLI": str(launcher)}
    env.pop("ELECTRON_RUN_AS_NODE", None)
    with (home / "desktop.log").open("a") as log:
        process = subprocess.Popen(command, env=env, cwd=project, start_new_session=True,
                                   stdin=subprocess.DEVNULL, stdout=log, stderr=log)
        try:
            code = process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            return
    if code or not running_desktop(home):
        raise lifecycle.LifecycleError("Electron exited during startup; see desktop.log.")


def restart_stale_server() -> bool:
    """At desktop startup, replace an idle, owned server with this install."""
    from filelock import FileLock, Timeout

    if not build.managed():
        return False
    cfg = load_config(read_only=True)
    try:
        with FileLock(build.tool_dir().parent / ".kova-update.lock", timeout=0):
            status = health(cfg)
            current = build.installed()
            stale = (status["version"] != current.version
                     or bool(status.get("build") and current.fingerprint
                             and status["build"] != current.fingerprint))
            if not stale or status.get("busy"):
                return False
            process = running_server(cfg)
            if process is None:
                return False
            stop(process)
            start_server(cfg, current)
            return True
    except (Timeout, FileNotFoundError, KovaError):
        return False
