"""A short read-only summary of the user's computer."""

from __future__ import annotations

import ctypes
import os
import platform
import shutil
import subprocess
import sys
import time

from . import Tool, ToolContext

_COMMAND_TIMEOUT = 3
_SUBPROCESS_FLAGS = (
    {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)}
    if sys.platform == "win32" else {}
)
_WINDOWS_GPU_COMMAND = (
    'Get-CimInstance Win32_VideoController | '
    'ForEach-Object { "$($_.Name)|$($_.AdapterRAM)" }'
)
_WINDOWS_CPU_COMMAND = "(Get-CimInstance Win32_Processor).Name"


class _MemoryStatusEx(ctypes.Structure):
    """The Win32 MEMORYSTATUSEX struct, filled by GlobalMemoryStatusEx."""

    _fields_ = [
        ("dwLength", ctypes.c_ulong),
        ("dwMemoryLoad", ctypes.c_ulong),
        ("ullTotalPhys", ctypes.c_ulonglong),
        ("ullAvailPhys", ctypes.c_ulonglong),
        ("ullTotalPageFile", ctypes.c_ulonglong),
        ("ullAvailPageFile", ctypes.c_ulonglong),
        ("ullTotalVirtual", ctypes.c_ulonglong),
        ("ullAvailVirtual", ctypes.c_ulonglong),
        ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
    ]


def _read_text(path: str) -> str:
    """Read a small text file, returning empty on any error."""
    try:
        with open(path, encoding="utf-8", errors="replace") as file:
            return file.read(1024 * 1024)
    except OSError:
        return ""


def _run(command: list[str]) -> str:
    """Run a short read-only command and return its stdout, or "" on any error."""
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=_COMMAND_TIMEOUT,
            check=False,
            **_SUBPROCESS_FLAGS,
        )
    except (OSError, subprocess.SubprocessError, ValueError):
        return ""
    return (result.stdout or "") if result.returncode == 0 else ""


def human_size(size: int | float) -> str:
    """Format a byte count as a short human size, e.g. ``31.3 GB``."""
    value = float(size)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{int(value)} B" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} TB"


def format_duration(seconds: int) -> str:
    """Format a number of seconds as ``3d 2h 5m``, ``2h 5m`` or ``5m``."""
    minutes = max(int(seconds), 0) // 60
    days, rest = divmod(minutes, 24 * 60)
    hours, minutes = divmod(rest, 60)
    if days:
        return f"{days}d {hours}h {minutes}m"
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"


def parse_cpuinfo(text: str) -> str | None:
    """The CPU model from /proc/cpuinfo (x86, ARM and PowerPC spellings)."""
    keys = ("model name", "Model", "cpu model", "Hardware")
    for line in text.splitlines():
        key, separator, value = line.partition(":")
        if separator and key.strip() in keys:
            value = " ".join(value.split())
            if value:
                return value
    return None


def parse_meminfo(text: str) -> tuple[int, int] | None:
    """Total and used RAM in bytes from /proc/meminfo."""
    values: dict[str, int] = {}
    for line in text.splitlines():
        key, separator, rest = line.partition(":")
        fields = rest.split()
        if not separator or not fields or not fields[0].isdigit():
            continue
        values[key.strip()] = int(fields[0]) * 1024
    total = values.get("MemTotal")
    if not total:
        return None
    available = values.get("MemAvailable")
    if available is None:
        available = (values.get("MemFree", 0) + values.get("Buffers", 0)
                     + values.get("Cached", 0))
    return total, max(total - available, 0)


def parse_uptime(text: str) -> int | None:
    """Seconds since boot from /proc/uptime."""
    fields = text.split()
    if not fields:
        return None
    try:
        seconds = float(fields[0])
    except ValueError:
        return None
    return int(seconds) if seconds >= 0 else None


def parse_boottime(text: str) -> int | None:
    """The boot timestamp from macOS ``sysctl kern.boottime`` output."""
    marker = "sec ="
    index = text.find(marker)
    if index < 0:
        return None
    digits = ""
    for char in text[index + len(marker):].lstrip():
        if not char.isdigit():
            break
        digits += char
    return int(digits) if digits else None


def parse_os_release(text: str) -> str | None:
    """The PRETTY_NAME from /etc/os-release, e.g. ``Ubuntu 24.04.1 LTS``."""
    for line in text.splitlines():
        key, separator, value = line.partition("=")
        if separator and key.strip() == "PRETTY_NAME":
            value = value.strip().strip("\"'").strip()
            if value:
                return value
    return None


