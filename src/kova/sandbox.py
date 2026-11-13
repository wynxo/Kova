"""OS sandbox for shell commands: landlock (or bubblewrap when landlock is missing).

Why
---
The destructive-command regex in `kova.permissions` is a heuristic: it cannot tell a
harmful command from an unusual one, so the safe way to use it is to ask about
everything it does not recognise. A kernel-enforced sandbox inverts that: a command
that is on the inspection allowlist runs confined by the OS and needs no approval
prompt, while everything else keeps the existing approval behaviour.

Fail closed
-----------
`wrap` returns None whenever no backend is available, the sandbox is disabled, or the
command is not on the allowlist, and the caller then keeps the normal approval path.
A wrapped command runs the landlock bootstrap, which refuses to exec the command at
all when it cannot establish the domain, so a sandboxed call either runs confined or
fails -- it never runs unsandboxed.

What the sandbox allows (landlock backend)
------------------------------------------
- read and execute: system paths, the temp folder, the working folder, and the paths
  of the running Python and of the programs on ``PATH``;
- read, write and create: the working folder, the temp folder, and ``/dev``;
- no network at all (landlock ABI 4 and later), and no signals or abstract UNIX
  socket connections to processes outside the sandbox (ABI 6 and later);
- the user's home folder is not readable, and nothing outside the paths above can be
  written.

``KOVA_SANDBOX=0`` (also ``off``, ``false``, ``no``) disables the sandbox, which puts
every command back behind approval.
"""

from __future__ import annotations

import ctypes
import functools
import os
from pathlib import Path
import re
import shutil
import struct
import subprocess
import sys
import tempfile

from kova.permissions import command_risk, shell_parts

OFF_VALUES = frozenset({"0", "off", "false", "no"})

# Commands that only read: the working folder, the machine's system paths, and tests.
# Interpreters and test runners are included because the sandbox, not the prompt, is
# what confines them. Anything with a write or network purpose (echo included) is left
# out and keeps asking.
SAFE_COMMANDS = frozenset({
    "ls", "cat", "head", "tail", "wc", "grep", "rg", "find", "fd", "pwd", "stat", "file",
    "du", "df", "sort", "uniq", "cut", "tr", "awk", "sed", "jq", "diff", "cmp", "tree",
    "which", "basename", "dirname", "realpath", "readlink", "nl", "od", "strings", "xxd",
    "sha256sum", "sha1sum", "md5sum", "b2sum", "ts", "git", "python", "python3", "pytest",
    "node", "deno", "bun", "ruby",
})
GIT_READ_COMMANDS = frozenset({
    "status", "log", "diff", "show", "branch", "remote", "describe", "blame", "rev-parse",
    "ls-files", "show-ref", "tag", "shortlog", "cat-file", "ls-tree", "grep", "whatchanged",
    "rev-list", "symbolic-ref", "count-objects", "for-each-ref", "name-rev", "annotate",
})

# Shell syntax we will not classify: expansion, redirection, escaping and groups.
_UNSAFE_SHELL = re.compile(r"[<>$`\\{}]")

_SYS_LANDLOCK_CREATE_RULESET = 444
_SYS_LANDLOCK_ADD_RULE = 445
_SYS_LANDLOCK_RESTRICT_SELF = 446
_LANDLOCK_CREATE_RULESET_VERSION = 1
_LANDLOCK_RULE_PATH_BENEATH = 1
_PR_SET_NO_NEW_PRIVS = 38

_EXECUTE = 1 << 0
_READ_FILE = 1 << 2
_READ_DIR = 1 << 3
_READ = _EXECUTE | _READ_FILE | _READ_DIR
_SCOPED = 0b11  # abstract UNIX sockets and signals stay inside the domain (ABI 6+)

# (landlock ABI that introduced the right, highest bit index + 1)
_FS_ABI = ((1, 13), (2, 14), (3, 15), (5, 16), (9, 17))
_NET_ABI = ((4, 2), (10, 4))

_SYSTEM_READABLE = ("/usr", "/bin", "/sbin", "/lib", "/lib32", "/lib64", "/libx32",
                    "/etc", "/opt", "/nix", "/snap", "/run", "/sys", "/proc", "/dev")


class SandboxUnavailable(RuntimeError):
    """A backend was expected but could not be established."""


def _mask(table: tuple[tuple[int, int], ...], abi: int) -> int:
    bits = 0
    for version, width in table:
        if abi >= version:
            bits = width
    return (1 << bits) - 1


