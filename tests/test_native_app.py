"""Installing and launching the native app (`kova update`, `kova app`).

Downloads go through ``httpx.MockTransport``; the "app" is a tiny shell script.
"""
from __future__ import annotations

import hashlib
import io
import os
import tarfile

import httpx
import pytest

from kova import app, native_app

posix_only = pytest.mark.skipif(os.name == "nt", reason="the stand-in app is a shell script")


def archive(script: str) -> bytes:
    data = script.encode()
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        info = tarfile.TarInfo("kova-app")
        info.size, info.mode = len(data), 0o755
        tar.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


def serve(files: dict[str, bytes], seen: list | None = None) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        name = request.url.path.rsplit("/", 1)[-1]
        if seen is not None:
            seen.append(name)
        return httpx.Response(200, content=files[name]) if name in files else httpx.Response(404)
    return httpx.MockTransport(handler)


@pytest.fixture
def linux(monkeypatch):
    monkeypatch.setattr(native_app, "target", lambda: "linux-x64")
    return "kova-app-linux-x64.tar.gz"


def release(blob: bytes, name: str, digest: str | None = None) -> dict[str, bytes]:
    return {name: blob, native_app.SUMS: f"{digest or hashlib.sha256(blob).hexdigest()}  {name}\n".encode()}


def test_targets_and_sums():
    assert native_app.archive_name("windows-x64") == "kova-app-windows-x64.zip"
    assert native_app.archive_name("linux-x64") == "kova-app-linux-x64.tar.gz"
    good = "a" * 64
    assert native_app.parse_sums(f"{good}  one.tar.gz\n{good} *two.zip\nnot a line\n{'z' * 64}  bad") == {
        "one.tar.gz": good, "two.zip": good}


def test_install_verifies_swaps_in_and_skips_when_current(tmp_path, linux):
    blob = archive("#!/bin/sh\nexit 0\n")
    seen = []
    assert native_app.install(tmp_path, transport=serve(release(blob, linux), seen)) == "Installed the native app."
    exe = native_app.binary(tmp_path)
    assert exe and exe.read_bytes().startswith(b"#!/bin/sh")
    assert native_app.installed_sha(tmp_path) == hashlib.sha256(blob).hexdigest()
    seen.clear()
    assert native_app.install(tmp_path, transport=serve(release(blob, linux), seen)) == "Native app is up to date."
    assert seen == [native_app.SUMS]  # nothing re-downloaded


def test_a_bad_checksum_installs_nothing_and_keeps_the_old_app(tmp_path, linux):
    native_app.install(tmp_path, transport=serve(release(archive("#!/bin/sh\necho old\n"), linux)))
    with pytest.raises(native_app.NativeAppError, match="checksum"):
        native_app.install(tmp_path, transport=serve(release(archive("#!/bin/sh\necho new\n"), linux, "0" * 64)))
    assert b"old" in native_app.binary(tmp_path).read_bytes()


def test_a_missing_build_or_release_says_so(tmp_path, linux, monkeypatch):
    with pytest.raises(native_app.NativeAppError, match="HTTP 404"):
        native_app.install(tmp_path, transport=serve({}))
    assert "no build for this system" in native_app.install(tmp_path, transport=serve({native_app.SUMS: b""}))
    monkeypatch.setattr(native_app, "target", lambda: None)
    assert "classic app" in native_app.install(tmp_path, transport=serve({}))


@posix_only
def test_launch_reports_an_app_that_quits_at_once(tmp_path, linux, monkeypatch):
    monkeypatch.setattr(native_app, "START_GRACE", 1.0)
    assert native_app.launch(tmp_path) is False  # nothing installed
    native_app.install(tmp_path, transport=serve(release(archive("#!/bin/sh\nexit 3\n"), linux)))
    assert native_app.launch(tmp_path) is False
    native_app.install(tmp_path, transport=serve(release(archive("#!/bin/sh\nsleep 3\n"), linux)))
    assert native_app.launch(tmp_path) is True


def test_kova_app_prefers_native_and_falls_back(tmp_path, monkeypatch):
    monkeypatch.setenv("KOVA_HOME", str(tmp_path))
    calls = []
    monkeypatch.setattr(app, "launch_desktop", lambda extra: calls.append(("classic", extra)) or 0)
    monkeypatch.setattr(native_app, "binary", lambda home: tmp_path / "kova-app")
    monkeypatch.setattr(native_app, "launch", lambda home, cli: calls.append(("native",)) or True)
    with pytest.raises(SystemExit):
        app.main([])
    assert calls == [("native",)]
    calls.clear()
    with pytest.raises(SystemExit):
        app.main(["--classic"])
    assert calls == [("classic", [])]
    calls.clear()
    monkeypatch.setattr(native_app, "launch", lambda home, cli: calls.append(("native",)) or False)
    with pytest.raises(SystemExit):
        app.main([])
    assert calls == [("native",), ("classic", [])]