def parse_nvidia_smi(text: str) -> list[tuple[str, int | None]]:
    """GPU names and VRAM in bytes from ``nvidia-smi --query-gpu`` CSV output."""
    gpus: list[tuple[str, int | None]] = []
    for line in text.splitlines():
        name, separator, memory = line.rpartition(",")
        name = " ".join(name.split())
        amount, _, unit = memory.strip().partition(" ")
        if not separator or not name or not amount.isdigit():
            continue
        size = int(amount)
        if unit.strip().lower().startswith("gib"):
            size *= 1024 ** 3
        else:
            size *= 1024 ** 2
        gpus.append((name, size))
    return gpus


def parse_lspci(text: str) -> list[str]:
    """Display adapter names from ``lspci`` output."""
    names: list[str] = []
    labels = ("vga", "3d controller", "display controller")
    for line in text.splitlines():
        label, separator, value = line.partition(": ")
        if not separator or not any(marker in label.lower() for marker in labels):
            continue
        value = value.strip()
        if value.endswith(")") and "(rev " in value:
            value = value[:value.rindex("(rev ")].strip()
        if value and value not in names:
            names.append(value)
    return names


def parse_windows_gpus(text: str) -> list[tuple[str, int | None]]:
    """GPU names and VRAM from the PowerShell ``Name|AdapterRAM`` lines."""
    gpus: list[tuple[str, int | None]] = []
    for line in text.splitlines():
        name, separator, memory = line.partition("|")
        name = " ".join(name.split())
        if not separator or not name:
            continue
        amount, _, _ = memory.strip().partition(".")
        size = int(amount) if amount.isdigit() else 0
        gpus.append((name, size if size > 0 else None))
    return gpus


def parse_system_profiler(text: str) -> list[str]:
    """GPU names from macOS ``system_profiler SPDisplaysDataType`` output."""
    names: list[str] = []
    for line in text.splitlines():
        key, separator, value = line.partition(":")
        if separator and key.strip() == "Chipset Model":
            value = " ".join(value.split())
            if value and value not in names:
                names.append(value)
    return names


def _os_line() -> str | None:
    """The OS name, its version and the distro name where there is one."""
    system = (platform.system() or "").strip()
    release = (platform.release() or "").strip()
    name = " ".join(part for part in (system, release) if part)

    detail = None
    if system == "Linux":
        detail = parse_os_release(_read_text("/etc/os-release"))
    elif system:
        detail = (platform.version() or "").strip() or None

    if not name and not detail:
        return None
    if name and detail:
        return f"OS: {name} ({detail})"
    return f"OS: {name or detail}"


def _cpu_model() -> str | None:
    """A human CPU model name for the current platform, when it can be found."""
    model = None
    if sys.platform == "linux":
        model = parse_cpuinfo(_read_text("/proc/cpuinfo"))
    elif sys.platform == "win32":
        model = _run(["powershell", "-NoProfile", "-NonInteractive", "-Command",
                      _WINDOWS_CPU_COMMAND]).strip() or None
    elif sys.platform == "darwin":
        model = _run(["sysctl", "-n", "machdep.cpu.brand_string"]).strip() or None
    if not model:
        model = (platform.processor() or "").strip() or None
    if not model:
        return None
    model = " ".join(model.split())
    for suffix in (", GenuineIntel", ", AuthenticAMD"):
        model = model.removesuffix(suffix)
    return model or None


def _cpu_line() -> str | None:
    model = _cpu_model()
    cores = os.cpu_count()
    if model and cores:
        noun = "core" if cores == 1 else "cores"
        return f"CPU: {model} ({cores} {noun})"
    if model:
        return f"CPU: {model}"
    if cores:
        noun = "core" if cores == 1 else "cores"
        return f"CPU: {cores} {noun}"
    return None