def _libc() -> ctypes.CDLL:
    libc = ctypes.CDLL(None, use_errno=True)
    libc.syscall.restype = ctypes.c_long
    libc.prctl.restype = ctypes.c_int
    return libc


@functools.lru_cache(maxsize=1)
def landlock_abi() -> int:
    """The running kernel's landlock ABI level, or 0 when landlock is unavailable."""
    if not sys.platform.startswith("linux"):
        return 0
    try:
        result = _libc().syscall(ctypes.c_long(_SYS_LANDLOCK_CREATE_RULESET), ctypes.c_void_p(None),
                                 ctypes.c_size_t(0), ctypes.c_uint(_LANDLOCK_CREATE_RULESET_VERSION))
    except (OSError, AttributeError, ValueError):
        return 0
    return int(result) if result > 0 else 0


def _bwrap_works() -> bool:
    bwrap = shutil.which("bwrap")
    if not bwrap:
        return False
    try:
        probe = subprocess.run(
            [bwrap, "--ro-bind", "/", "/", "--dev", "/dev", "--proc", "/proc", "--", "/bin/true"],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            timeout=10, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return probe.returncode == 0


@functools.lru_cache(maxsize=1)
def backend() -> str | None:
    """The sandbox in use: ``"landlock"``, ``"bwrap"``, or None when unavailable."""
    if os.environ.get("KOVA_SANDBOX", "").strip().lower() in OFF_VALUES:
        return None
    if not sys.platform.startswith("linux"):
        return None
    if landlock_abi() >= 1:
        return "landlock"
    return "bwrap" if _bwrap_works() else None


def _existing(paths) -> list[Path]:
    found: list[Path] = []
    for path in paths:
        try:
            resolved = Path(path).resolve()
        except OSError:
            continue
        if resolved.is_dir() and resolved not in found:
            found.append(resolved)
    return found


def writable_paths(cwd: Path) -> list[Path]:
    """Folders a sandboxed command may change: the working folder, temp, and /dev."""
    return _existing([cwd, tempfile.gettempdir(), "/tmp", "/dev"])


def readable_paths(cwd: Path) -> list[Path]:
    """Folders a sandboxed command may read and execute; the user's home is absent."""
    roots = [cwd, *_SYSTEM_READABLE, *writable_paths(cwd)]
    roots += [sys.prefix, sys.base_prefix, Path(sys.executable).resolve().parent]
    roots += [entry for entry in sys.path if entry]
    roots += [entry for entry in os.environ.get("PATH", "").split(os.pathsep) if entry]
    return _existing(roots)


def restrict(cwd: Path, *, writable: list[Path] | None = None,
             readable: list[Path] | None = None) -> None:
    """Enforce the landlock domain on this process.

    There is no way back, so the only correct caller is the sandbox bootstrap, which
    execs the real command right after this. Raises `SandboxUnavailable` when the
    domain cannot be established, which the caller must treat as "do not run".
    """
    abi = landlock_abi()
    if abi < 1:
        raise SandboxUnavailable("this kernel has no landlock support")
    libc = _libc()
    handled_fs = _mask(_FS_ABI, abi)
    words = 1 if abi < 4 else 3 if abi >= 6 else 2
    attr = [handled_fs]
    if words > 1:
        attr.append(_mask(_NET_ABI, abi))   # no net rules: every port is denied
    if words > 2:
        attr.append(_SCOPED)
    payload = struct.pack(f"<{words}Q", *attr)
    buffer = ctypes.create_string_buffer(payload, len(payload))
    ruleset = libc.syscall(ctypes.c_long(_SYS_LANDLOCK_CREATE_RULESET), ctypes.byref(buffer),
                           ctypes.c_size_t(len(payload)), ctypes.c_uint(0))
    if ruleset < 0:
        raise SandboxUnavailable(f"landlock_create_ruleset: {os.strerror(ctypes.get_errno())}")
    try:
        for path in readable if readable is not None else readable_paths(cwd):
            _add_rule(libc, ruleset, path, _READ)
        for path in writable if writable is not None else writable_paths(cwd):
            _add_rule(libc, ruleset, path, handled_fs)
        if libc.prctl(_PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0) != 0:
            raise SandboxUnavailable(f"prctl(PR_SET_NO_NEW_PRIVS): {os.strerror(ctypes.get_errno())}")
        if libc.syscall(ctypes.c_long(_SYS_LANDLOCK_RESTRICT_SELF), ctypes.c_int(ruleset),
                        ctypes.c_uint(0)) != 0:
            raise SandboxUnavailable(f"landlock_restrict_self: {os.strerror(ctypes.get_errno())}")
    finally:
        os.close(ruleset)


def _add_rule(libc: ctypes.CDLL, ruleset: int, path: Path, rights: int) -> None:
    try:
        parent = os.open(path, os.O_PATH | os.O_CLOEXEC)
    except OSError:
        return  # a path that does not exist cannot be granted
    try:
        payload = struct.pack("<qi", rights, parent)
        buffer = ctypes.create_string_buffer(payload, len(payload))
        if libc.syscall(ctypes.c_long(_SYS_LANDLOCK_ADD_RULE), ctypes.c_int(ruleset),
                        ctypes.c_uint(_LANDLOCK_RULE_PATH_BENEATH), ctypes.byref(buffer),
                        ctypes.c_uint(0)) != 0:
            raise SandboxUnavailable(
                f"landlock_add_rule {path}: {os.strerror(ctypes.get_errno())}")
    finally:
        os.close(parent)


def is_safe(command: str) -> bool:
    """Whether ``command`` is one plain inspection command we may run sandboxed."""
    text = str(command or "").strip()
    if not text or command_risk(text) != "normal" or _UNSAFE_SHELL.search(text):
        return False
    parts = shell_parts(text)
    if len(parts) != 1:            # separators, pipes, substitution: not one command
        return False
    tokens = parts[0].split()
    if not tokens or tokens[0] not in SAFE_COMMANDS:
        return False
    if tokens[0] == "git":
        return len(tokens) < 2 or tokens[1] in GIT_READ_COMMANDS
    return True


def wrap(command: str, cwd: Path) -> list[str] | None:
    """argv that runs ``command`` inside the sandbox, or None to run it as usual."""
    name = backend()
    if name is None or not is_safe(command):
        return None
    from kova.tools.commands import shell_command
    argv = shell_command(str(command))
    if name == "landlock":
        return [sys.executable, "-m", "kova.sandbox", "--cwd", str(cwd), "--", *argv]
    return _bwrap_argv(argv, cwd)


def auto_runs(name: str, args: dict, cwd: Path) -> bool:
    """Whether this tool call may skip approval because the sandbox replaces it."""
    return name == "run_command" and wrap(str(args.get("command", "")), cwd) is not None


def _bwrap_argv(argv: list[str], cwd: Path) -> list[str]:
    command = [shutil.which("bwrap") or "bwrap", "--die-with-parent", "--new-session",
               "--unshare-pid", "--unshare-net", "--ro-bind", "/", "/", "--dev", "/dev",
               "--proc", "/proc"]
    for path in writable_paths(cwd):
        if path == Path("/dev"):
            continue  # --dev already provides a writable /dev
        command += ["--bind", str(path), str(path)]
    return [*command, "--chdir", str(cwd), "--setenv", "KOVA_SANDBOXED", "bwrap", "--", *argv]


def status() -> str:
    """One line, for the system prompt, describing what shell commands may do."""
    name = backend()
    if name is None:
        return "none (every command keeps the configured approval behaviour)"
    if name == "landlock":
        return ("landlock (inspection commands run without asking; reads limited to system "
                "paths, the working folder and temp; writes only there and in /dev; no network)")
    return ("bubblewrap (inspection commands run without asking; the file system is read-only "
            "except the working folder and temp; no network)")


def _parse(argv: list[str]) -> tuple[str | None, list[str]]:
    """Split ``--cwd DIR -- COMMAND ...`` into its two parts."""
    try:
        index = argv.index("--")
    except ValueError:
        return None, []
    options = argv[:index]
    cwd = next((options[position + 1] for position, option in enumerate(options)
                if option == "--cwd" and position + 1 < len(options)), None)
    return cwd, argv[index + 1:]


def main(argv: list[str] | None = None) -> int:
    """Bootstrap entry point: ``python -m kova.sandbox --cwd DIR -- COMMAND ...``."""
    cwd, command = _parse(list(sys.argv[1:] if argv is None else argv))
    if not command or not cwd:
        print("usage: python -m kova.sandbox --cwd DIR -- COMMAND [ARG ...]", file=sys.stderr)
        return 2
    try:
        restrict(Path(cwd))
    except SandboxUnavailable as exc:
        print(f"Kova sandbox unavailable: {exc}. The command was not run.", file=sys.stderr)
        return 126
    os.environ["KOVA_SANDBOXED"] = "landlock"
    os.execvp(command[0], command)
    return 127  # only reached when exec failed


if __name__ == "__main__":
    sys.exit(main())
