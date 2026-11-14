#!/usr/bin/env python3
"""Fail if machine-identifying details are about to go public.

Kova's repository is public. Agents and people writing docs, tests, issues and
PR descriptions tend to paste real terminal output, which carries the author's
home folder, user@host prompt and home-network addresses. This check catches
those before they land.

Usage:
    python scripts/privacy_check.py              # every tracked file + PNG metadata
    python scripts/privacy_check.py --text FILE  # one text blob (e.g. a PR body)

Example values that are fine: ``~/``, ``/home/you``, ``you@demo``, and the
documentation ranges 192.0.2.x, 198.51.100.x and 203.0.113.x. Any other private
IPv4 address must be listed in ``.privacy-allow`` (one per line) on purpose.
"""

from __future__ import annotations

import argparse
import ipaddress
import re
import subprocess
import sys
import zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# Placeholder account names that are clearly not a real person's.
SAFE_USERS = {"you", "user", "username", "me", "demo", "runner", "example",
              "your-name", "yourname", "name", "<user>", "<you>", "<name>", "$user", "${user}"}

HOME = re.compile(r"(?:(?<![\w$}./])/home/|(?<![\w$}./])/Users/|\b[A-Za-z]:\\\\?Users\\\\?)([A-Za-z0-9_.$<>{}-]+)")
PROMPT = re.compile(r"\b([a-z_][a-z0-9_-]{1,31})@([a-z0-9][a-z0-9-]{1,62}):[~/]")
AGENT_TMP = re.compile(r"/tmp/claude-\d+/|\.t3/worktrees/|-home-[a-z0-9_]+-")
IPV4 = re.compile(r"(?<![\d.])(\d{1,3}(?:\.\d{1,3}){3})(?![\d.])")
SAFE_HOSTS = {"demo", "host", "example", "localhost", "machine", "laptop", "pc"}

SKIP_SUFFIXES = {".lock", ".woff", ".woff2", ".ttf", ".ico", ".svg", ".gif", ".jpg", ".jpeg"}
# The checker, its tests and the policy that lists the forbidden shapes.
SKIP_FILES = {"scripts/privacy_check.py", "tests/test_privacy_check.py", "AGENTS.md"}


def allowed_ips() -> set[str]:
    path = ROOT / ".privacy-allow"
    if not path.exists():
        return set()
    return {line.split("#")[0].strip() for line in path.read_text().splitlines()
            if line.split("#")[0].strip()}


def scan_text(text: str, where: str, allow: set[str], redact: bool = False) -> list[str]:
    """Problems found in text; ``redact`` leaves the matched value out of the message."""
    def show(value: str) -> str:
        return "" if redact else f" {value!r}"

    problems = []
    for number, line in enumerate(text.splitlines(), 1):
        at = f"{where}:{number}"
        for match in HOME.finditer(line):
            if match.group(1).lower() not in SAFE_USERS:
                problems.append(f"{at}: real-looking home folder{show(match.group(0))} (use ~/ or /home/you)")
        for match in PROMPT.finditer(line):
            user, host = match.group(1), match.group(2)
            if user not in SAFE_USERS or host not in SAFE_HOSTS:
                problems.append(f"{at}: shell prompt{show(match.group(0))} (use you@demo:~)")
        if AGENT_TMP.search(line):
            problems.append(f"{at}: agent temp or worktree path (use ~/projects/demo)")
        for match in IPV4.finditer(line):
            try:
                ip = ipaddress.ip_address(match.group(1))
            except ValueError:
                continue
            if ip.is_private and not ip.is_loopback and not ip.is_link_local and not ip.is_unspecified \
                    and match.group(1) not in allow and not _documentation(ip):
                problems.append(f"{at}: private network address{show(match.group(1))} "
                                "(use 192.0.2.x, or add it to .privacy-allow if it is a made-up example)")
    return problems


def _documentation(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    return any(ip in ipaddress.ip_network(net) for net in
               ("192.0.2.0/24", "198.51.100.0/24", "203.0.113.0/24"))


def png_text(data: bytes) -> str:
    """The text chunks (tEXt, zTXt, iTXt) of a PNG: window titles live here."""
    out, pos = [], 8
    while pos + 8 <= len(data):
        length = int.from_bytes(data[pos:pos + 4], "big")
        kind = data[pos + 4:pos + 8]
        body = data[pos + 8:pos + 8 + length]
        if kind == b"tEXt":
            out.append(body.replace(b"\0", b" ").decode("latin-1"))
        elif kind == b"zTXt" and b"\0" in body:
            key, rest = body.split(b"\0", 1)
            try:
                out.append(key.decode("latin-1") + " " + zlib.decompress(rest[1:]).decode("latin-1"))
            except zlib.error:
                pass
        elif kind == b"iTXt":
            out.append(body.replace(b"\0", b" ").decode("utf-8", "replace"))
        if kind == b"IEND":
            break
        pos += 12 + length
    return "\n".join(out)


def tracked_files() -> list[str]:
    result = subprocess.run(["git", "ls-files"], cwd=ROOT, capture_output=True, text=True, check=True)
    return result.stdout.splitlines()


def scan_repo(allow: set[str]) -> list[str]:
    problems = []
    for name in tracked_files():
        path = ROOT / name
        if name in SKIP_FILES or not path.is_file() or path.suffix.lower() in SKIP_SUFFIXES:
            continue
        data = path.read_bytes()
        if path.suffix.lower() == ".png":
            meta = png_text(data)
            if meta:
                problems.append(f"{name}: PNG carries text metadata (strip it: exiftool -all= {name})")
                problems += scan_text(meta, name + " [metadata]", allow)
            continue
        if b"\0" in data[:4096]:
            continue
        problems += scan_text(data.decode("utf-8", "replace"), name, allow)
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--text", type=Path, help="scan this text file instead of the repository")
    parser.add_argument("--redact", action="store_true", help="do not repeat the matched values in the report")
    args = parser.parse_args(argv)
    allow = allowed_ips()
    if args.text:
        problems = scan_text(args.text.read_text(errors="replace"), "text", allow, args.redact)
    else:
        problems = scan_repo(allow)
    for problem in problems:
        print(problem)
    if problems:
        print(f"\n{len(problems)} privacy problem(s). This repository is public: "
              "replace real paths, prompts and addresses with placeholders.", file=sys.stderr)
        return 1
    print("privacy check: clean")
    return 0


if __name__ == "__main__":
    sys.exit(main())