def _ram() -> tuple[int, int | None] | None:
    """Total and used RAM in bytes; used is None when it cannot be measured."""
    if sys.platform == "linux":
        memory = parse_meminfo(_read_text("/proc/meminfo"))
        if memory:
            return memory
    elif sys.platform == "win32":
        try:
            status = _MemoryStatusEx()
            status.dwLength = ctypes.sizeof(_MemoryStatusEx)
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
                total = int(status.ullTotalPhys)
                if total:
                    return total, max(total - int(status.ullAvailPhys), 0)
        except (AttributeError, OSError, ValueError):
            pass
    elif sys.platform == "darwin":
        total = _run(["sysctl", "-n", "hw.memsize"]).strip()
        if total.isdigit() and int(total):
            return int(total), None

    # Any POSIX system can at least report the installed total.
    try:
        total = os.sysconf("SC_PHYS_PAGES") * os.sysconf("SC_PAGE_SIZE")
    except (AttributeError, OSError, ValueError):
        return None
    return (int(total), None) if total > 0 else None


def _ram_line() -> str | None:
    memory = _ram()
    if not memory:
        return None
    total, used = memory
    if used is None:
        return f"RAM: {human_size(total)} total"
    return f"RAM: {human_size(total)} total, {human_size(used)} used"


def _disk_line() -> str | None:
    """Total and free space on the volume holding the home folder."""
    usage = shutil.disk_usage(os.path.expanduser("~"))
    total, free = int(usage.total), int(usage.free)
    if total <= 0:
        return None
    used_percent = (total - free) * 100 // total
    return (f"Disk (home): {human_size(total)} total, {human_size(free)} free "
            f"({used_percent}% used)")


def _gpus() -> list[tuple[str, int | None]]:
    """GPU names and VRAM in bytes, trying each platform's usual source."""
    gpus = parse_nvidia_smi(_run([
        "nvidia-smi", "--query-gpu=name,memory.total", "--format=csv,noheader",
    ]))
    if gpus:
        return gpus
    if sys.platform == "linux":
        return [(name, None) for name in parse_lspci(_run(["lspci"]))]
    if sys.platform == "win32":
        return parse_windows_gpus(_run(["powershell", "-NoProfile", "-NonInteractive",
                                        "-Command", _WINDOWS_GPU_COMMAND]))
    if sys.platform == "darwin":
        displays = _run(["system_profiler", "SPDisplaysDataType"])
        return [(name, None) for name in parse_system_profiler(displays)]
    return []


def _gpu_line() -> str | None:
    entries: list[str] = []
    seen: set[str] = set()
    for name, memory in _gpus():
        name = " ".join(name.split())
        if not name or name.lower() in seen:
            continue
        seen.add(name.lower())
        entries.append(f"{name} ({human_size(memory)} VRAM)" if memory else name)
    return f"GPU: {'; '.join(entries)}" if entries else None


def _uptime_seconds() -> int | None:
    """Seconds since the last boot, when the platform can tell us."""
    if sys.platform == "linux":
        seconds = parse_uptime(_read_text("/proc/uptime"))
        if seconds is not None:
            return seconds
        try:
            return int(time.clock_gettime(time.CLOCK_BOOTTIME))
        except (AttributeError, OSError):
            return None
    if sys.platform == "win32":
        try:
            kernel32 = ctypes.windll.kernel32
            kernel32.GetTickCount64.restype = ctypes.c_ulonglong
            return int(kernel32.GetTickCount64() // 1000)
        except (AttributeError, OSError):
            return None
    if sys.platform == "darwin":
        boot = parse_boottime(_run(["sysctl", "-n", "kern.boottime"]))
        if boot is not None:
            return max(int(time.time()) - boot, 0)
    return None


def _uptime_line() -> str | None:
    seconds = _uptime_seconds()
    if seconds is None:
        return None
    return f"Uptime: {format_duration(seconds)}"


def summarize() -> str:
    """Build the plain-text system summary. Broken sections are simply left out."""
    sections = (_os_line, _cpu_line, _ram_line, _disk_line, _gpu_line, _uptime_line)
    lines: list[str] = []
    for section in sections:
        try:
            line = section()
        except Exception:  # a broken section must never fail a chat turn
            line = None
        if line:
            lines.append(line)
    return "\n".join(lines) or "Could not read any system information on this machine."


def tools(ctx: ToolContext) -> list[Tool]:
    """Create the system information tool for a context."""
    def system_info(args: dict) -> str:
        return summarize()

    return [
        Tool(
            "system_info",
            "Report the user's computer: operating system and version, CPU model and "
            "cores, RAM total and used, free disk space for the home folder, GPU name "
            "and VRAM when detectable, and uptime. Read-only. Use it for questions like "
            "'how much RAM do I have?', 'what GPU is in my PC?' or 'how full is my disk?'.",
            {"type": "object", "properties": {}},
            system_info,
        ),
    ]
