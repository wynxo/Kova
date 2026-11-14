"""The privacy check catches the leaks that actually happened, and lets placeholders through."""

from __future__ import annotations

import importlib.util
import struct
import zlib
from pathlib import Path

import pytest

SPEC = importlib.util.spec_from_file_location(
    "privacy_check", Path(__file__).resolve().parent.parent / "scripts" / "privacy_check.py")
privacy = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(privacy)


@pytest.mark.parametrize("text", [
    "files_path = \"/home/alice/Kova Files\"",
    "KOVA_HOME=/Users/bob/.kova kova stop",
    r"C:\Users\carol\AppData\Local\kova",
    "alice@fedora:~/projects/kova$ kova --help",
    "memory /tmp/claude-1000/-home-alice--t3-worktrees-Kova-abc",
    "folder ~/.t3/worktrees/Kova/t3-1234",
    "llama.cpp on homelab  http://192.168.178.29:8080/v1",
    "connect 10.20.30.40:11434",
])
def test_real_looking_details_are_flagged(text):
    assert privacy.scan_text(text, "x", set())


@pytest.mark.parametrize("text", [
    "files_path = \"~/Kova Files\"",
    "see /home/you/notes or /home/runner/work",
    "you@demo:~/projects/demo$ kova",
    "kova setup --url http://192.0.2.10:11434",
    'TOKEN="$(cat "$WORK/home/token")"',
    "bind to 0.0.0.0 or 127.0.0.1, never 169.254.169.254",
])
def test_placeholders_and_examples_pass(text):
    assert not privacy.scan_text(text, "x", set())


def test_redacted_report_does_not_repeat_the_leak():
    problems = privacy.scan_text("alice@fedora:~ at /home/alice and 10.20.30.40", "x", set(), redact=True)
    assert len(problems) == 3
    assert not any("alice" in p or "10.20.30.40" in p for p in problems)


def test_allow_list_admits_a_made_up_example():
    assert privacy.scan_text("try 192.168.1.50", "x", set())
    assert not privacy.scan_text("try 192.168.1.50", "x", {"192.168.1.50"})


def _chunk(kind: bytes, body: bytes) -> bytes:
    return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body))


def test_png_window_title_metadata_is_found():
    png = (b"\x89PNG\r\n\x1a\n" + _chunk(b"IHDR", b"\0" * 13)
           + _chunk(b"zTXt", b"WindowTitle\0\0" + zlib.compress(b"alice@fedora:~/kova"))
           + _chunk(b"IEND", b""))
    text = privacy.png_text(png)
    assert "alice@fedora" in text
    assert privacy.scan_text(text, "shot.png", set())


def test_the_repository_is_clean():
    assert privacy.scan_repo(privacy.allowed_ips()) == []
