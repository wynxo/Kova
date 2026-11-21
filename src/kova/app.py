"""Launchers for Kova's Electron desktop app and its browser web UI.

``kova-app`` refreshes the staged Electron app from the installed package
and installs its dependencies when missing or when the lockfile changes.
``kova-web`` starts (or reuses) the server and opens the packaged web UI in the
default browser; ``kova-app --browser`` is the historic alias for it.

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
# npm names its shims differently per platform; any one means deps are installed.
ELECTRON_SHIMS = ("electron", "electron.cmd", "electron.ps1")


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
        raise DesktopError("The desktop app files are missing. Reinstall Kova or use kova-web.")
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
        if pending.exists() or not _electron_installed(project):
            if npm is None:
                raise DesktopError("Install Node.js 22+ with npm and run kova-app to finish updating Electron.")
            pending.touch()
            print("Kova: preparing Electron ...", flush=True)
            if _run(npm_command(npm, "ci", "--include=dev"), project) != 0:
                raise DesktopError("npm ci failed. Check your connection and run kova-app again, or use kova-web.")
        # Electron 45 downloads its native binary on demand, not during npm ci.
        # App-menu launchers call that binary directly, so fetch it now.
        if not _electron_binary(project).is_file():
            node = shutil.which("node")
            if node is None or _run([node, "node_modules/electron/install.js"], project) != 0:
                raise DesktopError("Electron could not be downloaded. Check your connection and run kova-app again.")
            if not _electron_binary(project).is_file():
                raise DesktopError("Electron is missing. Enable Electron downloads and run kova-app again.")
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


def _electron_installed(project: Path) -> bool:
    binary = project / "node_modules" / ".bin"
    return any((binary / name).exists() for name in ELECTRON_SHIMS)


def _run(command: list[str], project: Path, env: dict[str, str] | None = None) -> int:
    return subprocess.run(command, cwd=project, env=env, check=False).returncode


def launch_desktop() -> int:
    """Run the Electron app, installing its dependencies on first use."""
    npm = shutil.which("npm")
    if npm is None:
        print("Kova: Node.js and npm are required for the desktop app. Install Node.js 22+ from "
              "https://nodejs.org and run kova-app again. Opening the web UI instead.", file=sys.stderr)
        return _open_in_browser("kova-web")
    project = desktop_dir()
    if project is None:
        print("Kova: the desktop app files are missing from this install. Reinstall Kova to add "
              "them, or open the web UI with kova-web.", file=sys.stderr)
        return 1
    if not os.environ.get(DESKTOP_DIR_ENV) and (
        project != _bundled_desktop() or not (project.parents[2] / "pyproject.toml").is_file()
    ):
        try:
            project = refresh_desktop(kova_home(), npm)
        except (DesktopError, OSError, Timeout) as exc:
            print(f"Kova: could not prepare the desktop app: {exc}", file=sys.stderr)
            return 1
    if not _electron_installed(project):
        print(f"Kova: installing desktop dependencies in {project} (first run only) ...")
        if _run(npm_command(npm, "ci"), project) != 0:
            print("Kova: npm ci failed, so the desktop app cannot start. Check your network "
                  "connection to the npm registry, or open the web UI with kova-web.", file=sys.stderr)
            return 1
    env = dict(os.environ)
    # `npm start` must launch the real Electron binary, not Node.
    env.pop("ELECTRON_RUN_AS_NODE", None)
    return _run(npm_command(npm, "start"), project, env=env)


def _open_in_browser(command: str) -> int:
    """Start (or reuse) the server and open the packaged web UI in a browser."""
    client = None
    try:
        client = ensure_server(load_config())

        def login_url() -> str:
            # A fragment is not sent to HTTP servers or recorded in access logs.
            return f"{client.base_url}/app#ticket={quote(client.app_ticket(), safe='')}"

        if not webbrowser.open(login_url()):
            raise KovaError(None, f"No browser could be opened. Check your default browser and run {command} again.")
    except KovaError as exc:
        message = ("The running server needs a restart for the web UI. Run kova stop, then kova-web."
                   if exc.status == 404 else str(exc))
        print(f"Kova: {message}", file=sys.stderr)
        return 1
    finally:
        if client is not None:
            client.http.close()
    return 0


def web_main(argv: list[str] | None = None) -> None:
    """Console entry point for ``kova-web``: the web UI in the default browser."""
    parser = argparse.ArgumentParser(prog="kova-web", description="Open Kova's web UI in your browser.")
    parser.parse_args(argv)
    raise SystemExit(_open_in_browser("kova-web"))


def main(argv: list[str] | None = None) -> None:
    """Console entry point for ``kova-app``: the Electron desktop app."""
    parser = argparse.ArgumentParser(prog="kova-app", description="Open the Kova desktop app.")
    parser.add_argument("--browser", action="store_true",
                        help="Open the web UI in your browser instead (same as kova-web).")
    parser.add_argument("--prepare", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args.prepare:
        try:
            refresh_desktop(kova_home())
        except (DesktopError, OSError, Timeout) as exc:
            print(f"Kova: could not prepare the desktop app: {exc}", file=sys.stderr)
            raise SystemExit(1) from None
        return
    raise SystemExit(_open_in_browser("kova-web") if args.browser else launch_desktop())


if __name__ == "__main__":
    main()
