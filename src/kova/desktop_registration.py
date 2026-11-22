"""Per-user desktop integration from the currently installed Kova package."""

from __future__ import annotations

import os
import plistlib
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

from kova.autostart import _desktop_arg

ICON_SIZES = (16, 22, 24, 32, 36, 48, 64, 72, 96, 128, 192, 256, 512)


def registration_path() -> Path | None:
    home = Path.home()
    if sys.platform == "linux":
        return home / ".local/share/applications/kova.desktop"
    if sys.platform == "darwin":
        return home / "Applications/Kova.app/Contents/Info.plist"
    if sys.platform == "win32":
        roaming = Path(os.environ.get("APPDATA") or home / "AppData/Roaming")
        return roaming / "Microsoft/Windows/Start Menu/Programs/Kova.lnk"
    return None


def registered_linux_home(entry: Path) -> Path | None:
    """Preserve a custom data home recorded by either generation of Linux launcher."""
    if sys.platform != "linux":
        return None
    try:
        command = next((line[5:] for line in entry.read_text(encoding="utf-8").splitlines()
                        if line.startswith("Exec=")), "")
        args = shlex.split(command)
    except (OSError, ValueError):
        return None
    if len(args) == 4 and args[1:3] == ["app", "--data-home"]:
        data = Path(args[3].replace("%%", "%"))
    elif len(args) == 1 and Path(args[0]).parts[-2:] == ("desktop", "launch"):
        data = Path(args[0]).parents[1]
    else:
        return None
    return data if data.is_absolute() else None


def linux_icons(share: Path):
    root = share / "icons/hicolor"
    for size in ICON_SIZES:
        source = "icon.png" if size == 512 else f"icon-{size}.png"
        yield source, root / f"{size}x{size}/apps/kova.png"
    yield "icon.svg", root / "scalable/apps/kova.svg"


def refresh_linux_caches(share: Path) -> None:
    for name, args in (
        ("update-desktop-database", [str(share / "applications")]),
        ("gtk-update-icon-cache", ["-f", "-t", str(share / "icons/hicolor")]),
        ("xdg-desktop-menu", ["forceupdate"]),
        ("kbuildsycoca6", []),
    ):
        if tool := shutil.which(name):
            try:
                subprocess.run([tool, *args], check=False, timeout=10,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            except (OSError, subprocess.TimeoutExpired):
                pass  # A session cache is best-effort; registration is already on disk.


def register_desktop(data: Path, source: Path) -> None:
    """Write the launcher and current icon assets without depending on Electron preparation."""
    entry = registration_path()
    if entry is None:
        return
    bin_dir = Path(os.environ.get("UV_TOOL_BIN_DIR") or Path.home() / ".local/bin")
    cli = bin_dir / ("kova.exe" if sys.platform == "win32" else "kova")
    if not cli.is_file():
        raise OSError("The installed kova command is missing. Re-run the Kova installer.")
    assets = source / "assets"
    entry.parent.mkdir(parents=True, exist_ok=True)
    if sys.platform == "linux":
        _register_linux(entry, cli, data, source)
    elif sys.platform == "darwin":
        _register_macos(entry, cli, data, assets)
    elif sys.platform == "win32":
        _register_windows(entry, data, assets)


def _register_linux(entry: Path, cli: Path, data: Path, source: Path) -> None:
    share = entry.parent.parent
    arguments = [str(cli), "app"]
    if data != Path.home() / ".kova":
        arguments += ["--data-home", str(data)]
    command = " ".join(_desktop_arg(arg.replace("%", "%%")) for arg in arguments)
    template = (source / "kova.desktop").read_text(encoding="utf-8")
    entry.write_text(template.replace("Exec=kova app", f"Exec={command}"), encoding="utf-8")
    for name, target in linux_icons(share):
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source / "assets" / name, target)
    refresh_linux_caches(share)


def _register_macos(entry: Path, cli: Path, data: Path, assets: Path) -> None:
    contents = entry.parent
    launcher = contents / "MacOS/Kova"
    launcher.parent.mkdir(exist_ok=True)
    launcher.write_text(f"#!/bin/sh\nexec {shlex.join([str(cli), 'app', '--data-home', str(data)])}\n",
                        encoding="utf-8")
    launcher.chmod(0o755)
    icon = contents / "Resources/kova.icns"
    icon.parent.mkdir(exist_ok=True)
    shutil.copyfile(assets / "icon.icns", icon)
    entry.write_bytes(plistlib.dumps({
        "CFBundleIdentifier": "io.github.wynxo.kova.launcher",
        "CFBundleName": "Kova",
        "CFBundleDisplayName": "Kova",
        "CFBundleExecutable": "Kova",
        "CFBundleIconFile": "kova.icns",
        "CFBundlePackageType": "APPL",
    }))
    lsregister = Path("/System/Library/Frameworks/CoreServices.framework/Frameworks/"
                      "LaunchServices.framework/Support/lsregister")
    if lsregister.is_file():
        subprocess.run([str(lsregister), "-f", str(contents.parent)], check=False)


def _register_windows(entry: Path, data: Path, assets: Path) -> None:
    project = data / "desktop"
    project.mkdir(parents=True, exist_ok=True)
    icon = project / "assets/icon.ico"
    icon.parent.mkdir(exist_ok=True)
    # The staged copy may already be current on install.
    if not icon.is_file() or icon.read_bytes() != (assets / icon.name).read_bytes():
        shutil.copyfile(assets / icon.name, icon)
    script = """
$ErrorActionPreference = 'Stop'
$shell = New-Object -ComObject WScript.Shell
$link = $shell.CreateShortcut($env:KOVA_SHORTCUT_PATH)
$link.TargetPath = $env:KOVA_SHORTCUT_TARGET
$link.Arguments = $env:KOVA_SHORTCUT_ARGS
$link.WorkingDirectory = $env:KOVA_SHORTCUT_CWD
$link.IconLocation = $env:KOVA_SHORTCUT_ICON
$link.Description = 'Private local AI assistant'
$link.Save()
"""
    env = dict(os.environ,
               KOVA_SHORTCUT_PATH=str(entry),
               KOVA_SHORTCUT_TARGET=str(project / "node_modules/electron/dist/electron.exe"),
               KOVA_SHORTCUT_ARGS=f'"{project}" "--kova-home={data}"',
               KOVA_SHORTCUT_CWD=str(project), KOVA_SHORTCUT_ICON=f"{icon},0")
    try:
        subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
                       env=env, check=True, timeout=15)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        raise OSError("Could not update the Kova Start menu shortcut.") from exc
