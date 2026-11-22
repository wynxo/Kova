"""Per-user desktop launchers, icons, and caches for installs and updates."""

from __future__ import annotations

import os
import plistlib
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

from kova import autostart, build
from kova.app import DesktopError, _electron_binary
from kova.config import private_write

ICON_SIZES = (16, 22, 24, 32, 36, 48, 64, 72, 96, 128, 192, 256, 512)
ICON_FILES = {size: "icon.png" if size == 512 else f"icon-{size}.png" for size in ICON_SIZES}
LAUNCH_SERVICES = Path("/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister")


def menu_path() -> Path | None:
    if sys.platform == "linux":
        return Path.home() / ".local/share/applications/kova.desktop"
    if sys.platform == "darwin":
        return Path.home() / "Applications/Kova.app"
    if sys.platform == "win32":
        roaming = Path(os.environ.get("APPDATA") or Path.home() / "AppData/Roaming")
        return roaming / "Microsoft/Windows/Start Menu/Programs/Kova.lnk"
    return None


def registered_linux_home(entry: Path | None) -> Path | None:
    """Preserve a custom data home recorded by either generation of Linux launcher."""
    if sys.platform != "linux" or entry is None:
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
        data = Path(args[0].replace("%%", "%")).parents[1]
    else:
        return None
    return data if data.is_absolute() else None


def linux_icons(share: Path):
    root = share / "icons/hicolor"
    for size, source in ICON_FILES.items():
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


def remove_desktop_registration() -> None:
    """Remove only the app menu and shared icons installed for this user."""
    menu = menu_path()
    if menu is None:
        return
    if sys.platform == "linux":
        menu.unlink(missing_ok=True)
        share = menu.parent.parent
        for _, icon in linux_icons(share):
            icon.unlink(missing_ok=True)
        refresh_linux_caches(share)
    elif sys.platform == "darwin" and not menu.is_symlink():
        if (menu / "Contents/Info.plist").is_file():
            shutil.rmtree(menu)


def _launcher_script(home: Path, launcher: Path) -> str:
    command = shlex.join([str(launcher), "app", "--data-home", str(home)])
    return f'#!/bin/sh\nexec {command} "$@"\n'


def _shell_launcher(path: Path, home: Path, launcher: Path) -> None:
    private_write(path, _launcher_script(home, launcher))
    path.chmod(0o755)


def _linux_exec(project: Path) -> str:
    return autostart._desktop_arg(str(project / "launch").replace("%", "%%"))


def _register_linux(menu: Path, project: Path, source: Path) -> None:
    share = menu.parent.parent
    for name, destination in linux_icons(share):
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source / "assets" / name, destination)
    template = (source / "kova.desktop").read_text(encoding="utf-8")
    private_write(menu, template.replace("Exec=kova app\n", f"Exec={_linux_exec(project)}\n", 1))
    menu.chmod(0o644)
    refresh_linux_caches(share)


def _register_macos(menu: Path, project: Path, launcher: Path) -> None:
    contents = menu / "Contents"
    _shell_launcher(contents / "MacOS/Kova", project.parent, launcher)
    resources = contents / "Resources"
    resources.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(project / "assets/icon.icns", resources / "kova.icns")
    (contents / "Info.plist").write_bytes(plistlib.dumps({
        "CFBundleIdentifier": "io.github.wynxo.kova.launcher", "CFBundleName": "Kova",
        "CFBundleDisplayName": "Kova", "CFBundleExecutable": "Kova",
        "CFBundleIconFile": "kova.icns", "CFBundlePackageType": "APPL",
    }))
    if tool := shutil.which(str(LAUNCH_SERVICES)):
        subprocess.run([tool, "-f", str(menu)], check=True, timeout=15)


