"""Install and launch Kova's native app (``native/``, Rust on Rune).

CI builds the app for each supported system on every merge to ``main`` and
publishes the archives and a ``SHA256SUMS`` file as the ``native-latest``
GitHub release of this repository (``.github/workflows/native-release.yml``).
:func:`install` fetches the list, downloads this system's archive, refuses it
unless its SHA-256 matches, unpacks it safely and swaps it in atomically. It
is the same trust as ``kova update`` itself, which installs this repository's
code from GitHub.

:func:`launch` starts the app detached and reports whether it stayed up, so
``kova app`` can fall back to the classic desktop app on a machine where it
cannot start (no GPU driver, a missing system library).
"""
from __future__ import annotations

import hashlib
import json
import os
import platform
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx

from kova import egress

RELEASE = "https://github.com/wynxo/Kova/releases/download/native-latest/"
SUMS = "SHA256SUMS"
MAX_ARCHIVE = 300 * 1024 * 1024
#: How long a fresh start may take to fail before we call it started.
START_GRACE = 4.0


class NativeAppError(RuntimeError):
    """A plain reason the native app could not be installed or started."""


def target() -> str | None:
    """This system's build name, or ``None`` when no build is published for it."""
    machine = platform.machine().lower()
    arch = {"x86_64": "x64", "amd64": "x64", "aarch64": "arm64", "arm64": "arm64"}.get(machine)
    system = {"Linux": "linux", "Darwin": "macos", "Windows": "windows"}.get(platform.system())
    key = f"{system}-{arch}" if system and arch else None
    return key if key in ("linux-x64", "macos-arm64", "windows-x64") else None


def archive_name(key: str) -> str:
    return f"kova-app-{key}." + ("zip" if key.startswith("windows") else "tar.gz")


def root(home: Path) -> Path:
    return Path(home) / "native"


def binary(home: Path) -> Path | None:
    """The installed app, if a complete install is present."""
    folder = root(home) / "current"
    if not (folder / ".complete").is_file():
        return None
    # The name follows the build that was installed, as install() chose it.
    for name in ("kova-app.exe", "kova-app") if os.name == "nt" else ("kova-app", "kova-app.exe"):
        if (folder / name).is_file():
            return folder / name
    return None


def installed_sha(home: Path) -> str | None:
    try:
        return json.loads((root(home) / "installed.json").read_text()).get("sha256")
    except (OSError, ValueError, AttributeError):
        return None


def _get(client: httpx.Client, url: str, limit: int) -> bytes:
    with client.stream("GET", url, follow_redirects=True) as response:
        if response.status_code != 200:
            raise NativeAppError(f"GitHub answered HTTP {response.status_code} for {url.rsplit('/', 1)[-1]}.")
        chunks, size = [], 0
        for chunk in response.iter_bytes():
            size += len(chunk)
            if size > limit:
                raise NativeAppError("The download is larger than expected; nothing was installed.")
            chunks.append(chunk)
        return b"".join(chunks)


def parse_sums(text: str) -> dict[str, str]:
    """``sha256  name`` lines (sha256sum's format) as ``{name: sha256}``."""
    sums = {}
    for line in text.splitlines():
        parts = line.strip().split()
        if len(parts) == 2 and len(parts[0]) == 64 and all(c in "0123456789abcdef" for c in parts[0]):
            sums[parts[1].lstrip("*")] = parts[0]
    return sums


def install(home: Path, *, transport: httpx.BaseTransport | None = None) -> str:
    """Install or refresh the native app; returns one line saying what happened."""
    key = target()
    if key is None:
        return "No native build for this system yet; the classic app stays in use."
    name = archive_name(key)
    with egress.Client(transport=transport, timeout=httpx.Timeout(30, read=120)) as client:
        try:
            sums = parse_sums(_get(client, RELEASE + SUMS, 64 * 1024).decode("utf-8", "replace"))
        except httpx.HTTPError as exc:
            raise NativeAppError(f"Could not reach GitHub for the native app: {type(exc).__name__}.") from None
        expected = sums.get(name)
        if expected is None:
            return "The latest native release has no build for this system yet; the classic app stays in use."
        if expected == installed_sha(home) and binary(home):
            return "Native app is up to date."
        try:
            data = _get(client, RELEASE + name, MAX_ARCHIVE)
        except httpx.HTTPError as exc:
            raise NativeAppError(f"Could not download the native app: {type(exc).__name__}.") from None
    if hashlib.sha256(data).hexdigest() != expected:
        raise NativeAppError("The native app's checksum did not match; nothing was installed.")
    folder = root(home)
    folder.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=".unpack-", dir=folder))
    try:
        archive = staging / name
        archive.write_bytes(data)
        unpacked = staging / "app"
        unpacked.mkdir()
        from kova.engine_downloads import _extract
        try:
            _extract(archive, unpacked)
        except Exception as exc:
            raise NativeAppError("The native app's archive could not be unpacked safely.") from exc
        exe = unpacked / ("kova-app.exe" if key.startswith("windows") else "kova-app")
        if not exe.is_file():
            raise NativeAppError("The native app's archive has no kova-app program.")
        if os.name != "nt":
            exe.chmod(0o755)
        (unpacked / ".complete").write_text(expected)
        current, old = folder / "current", folder / ".old"
        shutil.rmtree(old, ignore_errors=True)
        if current.exists():
            current.replace(old)
        unpacked.replace(current)
        shutil.rmtree(old, ignore_errors=True)
        (folder / "installed.json").write_text(json.dumps({"sha256": expected, "target": key}))
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    return "Installed the native app."


def launch(home: Path, cli: Path | None = None) -> bool:
    """Start the native app detached; ``False`` when it is missing or quits at once."""
    exe = binary(home)
    if exe is None:
        return False
    env = dict(os.environ)
    env["KOVA_HOME"] = str(home)
    if cli is not None and cli.is_file():
        env["KOVA_CLI"] = str(cli)
    log = root(home) / "app.log"
    flags: dict = {}
    if os.name == "nt":
        flags["creationflags"] = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    else:
        flags["start_new_session"] = True
    try:
        with log.open("ab") as output:
            process = subprocess.Popen([str(exe)], env=env, stdin=subprocess.DEVNULL, stdout=output,
                                       stderr=output, cwd=str(exe.parent), **flags)
    except OSError:
        return False
    deadline = time.monotonic() + START_GRACE
    while time.monotonic() < deadline:
        code = process.poll()
        if code is not None:
            return code == 0
        time.sleep(0.1)
    return True


def main(argv: list[str] | None = None) -> int:
    """``python -m kova.native_app install``: what ``kova update`` runs."""
    from kova.config import kova_home
    args = argv if argv is not None else sys.argv[1:]
    if args != ["install"]:
        print("usage: python -m kova.native_app install", file=sys.stderr)
        return 2
    try:
        print(install(kova_home().resolve()))
    except NativeAppError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
