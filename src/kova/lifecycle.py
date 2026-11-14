"""User-level uv installation management. Never starts a server or creates config."""

from __future__ import annotations

import base64
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tomllib
from importlib import metadata
from pathlib import Path
from urllib.parse import quote

import httpx
import psutil

from kova import __version__, autostart
from kova.config import kova_home

REPOSITORY = "https://github.com/wynxo/Kova"
UNINSTALL_KEY = r"Software\Microsoft\Windows\CurrentVersion\Uninstall\Kova"


class LifecycleError(Exception):
    """An actionable install/update/uninstall failure."""


def selected_ref(ref: str | None = None) -> str:
    value = ref if ref is not None else os.environ.get("KOVA_REF", "main")
    if not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_./-]*", value):
        raise LifecycleError("Invalid ref. Use a branch, tag, or commit SHA.")
    return value


def source_spec(ref: str) -> str:
    if source := os.environ.get("KOVA_SOURCE"):
        return str(Path(source).expanduser().resolve(strict=True))
    # Once published, replace this return expression with: "kova-ai==VERSION".
    return (f"kova-ai @ git+{REPOSITORY}@{ref}" if shutil.which("git") else
            f"kova-ai @ {REPOSITORY}/archive/{ref}.tar.gz")


def tool_env() -> dict[str, str]:
    """Match the standalone installers, independent of XDG/global uv settings."""
    env = os.environ.copy()
    home = Path.home()
    env.update({
        "UV_TOOL_BIN_DIR": str(home / ".local/bin"),
        "UV_TOOL_DIR": str(home / ".local/share/uv/tools"),
        "UV_PYTHON_INSTALL_DIR": str(home / ".local/share/uv/python"),
        "UV_CACHE_DIR": str(home / ".cache/uv"),
    })
    return env


def find_uv() -> str:
    for path in (Path.home() / ".local/bin/uv", Path.home() / ".cargo/bin/uv"):
        if sys.platform == "win32":
            path = path.with_suffix(".exe")
        if path.is_file() and os.access(path, os.X_OK):
            return str(path)
    if uv := shutil.which("uv"):
        return uv
    raise LifecycleError("uv was not found. Re-run the Kova installer first.")


def installed_info() -> tuple[str, str | None]:
    try:
        dist = metadata.distribution("kova-ai")
    except metadata.PackageNotFoundError:
        return __version__, None
    try:
        origin = json.loads(dist.read_text("direct_url.json") or "{}")
        commit = origin.get("vcs_info", {}).get("commit_id")
        if not commit:
            match = re.search(r"/archive/([0-9a-f]{40})\.tar\.gz$", origin.get("url", ""))
            commit = match[1] if match else None
        return dist.version, commit
    except (ValueError, TypeError):
        return dist.version, None


def check_update(ref: str) -> str:
    """Read-only comparison; same version alone cannot prove a branch is current."""
    try:
        response = httpx.get(
            f"https://api.github.com/repos/wynxo/Kova/commits/{quote(ref, safe='')}",
            timeout=15, follow_redirects=True,
        )
        response.raise_for_status()
        target = response.json()["sha"]
        response = httpx.get(
            f"https://raw.githubusercontent.com/wynxo/Kova/{target}/pyproject.toml",
            timeout=15, follow_redirects=True,
        )
        response.raise_for_status()
        version = tomllib.loads(response.text)["project"]["version"]
    except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
        raise LifecycleError(f"Could not check for updates: {exc}") from exc
    current_version, commit = installed_info()
    if commit == target:
        return f"Up to date: Kova {current_version} ({target[:12]}, {ref})."
    if commit or version != current_version:
        return (f"Update available: {current_version} ({(commit or 'unknown')[:12]}) → "
                f"{version} ({target[:12]}, {ref}). Run: kova update --ref {ref}")
    return (f"Kova {current_version}; {ref} is {version} ({target[:12]}). Installed commit unavailable; "
            "version matches, but freshness cannot be confirmed. Run: kova update")


def update(ref: str, emit=print) -> None:
    source = source_spec(ref)
    command = [find_uv(), "tool", "install", "--force", "--refresh", "--python", "3.12", source]
    where = source if os.environ.get("KOVA_SOURCE") else ref
    emit(f"→ Installing or updating Kova from {where}")
    try:
        subprocess.run(command, check=True, env=tool_env())
    except (OSError, subprocess.CalledProcessError) as exc:
        raise LifecycleError(f"Kova update failed: {exc}") from exc
    emit("✓ Updated Kova. Run: kova")


def verified_server(pid: int, home: Path):
    """Return a PID-reuse-aware handle only for this home's Kova server."""
    if pid <= 1:
        return None
    try:
        process = psutil.Process(pid)
        args = process.cmdline()
        module = any(args[i:i + 3] == ['-m', 'kova', 'serve'] for i in range(len(args)))
        script = len(args) >= 2 and Path(args[0]).stem in ('kova', 'kova.exe') and args[1] == 'serve'
        script = script or (len(args) >= 3 and Path(args[1]).stem == 'kova' and args[2] == 'serve')
        if not (module or script):
            return None
        environment = process.environ()
        owner_home = environment.get('KOVA_HOME') or str(Path.home() / '.kova')
        if Path(owner_home).resolve() != home.resolve():
            return None
        return process
    except (psutil.Error, OSError, ValueError):
        return None


