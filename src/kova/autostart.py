"""Start Kova's server at login, per user, without administrator rights.

Each platform gets one small, inspectable entry:

- Linux: a systemd user unit when ``systemctl --user`` answers, otherwise an
  autostart ``.desktop`` file.
- macOS: a LaunchAgent plist.
- Windows: the per-user ``Run`` key, through the standard-library ``winreg``.

The functions never elevate. ``status`` only reads; ``on`` and ``off`` write or
remove just the one entry Kova owns.
"""

from __future__ import annotations

import os
import plistlib
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from kova.config import kova_home

try:  # stdlib only on Windows; importing this module on POSIX must still work.
    import winreg
except ImportError:  # pragma: no cover - exercised on POSIX
    winreg = None


LABEL = "ai.kova.server"
SYSTEMD_UNIT = "kova.service"
DESKTOP_FILE = "kova.desktop"
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_VALUE = "Kova"
SYSTEMD_PATH = Path(".config") / "systemd" / "user" / SYSTEMD_UNIT
DESKTOP_PATH = Path(".config") / "autostart" / DESKTOP_FILE
LAUNCH_AGENT_PATH = Path("Library") / "LaunchAgents" / f"{LABEL}.plist"

MECHANISMS = {
    "systemd": "systemd user unit",
    "desktop": "autostart .desktop entry",
    "launchagent": "LaunchAgent",
    "registry": "HKCU Run entry",
}


class AutostartError(Exception):
    """A start-at-login action could not be completed."""


@dataclass(frozen=True)
class AutostartStatus:
    """What start-at-login looks like on this machine right now."""

    platform: str
    mechanism: str
    installed: bool
    enabled: bool = False
    running: bool = False
    stale: bool = False
    path: str | None = None
    detail: str = ""

    @property
    def available(self) -> bool:
        return self.mechanism != "unsupported"

    @property
    def label(self) -> str:
        return MECHANISMS.get(self.mechanism, "start-at-login entry")

    def as_dict(self) -> dict:
        return {
            "platform": self.platform,
            "mechanism": self.mechanism,
            "installed": self.installed,
            "enabled": self.enabled,
            "running": self.running,
            "stale": self.stale,
            "path": self.path,
            "detail": self.detail,
        }


#: A shorter alias for callers that just want the status type.
Status = AutostartStatus


def _platform_name(platform: str | None = None) -> str:
    raw = (platform or sys.platform).lower()
    if raw.startswith("linux"):
        return "linux"
    if raw in {"darwin", "macos", "mac"} or raw.startswith("mac"):
        return "macos"
    if raw in {"win32", "win64", "windows", "cygwin", "msys"}:
        return "windows"
    return raw


def _home(home: Path | None = None) -> Path:
    return Path(home).expanduser() if home is not None else Path.home()


def _kova_home(path: Path | None = None) -> Path:
    return Path(path).expanduser() if path is not None else kova_home()


def _python(python: str | None = None) -> str:
    value = str(python or sys.executable or "python")
    path = Path(value).expanduser()
    return str(path if path.is_absolute() else path.absolute())


def _serve_command(python: str | None = None) -> list[str]:
    return [_python(python), "-m", "kova", "serve"]


def systemd_unit_path(home: Path | None = None) -> Path:
    return _home(home) / SYSTEMD_PATH


def desktop_entry_path(home: Path | None = None) -> Path:
    return _home(home) / DESKTOP_PATH


def launch_agent_path(home: Path | None = None) -> Path:
    return _home(home) / LAUNCH_AGENT_PATH


def _systemd_arg(value: str) -> str:
    """Quote one systemd command-line or Environment= value when needed."""
    if any(character.isspace() or character in '"\\%' for character in value):
        return '"' + value.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%") + '"'
    return value


def _unit_text(python: str | None, khome: Path) -> str:
    command = " ".join(_systemd_arg(part) for part in _serve_command(python))
    log = khome / "server.log"
    return (
        "[Unit]\n"
        "Description=Kova local AI server\n"
        "After=network.target\n"
        "\n"
        "[Service]\n"
        "Type=simple\n"
        f"ExecStart={command}\n"
        "Restart=on-failure\n"
        "RestartSec=5\n"
        f"Environment={_systemd_arg(f'KOVA_HOME={khome}')}\n"
        f"StandardOutput=append:{log}\n"
        f"StandardError=append:{log}\n"
        "\n"
        "[Install]\n"
        "WantedBy=default.target\n"
    )


