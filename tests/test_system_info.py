"""Tests for the read-only system information tool."""

from collections import namedtuple

import kova.tools.system_info as si
from kova.agent import select_tools
from kova.tools import Tool

CPUINFO = """
processor\t: 0
vendor_id\t: AuthenticAMD
model name\t: AMD Ryzen 7 5800X 8-Core Processor
cpu MHz\t\t: 3800.000
"""

MEMINFO = """MemTotal:       33554432 kB
MemFree:         1048576 kB
MemAvailable:   12582912 kB
Buffers:          524288 kB
Cached:          2097152 kB
"""

NVIDIA_SMI = """NVIDIA GeForce RTX 3070, 8192 MiB
NVIDIA GeForce RTX 3070, 8192 MiB
"""

LSPCI = """00:02.0 VGA compatible controller: Intel Corporation UHD Graphics 630 (rev 02)
01:00.0 3D controller: NVIDIA Corporation GA104M [GeForce RTX 3070 Mobile] (rev a1)
02:00.0 Ethernet controller: Realtek Semiconductor Co., Ltd. RTL8111/8168/8411 (rev 15)
"""


def test_parse_cpuinfo():
    assert si.parse_cpuinfo(CPUINFO) == "AMD Ryzen 7 5800X 8-Core Processor"
    assert si.parse_cpuinfo("processor: 0\nModel: Raspberry Pi 5 Model B Rev 1.0\n") == \
        "Raspberry Pi 5 Model B Rev 1.0"
    assert si.parse_cpuinfo("processor: 0\n") is None


def test_parse_meminfo():
    total, used = si.parse_meminfo(MEMINFO)
    assert total == 33554432 * 1024
    assert used == (33554432 - 12582912) * 1024
    # Without MemAvailable, used falls back to free + buffers + cached.
    total, used = si.parse_meminfo("MemTotal: 1000 kB\nMemFree: 100 kB\nBuffers: 50 kB\nCached: 50 kB\n")
    assert total == 1000 * 1024 and used == 800 * 1024
    assert si.parse_meminfo("") is None


def test_parse_uptime_and_boottime():
    assert si.parse_uptime("266400.00 12345.67\n") == 266400
    assert si.parse_uptime("") is None
    assert si.parse_uptime("nonsense") is None
    assert si.parse_boottime("{ sec = 1700000000, usec = 123456 } Mon Nov 13 2023") == 1700000000
    assert si.parse_boottime("garbage") is None


def test_parse_os_release():
    text = 'NAME="Ubuntu"\nPRETTY_NAME="Ubuntu 24.04.1 LTS"\nVERSION_ID="24.04"\n'
    assert si.parse_os_release(text) == "Ubuntu 24.04.1 LTS"
    assert si.parse_os_release("NAME=Arch\n") is None


def test_parse_nvidia_smi():
    assert si.parse_nvidia_smi(NVIDIA_SMI) == [
        ("NVIDIA GeForce RTX 3070", 8 * 1024 ** 3),
        ("NVIDIA GeForce RTX 3070", 8 * 1024 ** 3),
    ]
    assert si.parse_nvidia_smi("NVIDIA H100, 80 GiB\n") == [("NVIDIA H100", 80 * 1024 ** 3)]
    assert si.parse_nvidia_smi("no gpu here\n") == []


def test_parse_lspci():
    assert si.parse_lspci(LSPCI) == [
        "Intel Corporation UHD Graphics 630",
        "NVIDIA Corporation GA104M [GeForce RTX 3070 Mobile]",
    ]
    assert si.parse_lspci("00:00.0 Host bridge: Intel Corporation Device 1234\n") == []


def test_parse_windows_gpus():
    assert si.parse_windows_gpus(
        "Intel(R) UHD Graphics 630|1073741824\r\nNVIDIA GeForce RTX 3070|\r\n"
    ) == [
        ("Intel(R) UHD Graphics 630", 1073741824),
        ("NVIDIA GeForce RTX 3070", None),
    ]
    assert si.parse_windows_gpus("") == []


def test_parse_system_profiler():
    text = """Graphics/Displays:
    Apple M2:
      Chipset Model: Apple M2
      Type: GPU
      Bus: Built-In
"""
    assert si.parse_system_profiler(text) == ["Apple M2"]


