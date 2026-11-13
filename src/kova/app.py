"""Launchers for Kova's Electron desktop app and its browser web UI.

``kova-app`` runs the Electron app in ``desktop/`` — the copy the installer
staged under the Kova data dir, or the checkout the launcher runs from — and
installs the Electron dependencies with ``npm ci`` when they are missing.
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
import webbrowser
from pathlib import Path
from urllib.parse import quote

from kova.client import KovaError, ensure_server
from kova.config import kova_home, load_config

DESKTOP_DIR_ENV = "KOVA_DESKTOP_DIR"
DESKTOP_NAME = "desktop"
# npm names its shims differently per platform; any one means deps are installed.
ELECTRON_SHIMS = ("electron", "electron.cmd", "electron.ps1")


def desktop_dir(home: Path | None = None) -> Path | None:
    """The Electron project to run, or None when Kova has no copy of it.

    An explicit ``KOVA_DESKTOP_DIR`` wins, then the checkout this module lives
    in (``src/kova/app.py`` two levels below the repository root), then the copy
    the installer staged under the data dir for wheel installs.
    """
    override = os.environ.get(DESKTOP_DIR_ENV)
    staged = (Path(home) if home else kova_home()) / DESKTOP_NAME
    candidates = [Path(override).expanduser()] if override else []
    candidates += [Path(__file__).resolve().parents[2] / DESKTOP_NAME, staged]
    for candidate in candidates:
        if (candidate / "package.json").is_file():
            return candidate
    return None


def npm_command(npm: str, *args: str) -> list[str]:
    """Windows cannot execute an ``.cmd`` shim directly; go through the shell."""
    if os.name == "nt":
        return [os.environ.get("COMSPEC", "cmd.exe"), "/c", npm, *args]
    return [npm, *args]


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
    args = parser.parse_args(argv)
    raise SystemExit(_open_in_browser("kova-web") if args.browser else launch_desktop())


if __name__ == "__main__":
    main()
