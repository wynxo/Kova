"""Launchers for Kova's Electron desktop app and its browser web UI.

``kova app`` refreshes the staged Electron app from the installed package
and installs its dependencies when missing or when the lockfile changes.
``kova web`` starts (or reuses) the server and opens the packaged web UI in the
default browser; ``kova app --browser`` is an alternative way to open it.

The Electron shell starts the server itself and mints its own single-use
ticket, so the desktop path never touches the bearer token here.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
import webbrowser
from pathlib import Path
from urllib.parse import quote

from filelock import FileLock, Timeout

from kova.client import KovaError, ensure_server
from kova.config import kova_home, load_config

DESKTOP_DIR_ENV = "KOVA_DESKTOP_DIR"
DESKTOP_NAME = "desktop"


class DesktopError(Exception):
    """Desktop preparation failed without affecting the installed CLI."""


def _bundled_desktop() -> Path:
    return Path(__file__).resolve().with_name(DESKTOP_NAME)


def desktop_dir(home: Path | None = None) -> Path | None:
    """An explicit override wins, then the stage, then the developer checkout."""
    override = os.environ.get(DESKTOP_DIR_ENV)
    candidates = [Path(override).expanduser()] if override else []
    candidates += [(home or kova_home()) / DESKTOP_NAME, _bundled_desktop()]
    return next((path for path in candidates if (path / "package.json").is_file()), None)


def refresh_desktop(home: Path, npm: str | None = None) -> Path:
    """Update app files in place, retaining legacy launchers and profile files."""
    source = _bundled_desktop()
    project = home / DESKTOP_NAME
    lockfile = source / "package-lock.json"
    if not lockfile.is_file():
        raise DesktopError("The desktop app files are missing. Reinstall Kova or use kova web.")
    if npm is None:
        npm = shutil.which("npm")
    project.mkdir(parents=True, exist_ok=True)
    with FileLock(home / ".desktop-install.lock", timeout=300):
        pending = project / ".npm-ci-pending"
        installed_lock = project / lockfile.name
        if not installed_lock.is_file() or installed_lock.read_bytes() != lockfile.read_bytes():
            pending.touch()
        for entry in source.iterdir():
            if entry.is_dir() and entry.name != "assets":
                continue
            files = entry.rglob("*") if entry.is_dir() else (entry,)
            for file in files:
                if not file.is_file():
                    continue
                target = project / file.relative_to(source)
                content = file.read_bytes()
                if target.is_file() and target.read_bytes() == content:
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                with tempfile.NamedTemporaryFile(dir=target.parent, delete=False) as temp:
                    temporary = Path(temp.name)
                    temp.write(content)
                try:
                    temporary.replace(target)
                finally:
                    temporary.unlink(missing_ok=True)
        reinstall = pending.exists()
        if reinstall or not _electron_binary(project).is_file():
            pending.touch()
        _prepare_electron(project, npm, reinstall=reinstall)
        pending.unlink(missing_ok=True)
    return project


def npm_command(npm: str, *args: str) -> list[str]:
    """Windows cannot execute an ``.cmd`` shim directly; go through the shell."""
    if os.name == "nt":
        return [os.environ.get("COMSPEC", "cmd.exe"), "/c", npm, *args]
    return [npm, *args]


def _electron_binary(project: Path) -> Path:
    relative = {
        "darwin": "Electron.app/Contents/MacOS/Electron",
        "win32": "electron.exe",
    }.get(sys.platform, "electron")
    return project / "node_modules/electron/dist" / relative


def _run(command: list[str], project: Path) -> int:
    return subprocess.run(command, cwd=project, check=False).returncode


def _prepare_electron(project: Path, npm: str | None, *, reinstall: bool = False) -> None:
    binary = _electron_binary(project)
    installer = Path("node_modules/electron/install.js")
    if reinstall or not (binary.is_file() or (project / installer).is_file()):
        if npm is None:
            raise DesktopError("Install Node.js 22+ with npm and run kova app to finish updating Electron.")
        print("Kova: preparing Electron ...", flush=True)
        if _run(npm_command(npm, "ci", "--include=dev"), project) != 0:
            raise DesktopError("npm ci failed. Check your connection and run kova app again, or use kova web.")
    # Electron downloads the binary lazily. Do it explicitly outside the launch.
    if not binary.is_file():
        node = shutil.which("node")
        if node is None or _run([node, str(installer)], project) != 0 or not binary.is_file():
            raise DesktopError("Electron could not be downloaded. Check your connection and run kova app again.")


def launch_desktop(electron_args: list[str] | None = None) -> int:
    """Run the native Electron app, installing its dependencies on first use."""
    npm = shutil.which("npm")
    home = kova_home().absolute()
    project = desktop_dir()
    if project is None:
        return _report_preparation_failure(home, "the desktop app files are missing. Reinstall Kova.")
    if not _electron_binary(project).is_file() and npm is None:
        print("Kova: Node.js and npm are required for first-time desktop setup. Install Node.js 22+ from "
              "https://nodejs.org and run kova app again. Opening the web UI instead.", file=sys.stderr)
        return open_in_browser()
    try:
        if not os.environ.get(DESKTOP_DIR_ENV) and (
            project != _bundled_desktop() or not (project.parents[2] / "pyproject.toml").is_file()
        ):
            project = refresh_desktop(home, npm)
        else:
            _prepare_electron(project, npm)
    except (DesktopError, OSError, Timeout) as exc:
        return _report_preparation_failure(home, f"could not prepare the desktop app: {exc}")
    return _launch_electron(project.absolute(), home, electron_args or [])


def _open_desktop_log(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    return os.fdopen(fd, "a", encoding="utf-8", buffering=1)


def _report_preparation_failure(home: Path, message: str) -> int:
    log_path = home / "desktop.log"
    try:
        with _open_desktop_log(log_path) as log:
            log.write(f"Kova: {message}\n")
    except OSError as exc:
        message = f"{message} (could not write {log_path}: {exc})"
    else:
        message = f"{message} See {log_path}."
    print(f"Kova: {message} Open the web UI with kova web.", file=sys.stderr)
    return 1


def _launch_electron(project: Path, home: Path, electron_args: list[str] | None = None) -> int:
    env = dict(os.environ)
    env.pop("ELECTRON_RUN_AS_NODE", None)
    env["KOVA_HOME"] = str(home)
    # Match the running installation even when its bin directory is not on PATH.
    cli = Path(sys.executable).with_name("kova.exe" if os.name == "nt" else "kova")
    if cli.is_file():
        env["KOVA_DESKTOP_CLI"] = str(cli)
    command = [str(_electron_binary(project)), str(project)]
    if sys.platform == "linux":
        command.append("--ozone-platform-hint=auto")
    # Chromium switches such as --remote-debugging-port pass straight through.
    command += electron_args or []
    log_path = home / "desktop.log"
    try:
        with _open_desktop_log(log_path) as log:
            with tempfile.TemporaryDirectory(prefix=".desktop-start-", dir=home) as attempt:
                startup = Path(attempt) / "status"
                env["KOVA_DESKTOP_STARTUP"] = str(startup)
                log.write("\nKova: starting Electron\n")
                try:
                    code = subprocess.run(command, cwd=project, env=env, check=False,
                                          stdout=log, stderr=subprocess.STDOUT).returncode
                except OSError as exc:
                    message = f"could not execute Electron: {exc}"
                else:
                    state = startup.read_text(encoding="utf-8") if startup.is_file() else ""
                    if code == 0 and state == "ready":
                        return 0
                    if state == "profile-busy":
                        message = ("Electron could not acquire the desktop profile. "
                                   "If Kova is already running, its window was requested")
                    elif code:
                        message = f"Electron exited with status {code}"
                    else:
                        message = "Electron exited before opening a window"
                log.write(f"Kova: {message}\n")
    except OSError as exc:
        print(f"Kova: could not access desktop diagnostics at {log_path}: {exc}. Open the web UI with kova web.",
              file=sys.stderr)
        return 1
    print(f"Kova: {message}. See {log_path}, or open the web UI with kova web.", file=sys.stderr)
    return 1


def open_in_browser() -> int:
    """Start (or reuse) the server and open the packaged web UI in a browser."""
    client = None
    try:
        client = ensure_server(load_config())

        def login_url() -> str:
            # A fragment is not sent to HTTP servers or recorded in access logs.
            return f"{client.base_url}/app#ticket={quote(client.app_ticket(), safe='')}"

        if not webbrowser.open(login_url()):
            raise KovaError(None, "No browser could be opened. Check your default browser and run kova web again.")
        # The ticket fragment is a one-use secret: say where we went without it.
        print(f"Kova: opened {client.base_url}/app in your browser.", flush=True)
    except KovaError as exc:
        message = ("The running server needs a restart for the web UI. Run kova stop, then kova web."
                   if exc.status == 404 else str(exc))
        print(f"Kova: {message}", file=sys.stderr)
        return 1
    finally:
        if client is not None:
            client.http.close()
    return 0


def web_main(argv: list[str] | None = None) -> None:
    """Compatibility entry point: the web UI in the default browser."""
    parser = argparse.ArgumentParser(prog="kova web", description="Open Kova's web UI in your browser.")
    parser.parse_args(argv)
    raise SystemExit(open_in_browser())


def prepare_installed_desktop(*, register: bool = False) -> None:
    from kova.desktop_integration import menu_path, refresh_desktop_integration, registered_linux_home

    home = kova_home().absolute()
    menu = menu_path()
    repair = not register and menu is not None and menu.exists()
    bin_dir = Path(os.environ.get("UV_TOOL_BIN_DIR") or Path.home() / ".local/bin")
    launcher = bin_dir / ("kova.exe" if os.name == "nt" else "kova")
    if repair and sys.platform == "linux":
        if not os.environ.get("KOVA_HOME"):
            home = registered_linux_home(menu) or home
        refresh_desktop_integration(home, launcher, legacy_source=_bundled_desktop())
    refresh_desktop(home)
    if register or (repair and sys.platform != "linux"):
        refresh_desktop_integration(home, launcher)


def main(argv: list[str] | None = None) -> None:
    """Compatibility entry point: the Electron desktop app."""
    parser = argparse.ArgumentParser(prog="kova app", description="Open the Kova desktop app.")
    parser.add_argument("--browser", action="store_true",
                        help="Open the web UI in your browser instead (same as kova web).")
    parser.add_argument("--prepare", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--register", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--data-home", type=Path, help=argparse.SUPPRESS)
    args, extra = parser.parse_known_args(argv)
    unknown = [arg for arg in extra if not arg.startswith("--")]
    if unknown:
        parser.error(f"unrecognized arguments: {' '.join(unknown)}")
    if args.data_home is not None:
        os.environ["KOVA_HOME"] = str(args.data_home)
    if args.prepare:
        try:
            prepare_installed_desktop(register=args.register)
        except (DesktopError, OSError, Timeout, subprocess.SubprocessError) as exc:
            print(f"Kova: could not prepare the desktop app: {exc}", file=sys.stderr)
            raise SystemExit(1) from None
        return
    raise SystemExit(open_in_browser() if args.browser else launch_desktop(extra))


if __name__ == "__main__":
    main()