def _shortcut(menu: Path, binary: Path, project: Path, launcher: Path, *, verify=False) -> None:
    icon = project / "assets/icon.ico"
    if not icon.is_file():
        raise DesktopError("The staged Windows icon is missing.")
    arguments = subprocess.list2cmdline([str(project), f"--kova-home={project.parent}", f"--kova-cli={launcher}"])
    header = """
$ErrorActionPreference = 'Stop'
$shell = New-Object -ComObject WScript.Shell
$link = $shell.CreateShortcut($env:KOVA_SHORTCUT_PATH)
"""
    if verify:
        script = header + """
if ($link.TargetPath -ne $env:KOVA_SHORTCUT_TARGET -or
    $link.Arguments -ne $env:KOVA_SHORTCUT_ARGS -or
    ($link.IconLocation -replace ', *0$', ',0') -ne $env:KOVA_SHORTCUT_ICON) { exit 1 }
"""
    else:
        menu.parent.mkdir(parents=True, exist_ok=True)
        script = header + """
$link.TargetPath = $env:KOVA_SHORTCUT_TARGET
$link.Arguments = $env:KOVA_SHORTCUT_ARGS
$link.WorkingDirectory = $env:KOVA_SHORTCUT_CWD
$link.IconLocation = $env:KOVA_SHORTCUT_ICON
$link.Description = 'Private local AI assistant'
$link.Save()
ie4uinit.exe -show
"""
    env = dict(os.environ, KOVA_SHORTCUT_PATH=str(menu), KOVA_SHORTCUT_TARGET=str(binary),
               KOVA_SHORTCUT_ARGS=arguments, KOVA_SHORTCUT_CWD=str(project), KOVA_SHORTCUT_ICON=f"{icon},0")
    subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
                   env=env, check=True, timeout=15)


def refresh_desktop_integration(
    home: Path, launcher: Path, *, login: bool = False, legacy_source: Path | None = None,
) -> None:
    """Regenerate installed menu/icons or an already enabled login entry."""
    if login:
        autostart.enable(kova_home=home, python=str(build.tool_python()))
        state = autostart.status(kova_home=home, python=str(build.tool_python()))
        if state.stale or not state.enabled:
            raise DesktopError("The start-at-login entry could not be verified.")
        return

    menu = menu_path()
    project = home / "desktop"
    binary = _electron_binary(project)
    if menu is None:
        raise DesktopError("App-menu registration is unavailable on this platform.")
    # The old updater used a CLI hook. Repair its menu even if npm subsequently fails.
    if not binary.is_file() and not (sys.platform == "linux" and legacy_source is not None):
        raise DesktopError("The staged Electron binary is missing.")
    if not launcher.is_file():
        raise DesktopError("The installed kova command is missing. Re-run the Kova installer.")
    if sys.platform != "win32":
        _shell_launcher(project / "launch", home, launcher)
    if sys.platform == "linux":
        _register_linux(menu, project, legacy_source or project)
    elif sys.platform == "darwin":
        _register_macos(menu, project, launcher)
    else:
        _shortcut(menu, binary, project, launcher)


def verify_desktop_integration(home: Path, launcher: Path) -> None:
    menu = menu_path()
    project = home / "desktop"
    binary = _electron_binary(project)
    if menu is None or not menu.exists() or not binary.is_file():
        raise DesktopError("The launcher or Electron binary is missing.")
    expected_script = _launcher_script(home, launcher)
    if sys.platform == "linux":
        launch = project / "launch"
        correct = (f"Exec={_linux_exec(project)}" in menu.read_text(encoding="utf-8").splitlines()
                   and os.access(launch, os.X_OK)
                   and launch.read_text(encoding="utf-8") == expected_script
                   and all(icon.read_bytes() == (project / "assets" / name).read_bytes()
                           for name, icon in linux_icons(menu.parent.parent)))
    elif sys.platform == "darwin":
        contents = menu / "Contents"
        launch = contents / "MacOS/Kova"
        correct = (os.access(launch, os.X_OK)
                   and launch.read_text(encoding="utf-8") == expected_script
                   and plistlib.loads((contents / "Info.plist").read_bytes()).get("CFBundleExecutable") == "Kova"
                   and (contents / "Resources/kova.icns").read_bytes() == (project / "assets/icon.icns").read_bytes())
    else:
        _shortcut(menu, binary, project, launcher, verify=True)
        correct = True
    if not correct:
        raise DesktopError("The app-menu entry or its icons could not be verified.")