def stop_server(home: Path) -> None:
    pidfile = home / "server.pid"
    if not pidfile.exists():
        return
    try:
        pid = int(pidfile.read_text().strip())
        if pid <= 1:
            raise ValueError("invalid server PID")
    except ValueError:
        pidfile.unlink(missing_ok=True)
        return
    try:
        process = verified_server(pid, home)
        if process is None:
            pidfile.unlink(missing_ok=True)
            return
        process.send_signal(signal.SIGTERM)
        pidfile.unlink(missing_ok=True)
    except (ProcessLookupError, psutil.NoSuchProcess):
        pidfile.unlink(missing_ok=True)
    except (OSError, psutil.Error) as exc:
        raise LifecycleError(f"Could not stop the Kova server: {exc}") from exc


def validate_purge(home: Path) -> None:
    resolved, user_home = home.resolve(), Path.home().resolve()
    if home.is_symlink() or resolved == user_home or not resolved.is_relative_to(user_home):
        raise LifecycleError(f"Refusing to delete {home}: data must be a real directory inside your home.")


def _windows_uninstall(uv: str, home: Path, purge: bool) -> None:
    """Wait for this executable to exit before uv removes it (Windows file locking)."""
    def ps(value: str) -> str:
        return "'" + value.replace("'", "''") + "'"

    if purge:
        data = f"Remove-Item -LiteralPath {ps(str(home))} -Recurse -Force"
    else:
        # Staged desktop app files are reinstalled by the installer, unlike data.
        staged = home / 'desktop'
        data = (f"if (Test-Path -LiteralPath {ps(str(staged / 'package.json'))}) {{ "
                f"Remove-Item -LiteralPath {ps(str(staged))} -Recurse -Force -ErrorAction SilentlyContinue }}")

    script = f"""
$ErrorActionPreference = 'Stop'
Wait-Process -Id {os.getpid()} -ErrorAction SilentlyContinue
& {ps(uv)} tool uninstall kova-ai
if ($LASTEXITCODE -ne 0) {{ throw 'uv uninstall failed; data was kept.' }}
{data}
Remove-Item -LiteralPath 'HKCU:\\{UNINSTALL_KEY}' -ErrorAction SilentlyContinue
Remove-Item -LiteralPath {ps(str(Path.home() / '.local/share/kova/uninstall.ps1'))} -ErrorAction SilentlyContinue
Write-Host 'Done. Kova is removed.'
"""
    encoded = base64.b64encode(script.encode("utf-16le")).decode("ascii")
    subprocess.Popen(
        ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-EncodedCommand", encoded],
        env=tool_env(), creationflags=subprocess.CREATE_NEW_PROCESS_GROUP,
    )


def uninstall(*, purge: bool, keep_data: bool, confirm, emit=print) -> None:
    if purge and keep_data:
        raise LifecycleError("Choose --purge or --keep-data, not both.")
    home = kova_home().absolute()
    # Ask BEFORE deleting the tool so cancellation/EOF can always keep the data.
    if home.exists() and not purge and not keep_data and sys.stdin.isatty():
        purge = confirm(f"Delete your Kova data in {home} (chats, memory, model settings, token)?")
    if purge:
        validate_purge(home)
    emit("→ Stopping the Kova server (if running) ...")
    stop_server(home)
    emit("→ Removing Kova's start-at-login entry ...")
    try:
        autostart.disable()
    except (autostart.AutostartError, OSError) as exc:
        emit(f"! Could not remove the start-at-login entry: {exc}")
    uv = find_uv()
    emit("→ Removing the Kova commands (kova, kova-app, kova-web): uv tool uninstall kova-ai")
    try:
        if sys.platform == "win32":
            _windows_uninstall(uv, home, purge and home.exists())
            emit("✓ Uninstall scheduled; it completes when this command exits.")
            return
        subprocess.run([uv, "tool", "uninstall", "kova-ai"], check=True, env=tool_env())
    except (OSError, subprocess.CalledProcessError) as exc:
        raise LifecycleError(f"Kova uninstall failed; your data was kept: {exc}") from exc
    if purge and home.exists():
        shutil.rmtree(home)
        emit(f"✓ Deleted {home}.")
    elif home.exists():
        # The staged desktop app files are reinstalled by the installer, unlike
        # user data; keep only what --keep-data means to keep.
        staged = home / "desktop"
        if (staged / "package.json").is_file() and not staged.is_symlink():
            shutil.rmtree(staged)
            emit(f"✓ Removed the desktop app files in {staged}.")
        emit(f"✓ Keeping your data in {home}. Use --purge to delete it.")
    emit("✓ External memory folders and files are left alone.")
    emit("✓ Done. Kova is removed.")
