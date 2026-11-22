"""Per-user desktop registration for updates. The Electron install is separate."""

from __future__ import annotations

import os
import plistlib
import shlex
import shutil
import struct
import subprocess
import sys
from pathlib import Path

from kova import autostart, build
from kova.app import DesktopError, _electron_binary
from kova.config import private_write

ICON_FILES = {32: "icon-32.png", 64: "icon-64.png", 256: "icon-256.png", 512: "icon.png"}
LAUNCH_SERVICES = Path("/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister")


def menu_path() -> Path | None:
    if sys.platform == "linux":
        return Path.home() / ".local/share/applications/kova.desktop"
    if sys.platform == "darwin":
        return Path.home() / "Applications/Kova.app"
    if sys.platform == "win32":
        return Path(os.environ["APPDATA"]) / "Microsoft/Windows/Start Menu/Programs/Kova.lnk"
    return None


def _write(path: Path, data: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    private_write(path, data)


def _shell_launcher(path: Path, binary: Path, project: Path, launcher: Path) -> None:
    _write(path, "#!/bin/sh\n"
           "unset ELECTRON_RUN_AS_NODE\n"
           f"export KOVA_HOME={shlex.quote(str(project.parent))}\n"
           f"export KOVA_DESKTOP_CLI={shlex.quote(str(launcher))}\n"
           f"exec {shlex.quote(str(binary))} {shlex.quote(str(project))}\n")
    path.chmod(0o755)


def _refresh_caches(commands: list[list[str]]) -> None:
    failed = []
    for program, *args in commands:
        if tool := shutil.which(program):
            if subprocess.run([tool, *args], check=False).returncode:
                failed.append(Path(program).name)
    if failed:
        raise DesktopError(f"Could not refresh the desktop cache ({', '.join(failed)}).")


def _register_linux(menu: Path, project: Path) -> None:
    icons = menu.parent.parent / "icons/hicolor"
    for size, name in ICON_FILES.items():
        destination = icons / f"{size}x{size}/apps/kova.png"
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(project / "assets" / name, destination)
    vector = icons / "scalable/apps/kova.svg"
    vector.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(project / "assets/icon.svg", vector)
    command = autostart._desktop_arg(str(project / "launch").replace("%", "%%"))
    _write(menu, "[Desktop Entry]\nType=Application\nName=Kova\nComment=Private local AI assistant\n"
           f"Exec={command}\nIcon=kova\nTerminal=false\nCategories=Utility;\nStartupWMClass=Kova\n")
    menu.chmod(0o644)
    _refresh_caches([
        ["update-desktop-database", str(menu.parent)],
        ["gtk-update-icon-cache", "-f", "-t", str(icons)],
    ])


def _register_macos(menu: Path, binary: Path, project: Path, launcher: Path) -> None:
    contents = menu / "Contents"
    _shell_launcher(contents / "MacOS/Kova", binary, project, launcher)
    resources = contents / "Resources"
    resources.mkdir(parents=True, exist_ok=True)
    chunks = []
    for size, code in [(32, b"icp5"), (64, b"icp6"), (256, b"ic08"), (512, b"ic09")]:
        png = (project / "assets" / ICON_FILES[size]).read_bytes()
        chunks.append(struct.pack(">4sI", code, len(png) + 8) + png)
    image = b"".join(chunks)
    (resources / "kova.icns").write_bytes(struct.pack(">4sI", b"icns", len(image) + 8) + image)
    (contents / "Info.plist").write_bytes(plistlib.dumps({
        "CFBundleIdentifier": "io.github.wynxo.kova.launcher", "CFBundleName": "Kova",
        "CFBundleDisplayName": "Kova", "CFBundleExecutable": "Kova",
        "CFBundleIconFile": "kova.icns", "CFBundlePackageType": "APPL",
    }))
    _refresh_caches([[str(LAUNCH_SERVICES), "-f", str(menu)]])


def _shortcut(menu: Path, binary: Path, project: Path, launcher: Path, *, verify=False) -> None:
    icon = project / "assets/kova.ico"
    arguments = subprocess.list2cmdline([str(project), f"--kova-home={project.parent}", f"--kova-cli={launcher}"])
    def ps(value: Path | str) -> str:
        return "'" + str(value).replace("'", "''") + "'"
    header = ("$ErrorActionPreference = 'Stop'; $shell = New-Object -ComObject WScript.Shell; "
              f"$link = $shell.CreateShortcut({ps(menu)}); ")
    if verify:
        script = header + (f"if ($link.TargetPath -ne {ps(binary)} -or $link.Arguments -ne {ps(arguments)}) "
                           "{ exit 1 }")
    else:
        menu.parent.mkdir(parents=True, exist_ok=True)
        pngs = [(size, (project / "assets" / name).read_bytes()) for size, name in ICON_FILES.items() if size <= 256]
        offset = 6 + len(pngs) * 16
        entries = []
        for size, png in pngs:
            entries.append(struct.pack("<BBBBHHII", size % 256, size % 256, 0, 0, 1, 32, len(png), offset))
            offset += len(png)
        icon.write_bytes(struct.pack("<HHH", 0, 1, len(pngs)) + b"".join(entries) + b"".join(png for _, png in pngs))
        script = header + (f"$link.TargetPath = {ps(binary)}; $link.Arguments = {ps(arguments)}; "
                           f"$link.WorkingDirectory = {ps(project)}; $link.IconLocation = {ps(str(icon) + ',0')}; "
                           "$link.Description = 'Private local AI assistant'; $link.Save(); "
                           "ie4uinit.exe -show")
    subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script], check=True)


def refresh_desktop_integration(home: Path, launcher: Path, *, login: bool = False) -> None:
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
    if not binary.is_file():
        raise DesktopError("The staged Electron binary is missing.")
    if sys.platform != "win32":
        _shell_launcher(project / "launch", binary, project, launcher)
    if sys.platform == "linux":
        _register_linux(menu, project)
    elif sys.platform == "darwin":
        _register_macos(menu, binary, project, launcher)
    else:
        _shortcut(menu, binary, project, launcher)


def verify_desktop_integration(home: Path, launcher: Path) -> None:
    menu = menu_path()
    project = home / "desktop"
    binary = _electron_binary(project)
    if menu is None or not menu.exists() or not binary.is_file():
        raise DesktopError("The launcher or Electron binary is missing.")
    if sys.platform == "linux":
        icons = menu.parent.parent / "icons/hicolor"
        command = autostart._desktop_arg(str(project / "launch").replace("%", "%%"))
        correct = (f"Exec={command}" in menu.read_text().splitlines()
                   and os.access(project / "launch", os.X_OK)
                   and all((icons / f"{size}x{size}/apps/kova.png").read_bytes()
                           == (project / "assets" / name).read_bytes() for size, name in ICON_FILES.items())
                   and (icons / "scalable/apps/kova.svg").is_file())
    elif sys.platform == "darwin":
        contents = menu / "Contents"
        correct = (os.access(contents / "MacOS/Kova", os.X_OK)
                   and plistlib.loads((contents / "Info.plist").read_bytes()).get("CFBundleExecutable") == "Kova"
                   and (contents / "Resources/kova.icns").read_bytes().startswith(b"icns"))
    else:
        _shortcut(menu, binary, project, launcher, verify=True)
        correct = (project / "assets/kova.ico").is_file()
    if not correct:
        raise DesktopError("The app-menu entry or its icons could not be verified.")