def test_human_size_and_duration():
    assert si.human_size(512) == "512 B"
    assert si.human_size(1024) == "1.0 KB"
    assert si.human_size(1024 ** 3) == "1.0 GB"
    assert si.human_size(32 * 1024 ** 3) == "32.0 GB"
    assert si.human_size(5 * 1024 ** 4) == "5.0 TB"
    assert si.format_duration(0) == "0m"
    assert si.format_duration(59) == "0m"
    assert si.format_duration(60) == "1m"
    assert si.format_duration(3660) == "1h 1m"
    assert si.format_duration(266400) == "3d 2h 0m"


def test_summarize_linux(monkeypatch):
    texts = {
        "/proc/cpuinfo": CPUINFO,
        "/proc/meminfo": MEMINFO,
        "/proc/uptime": "266400.00 12345.67\n",
        "/etc/os-release": 'PRETTY_NAME="Ubuntu 24.04.1 LTS"\n',
    }
    monkeypatch.setattr(si, "_read_text", lambda path: texts.get(str(path), ""))

    def fake_run(command):
        return NVIDIA_SMI if command[0] == "nvidia-smi" else ""

    monkeypatch.setattr(si, "_run", fake_run)
    monkeypatch.setattr(si.sys, "platform", "linux")
    monkeypatch.setattr(si.platform, "system", lambda: "Linux")
    monkeypatch.setattr(si.platform, "release", lambda: "6.8.0-test")
    monkeypatch.setattr(si.platform, "version", lambda: "#1 SMP test")
    monkeypatch.setattr(si.os, "cpu_count", lambda: 16)
    usage = namedtuple("usage", "total used free")
    monkeypatch.setattr(
        si.shutil, "disk_usage",
        lambda path: usage(500 * 1024 ** 3, 200 * 1024 ** 3, 300 * 1024 ** 3),
    )

    assert si.summarize() == "\n".join([
        "OS: Linux 6.8.0-test (Ubuntu 24.04.1 LTS)",
        "CPU: AMD Ryzen 7 5800X 8-Core Processor (16 cores)",
        "RAM: 32.0 GB total, 20.0 GB used",
        "Disk (home): 500.0 GB total, 300.0 GB free (40% used)",
        "GPU: NVIDIA GeForce RTX 3070 (8.0 GB VRAM)",
        "Uptime: 3d 2h 0m",
    ])


def test_gpus_fall_back_to_lspci(monkeypatch):
    def fake_run(command):
        return LSPCI if command[0] == "lspci" else ""

    monkeypatch.setattr(si, "_run", fake_run)
    monkeypatch.setattr(si.sys, "platform", "linux")
    assert si._gpus() == [
        ("Intel Corporation UHD Graphics 630", None),
        ("NVIDIA Corporation GA104M [GeForce RTX 3070 Mobile]", None),
    ]


def test_gpu_line_dedupes_and_skips_vram_when_unknown(monkeypatch):
    def fake_run(command):
        return NVIDIA_SMI if command[0] == "nvidia-smi" else ""

    monkeypatch.setattr(si, "_run", fake_run)
    monkeypatch.setattr(si.sys, "platform", "linux")
    assert si._gpu_line() == "GPU: NVIDIA GeForce RTX 3070 (8.0 GB VRAM)"


def test_summarize_never_raises(monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError("broken")

    monkeypatch.setattr(si.sys, "platform", "linux")  # isolate all Linux probes
    monkeypatch.setattr(si, "_read_text", boom)
    monkeypatch.setattr(si, "_run", boom)
    monkeypatch.setattr(si.os, "cpu_count", boom)
    monkeypatch.setattr(si.platform, "system", boom)
    monkeypatch.setattr(si.platform, "release", boom)
    monkeypatch.setattr(si.platform, "version", boom)
    monkeypatch.setattr(si.platform, "processor", boom)
    monkeypatch.setattr(si.shutil, "disk_usage", boom)

    assert si.summarize() == "Could not read any system information on this machine."


def test_system_info_is_work_mode_only():
    tool = si.tools(None)[0]
    assert isinstance(tool, Tool) and tool.name == "system_info"
    assert tool.parameters == {"type": "object", "properties": {}}
    tools = {"system_info": tool, "web_search": tool}
    assert "system_info" in select_tools("work", False, tools)
    assert "system_info" not in select_tools("chat", True, tools)