def _desktop_arg(value: str) -> str:
    """Quote one Exec= argument using the desktop-entry rules."""
    if not any(character in ' \t\n"\'\\><~|&;$*?#()`' for character in value):
        return value
    escaped = (value.replace("\\", "\\\\").replace('"', '\\"')
               .replace("`", "\\`").replace("$", "\\$"))
    return f'"{escaped}"'


def _desktop_text(python: str | None) -> str:
    command = " ".join(_desktop_arg(part) for part in _serve_command(python))
    return (
        "[Desktop Entry]\n"
        "Type=Application\n"
        "Name=Kova\n"
        "Comment=Keep Kova's server running for reminders and phone access\n"
        f"Exec={command}\n"
        "Terminal=false\n"
        "X-GNOME-Autostart-enabled=true\n"
    )


def _plist_bytes(python: str | None, khome: Path) -> bytes:
    log = str(khome / "server.log")
    return plistlib.dumps({
        "Label": LABEL,
        "ProgramArguments": _serve_command(python),
        "EnvironmentVariables": {"KOVA_HOME": str(khome)},
        "RunAtLoad": True,
        "StandardOutPath": log,
        "StandardErrorPath": log,
    }, fmt=plistlib.FMT_XML, sort_keys=True)


def _windows_command(python: str | None = None) -> str:
    return subprocess.list2cmdline(_serve_command(python))


def _which(which, name: str) -> str | None:
    finder = which or shutil.which
    try:
        return finder(name)
    except OSError:
        return None


def _call(run, command: list[str]):
    """Run one user-level helper command, without a shell or a terminal."""
    runner = run or subprocess.run
    try:
        return runner(command, capture_output=True, text=True, timeout=10, check=False)
    except TypeError:
        # A small test double may accept only the command list.
        return runner(command)


def _systemctl_answers(which, run) -> bool:
    if not _which(which, "systemctl"):
        return False
    try:
        result = _call(run, ["systemctl", "--user", "show-environment"])
    except (OSError, subprocess.SubprocessError):
        return False
    return getattr(result, "returncode", 1) == 0


def _systemctl(run, *arguments: str) -> int:
    try:
        result = _call(run, ["systemctl", "--user", *arguments])
    except (OSError, subprocess.SubprocessError) as exc:
        raise AutostartError(f"systemctl could not {arguments[0]} Kova's user unit: {exc}") from exc
    return int(getattr(result, "returncode", 1) or 0)


def _launchctl(run, *arguments: str) -> int:
    try:
        result = _call(run, ["launchctl", *arguments])
    except (OSError, subprocess.SubprocessError) as exc:
        raise AutostartError(f"launchctl could not update Kova's LaunchAgent: {exc}") from exc
    return int(getattr(result, "returncode", 1) or 0)


def _write(path: Path, text: str | bytes) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.tmp")
        if isinstance(text, bytes):
            temporary.write_bytes(text)
        else:
            temporary.write_text(text)
        os.replace(temporary, path)
    except OSError as exc:
        raise AutostartError(f"Could not write {path}: {exc}") from exc


