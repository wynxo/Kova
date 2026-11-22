"""Identify installed builds even when several commits share a version."""

from __future__ import annotations

import hashlib
import json
import re
import sys
from dataclasses import dataclass
from importlib import metadata
from pathlib import Path

from kova import __version__

COMMIT = re.compile(r"[0-9a-f]{40}")


@dataclass(frozen=True)
class Build:
    version: str
    commit: str | None = None
    fingerprint: str | None = None

    def label(self) -> str:
        return f"{self.version} ({self.commit[:12] if self.commit else 'commit unknown'})"


def describe(dist: metadata.Distribution) -> Build:
    if not (version := dist.version):
        raise ValueError("Installed package metadata has no version.")
    try:
        origin = json.loads(dist.read_text("direct_url.json") or "{}")
        commit = origin.get("vcs_info", {}).get("commit_id")
        if not commit:
            match = re.search(r"/archive/([0-9a-f]{40})\.tar\.gz$", origin.get("url", ""))
            commit = match[1] if match else dist.read_text("kova-source-commit")
        if not isinstance(commit, str) or not COMMIT.fullmatch(commit):
            commit = None
    except (AttributeError, TypeError, ValueError):
        commit = None
    # RECORD contains the wheel's content hashes. This also detects local
    # reinstalls and same-version builds whose Git commit is unavailable.
    record = dist.read_text("RECORD")
    fingerprint = None
    if record:
        package_files = sorted(line for line in record.splitlines() if line.startswith("kova/"))
        if package_files:
            fingerprint = hashlib.sha256("\n".join([version, commit or "", *package_files]).encode()).hexdigest()
    return Build(version, commit, fingerprint)


def installed() -> Build:
    try:
        return describe(metadata.distribution("kova-ai"))
    except (metadata.PackageNotFoundError, OSError, ValueError):
        return Build(__version__)


def tool_dir() -> Path:
    return Path.home() / ".local/share/uv/tools/kova-ai"


def tool_python() -> Path:
    return tool_dir() / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")


def tool_distribution() -> Path | None:
    for pattern in ("Lib/site-packages/kova_ai-*.dist-info", "lib/python*/site-packages/kova_ai-*.dist-info"):
        if path := next(tool_dir().glob(pattern), None):
            return path
    return None


def tool_build() -> Build | None:
    if path := tool_distribution():
        return describe(metadata.Distribution.at(path))
    return None


def managed() -> bool:
    return Path(sys.prefix).resolve() == tool_dir().resolve()