def _remove(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError as exc:
        raise AutostartError(f"Could not remove {path}: {exc}") from exc


def _registry(registry):
    if registry is not None:
        return registry
    if winreg is None:
        raise AutostartError("The Windows Run key is unavailable because Python's winreg module is missing.")
    return winreg


def _registry_open(module, access: int):
    try:
        return module.OpenKey(module.HKEY_CURRENT_USER, RUN_KEY, 0, access)
    except OSError:
        if not hasattr(module, "CreateKeyEx"):
            raise
        return module.CreateKeyEx(module.HKEY_CURRENT_USER, RUN_KEY, 0, access)


def _registry_close(module, key) -> None:
    closer = getattr(module, "CloseKey", None)
    if closer is not None:
        closer(key)


def _registry_set(module, command: str) -> None:
    key = None
    try:
        key = _registry_open(module, module.KEY_SET_VALUE)
        module.SetValueEx(key, RUN_VALUE, 0, module.REG_SZ, command)
    except OSError as exc:
        raise AutostartError(f"Could not write the Kova Run key: {exc}") from exc
    finally:
        if key is not None:
            _registry_close(module, key)


def _registry_get(module) -> str | None:
    key = None
    try:
        key = module.OpenKey(module.HKEY_CURRENT_USER, RUN_KEY, 0, module.KEY_READ)
        value = module.QueryValueEx(key, RUN_VALUE)
        return value[0] if isinstance(value, tuple) else value
    except OSError:
        return None
    finally:
        if key is not None:
            _registry_close(module, key)


def _registry_delete(module) -> None:
    key = None
    try:
        try:
            key = module.OpenKey(module.HKEY_CURRENT_USER, RUN_KEY, 0, module.KEY_SET_VALUE)
        except OSError:
            return
        try:
            module.DeleteValue(key, RUN_VALUE)
        except OSError:
            pass
    finally:
        if key is not None:
            _registry_close(module, key)


def _stale_systemd(path: Path, python: str | None, khome: Path) -> bool:
    try:
        return path.read_text() != _unit_text(python, khome)
    except OSError:
        return True


def _stale_desktop(path: Path, python: str | None) -> bool:
    try:
        return path.read_text() != _desktop_text(python)
    except OSError:
        return True


def _stale_launch_agent(path: Path, python: str | None, khome: Path) -> bool:
    try:
        return path.read_bytes() != _plist_bytes(python, khome)
    except OSError:
        return True


def _linux_enable(*, home: Path, khome: Path, python: str | None,
                  run, which) -> AutostartStatus:
    if _systemctl_answers(which, run):
        path = systemd_unit_path(home)
        _write(path, _unit_text(python, khome))
        # A previous fallback entry would start a second server at login.
        _remove(desktop_entry_path(home))
        code = _systemctl(run, "daemon-reload")
        if code != 0:
            raise AutostartError("Kova's systemd unit was written, but systemd could not reload it.")
        code = _systemctl(run, "enable", SYSTEMD_UNIT)
        if code != 0:
            raise AutostartError("Kova's systemd unit was written, but systemd could not enable it.")
        return AutostartStatus("linux", "systemd", True, enabled=True,
                               path=str(path), detail="systemd user unit")
    path = desktop_entry_path(home)
    _write(path, _desktop_text(python))
    # Do not leave a unit that a later systemd login would also start.
    _remove(systemd_unit_path(home))
    return AutostartStatus("linux", "desktop", True, enabled=True,
                           path=str(path), detail="autostart .desktop entry")


def _linux_disable(*, home: Path, run, which) -> AutostartStatus:
    unit = systemd_unit_path(home)
    desktop = desktop_entry_path(home)
    unit_exists = unit.exists()
    if unit_exists and _systemctl_answers(which, run):
        # Best effort: a unit that is not loaded has nothing to stop.
        _systemctl(run, "disable", "--now", SYSTEMD_UNIT)
    _remove(unit)
    _remove(desktop)
    return AutostartStatus("linux", "systemd" if unit_exists else "desktop", False)


def _linux_status(*, home: Path, khome: Path, python: str | None,
                  run, which) -> AutostartStatus:
    unit = systemd_unit_path(home)
    if unit.is_file():
        stale = _stale_systemd(unit, python, khome)
        enabled = running = False
        if not stale and _systemctl_answers(which, run):
            enabled = _systemctl(run, "is-enabled", SYSTEMD_UNIT) == 0
            running = _systemctl(run, "is-active", SYSTEMD_UNIT) == 0
        return AutostartStatus("linux", "systemd", True, enabled=enabled,
                               running=running, stale=stale, path=str(unit),
                               detail="the unit does not match this Kova install" if stale else "")
    desktop = desktop_entry_path(home)
    if desktop.is_file():
        stale = _stale_desktop(desktop, python)
        return AutostartStatus("linux", "desktop", True, enabled=not stale,
                               stale=stale, path=str(desktop),
                               detail="the entry does not match this Kova install" if stale else "")
    mechanism = "systemd" if _systemctl_answers(which, run) else "desktop"
    return AutostartStatus("linux", mechanism, False)


def _macos_enable(*, home: Path, khome: Path, python: str | None,
                  run, which) -> AutostartStatus:
    path = launch_agent_path(home)
    _write(path, _plist_bytes(python, khome))
    if _which(which, "launchctl"):
        uid = os.getuid() if hasattr(os, "getuid") else 0
        _launchctl(run, "bootstrap", f"gui/{uid}", str(path))
    return AutostartStatus("macos", "launchagent", True, enabled=True,
                           path=str(path), detail="LaunchAgent")


def _macos_disable(*, home: Path, run, which) -> AutostartStatus:
    path = launch_agent_path(home)
    existed = path.exists()
    if existed and _which(which, "launchctl"):
        uid = os.getuid() if hasattr(os, "getuid") else 0
        # Best effort: booting out an unloaded job is harmless.
        _launchctl(run, "bootout", f"gui/{uid}/{LABEL}")
    _remove(path)
    return AutostartStatus("macos", "launchagent", False)


def _macos_status(*, home: Path, khome: Path, python: str | None,
                  run, which) -> AutostartStatus:
    path = launch_agent_path(home)
    if not path.is_file():
        return AutostartStatus("macos", "launchagent", False)
    stale = _stale_launch_agent(path, python, khome)
    running = False
    if not stale and _which(which, "launchctl"):
        uid = os.getuid() if hasattr(os, "getuid") else 0
        running = _launchctl(run, "print", f"gui/{uid}/{LABEL}") == 0
    return AutostartStatus("macos", "launchagent", True, enabled=not stale,
                           running=running, stale=stale, path=str(path),
                           detail="the plist does not match this Kova install" if stale else "")


def _windows_enable(*, python: str | None, registry) -> AutostartStatus:
    module = _registry(registry)
    _registry_set(module, _windows_command(python))
    return AutostartStatus("windows", "registry", True, enabled=True,
                           detail="HKCU Run entry")


def _windows_disable(*, registry) -> AutostartStatus:
    module = _registry(registry)
    _registry_delete(module)
    return AutostartStatus("windows", "registry", False)


def _windows_status(*, python: str | None, registry) -> AutostartStatus:
    module = _registry(registry)
    value = _registry_get(module)
    if value is None:
        return AutostartStatus("windows", "registry", False)
    stale = value != _windows_command(python)
    return AutostartStatus("windows", "registry", True, enabled=not stale,
                           stale=stale, detail="the Run value does not match this Kova install"
                           if stale else "")


def _unsupported(platform: str) -> AutostartError:
    return AutostartError(
        f"Start at login isn't supported on {platform}; use your system's startup settings."
    )


def _details(*, home, khome, python, run, which) -> dict:
    return {"home": home, "khome": khome, "python": python, "run": run, "which": which}


def enable(platform: str | None = None, home: Path | None = None, *,
           kova_home: Path | None = None, python: str | None = None,
           run=None, which=None, registry=None) -> AutostartStatus:
    """Install Kova's per-user start-at-login entry, replacing only Kova's own."""
    name = _platform_name(platform)
    details = _details(home=_home(home), khome=_kova_home(kova_home),
                       python=python, run=run, which=which)
    if name == "linux":
        return _linux_enable(**details)
    if name == "macos":
        return _macos_enable(**details)
    if name == "windows":
        return _windows_enable(python=python, registry=registry)
    raise _unsupported(name)


def disable(platform: str | None = None, home: Path | None = None, *,
            kova_home: Path | None = None, python: str | None = None,
            run=None, which=None, registry=None) -> AutostartStatus:
    """Remove Kova's own start-at-login entry; nothing else is touched."""
    name = _platform_name(platform)
    if name == "linux":
        return _linux_disable(home=_home(home), run=run, which=which)
    if name == "macos":
        return _macos_disable(home=_home(home), run=run, which=which)
    if name == "windows":
        return _windows_disable(registry=registry)
    raise _unsupported(name)


def status(platform: str | None = None, home: Path | None = None, *,
           kova_home: Path | None = None, python: str | None = None,
           run=None, which=None, registry=None) -> AutostartStatus:
    """Read the current entry without changing anything."""
    name = _platform_name(platform)
    details = _details(home=_home(home), khome=_kova_home(kova_home),
                       python=python, run=run, which=which)
    if name == "linux":
        return _linux_status(**details)
    if name == "macos":
        return _macos_status(**details)
    if name == "windows":
        return _windows_status(python=python, registry=registry)
    raise _unsupported(name)


# Friendly aliases: the command is a toggle, the module reads like one too.
install = enable
remove = disable


def describe(state: AutostartStatus) -> str:
    """One honest, plain-language status line for any platform."""
    if state.stale:
        return (f"Start at login is stale: Kova's {state.label} "
                f"{state.detail or 'does not match this Kova install'}.")
    if not state.installed:
        return f"Start at login is off (no {state.label} installed)."
    bits = [state.label]
    if state.mechanism == "systemd":
        bits.append("enabled" if state.enabled else "installed but not enabled")
        bits.append("running now" if state.running else "not running now")
    elif state.mechanism == "launchagent":
        bits.append("loaded now" if state.running else "starts at next login")
    elif state.mechanism == "desktop":
        bits.append("starts at next login")
    return f"Start at login is on ({', '.join(bits)})."
