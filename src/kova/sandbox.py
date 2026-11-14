"""Kernel confinement for ``run_command`` (Linux Landlock).

A safe command runs without an approval prompt only when the operating system
can promise that it cannot write outside the chat's working folder. Landlock
does that without root, cgroups or containers: the process keeps read and
execute access to the machine but may only create, change, rename or remove
files under the allowed roots. A missing or disabled Landlock therefore means
"ask the user", never "run it anyway".

The policy lives in ``permissions.needs_approval``; this module only answers
"can we confine?" and builds the wrapper that applies the boundary in the
child before ``exec``.
"""

from __future__ import annotations

import ctypes
import errno
import os
import sys
import tempfile
from pathlib import Path

# ``landlock_create_ruleset``, ``landlock_add_rule`` and
# ``landlock_restrict_self`` share these numbers on every mainstream Linux
# architecture (the generic syscall table).
_SYS_CREATE_RULESET = 444
_SYS_ADD_RULE = 445
_SYS_RESTRICT_SELF = 446
_CREATE_RULESET_VERSION = 1
_RULE_PATH_BENEATH = 1
_PR_SET_NO_NEW_PRIVS = 38

# Filesystem rights that can change the user's data. Reads and execution stay
# untouched, so ordinary tools work while nothing outside the allowed roots is
# created, altered or deleted.
_WRITE_FILE = 1 << 1
_REMOVE_DIR = 1 << 4
_REMOVE_FILE = 1 << 5
_MAKE_CHAR = 1 << 6
_MAKE_DIR = 1 << 7
_MAKE_REG = 1 << 8
_MAKE_SOCK = 1 << 9
_MAKE_FIFO = 1 << 10
_MAKE_BLOCK = 1 << 11
_MAKE_SYM = 1 << 12
_REFER = 1 << 13     # ABI 2: rename/link across directories
_TRUNCATE = 1 << 14  # ABI 3: truncate(2)
_DEVICE_RIGHTS = _WRITE_FILE | _TRUNCATE

EXIT_UNAVAILABLE = 125

_abi: int | None = None


class _RulesetAttr(ctypes.Structure):
    _fields_ = [("handled_access_fs", ctypes.c_uint64)]


class _PathBeneathAttr(ctypes.Structure):
    _pack_ = 1
    _fields_ = [("allowed_access", ctypes.c_uint64), ("parent_fd", ctypes.c_int32)]


def _libc() -> ctypes.CDLL:
    library = ctypes.CDLL(None, use_errno=True)
    library.syscall.restype = ctypes.c_long
    return library


def abi_version() -> int:
    """The kernel's Landlock ABI version, or 0 when it is not usable."""
    global _abi
    if _abi is not None:
        return _abi
    _abi = 0
    if sys.platform == "linux" and os.environ.get("KOVA_SANDBOX", "").strip().lower() not in {
            "off", "0", "false", "no", "none"}:
        try:
            version = _libc().syscall(_SYS_CREATE_RULESET, None, ctypes.c_size_t(0),
                                      ctypes.c_uint32(_CREATE_RULESET_VERSION))
            if version > 0:
                _abi = int(version)
        except (AttributeError, OSError):
            pass
    return _abi


def available() -> bool:
    """True when commands can be confined instead of merely approved."""
    return abi_version() > 0


def _rights(abi: int) -> int:
    rights = (_WRITE_FILE | _REMOVE_DIR | _REMOVE_FILE | _MAKE_CHAR | _MAKE_DIR | _MAKE_REG
              | _MAKE_SOCK | _MAKE_FIFO | _MAKE_BLOCK | _MAKE_SYM)
    if abi >= 2:
        rights |= _REFER
    if abi >= 3:
        rights |= _TRUNCATE
    return rights


def _check(result: int, action: str) -> int:
    if result < 0:
        code = ctypes.get_errno()
        raise OSError(code, f"{action}: {os.strerror(code)}")
    return result


def _allow(libc, ruleset: int, path: Path, allowed: int) -> None:
    """Permit ``allowed`` on a path (a directory, or a single device file)."""
    try:
        descriptor = os.open(path, os.O_PATH | os.O_CLOEXEC)
    except OSError:
        return  # a path this machine does not have is simply not allowed
    try:
        rule = _PathBeneathAttr(allowed, descriptor)
        _check(libc.syscall(_SYS_ADD_RULE, ruleset, _RULE_PATH_BENEATH,
                            ctypes.byref(rule), 0), f"landlock_add_rule {path}")
    finally:
        os.close(descriptor)


def writable_roots(root: Path) -> list[Path]:
    """Directories a confined command may write: its folder and temp space."""
    candidates = [Path(root), Path(tempfile.gettempdir()), Path("/tmp"), Path("/var/tmp")]
    roots: list[Path] = []
    for candidate in candidates:
        try:
            resolved = candidate.expanduser().resolve()
        except OSError:
            continue
        if resolved.is_dir() and resolved not in roots:
            roots.append(resolved)
    return roots


def apply(root: Path) -> None:
    """Confine the current process; raises OSError when it cannot be done."""
    abi = abi_version()
    if abi <= 0:
        raise OSError(errno.ENOTSUP, "Landlock is not available on this system")
    libc = _libc()
    rights = _rights(abi)
    ruleset = _RulesetAttr(rights)
    descriptor = _check(libc.syscall(_SYS_CREATE_RULESET, ctypes.byref(ruleset),
                                     ctypes.sizeof(ruleset), 0),
                        "landlock_create_ruleset")
    try:
        for directory in writable_roots(root):
            _allow(libc, descriptor, directory, rights)
        _allow(libc, descriptor, Path("/dev/null"), _DEVICE_RIGHTS & rights)
        _check(libc.prctl(_PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0), "prctl(NO_NEW_PRIVS)")
        _check(libc.syscall(_SYS_RESTRICT_SELF, descriptor, 0), "landlock_restrict_self")
    finally:
        os.close(descriptor)


def wrap(argv: list[str], root: Path, status_fd: int | None = None) -> list[str] | None:
    """The argv that applies the sandbox and then runs ``argv``, or None."""
    if not available():
        return None
    status = ["--status-fd", str(status_fd)] if status_fd is not None else []
    return [sys.executable, "-m", "kova.sandbox", *status, str(Path(root).resolve()), "--", *argv]


def main(args: list[str] | None = None) -> int:
    """Apply confinement, then replace this process with the command."""
    arguments = list(sys.argv[1:] if args is None else args)
    status_fd = None
    if arguments[:1] == ["--status-fd"]:
        status_fd = int(arguments[1])
        arguments = arguments[2:]
    if len(arguments) < 3 or arguments[1] != "--":
        sys.stderr.write("usage: python -m kova.sandbox ROOT -- COMMAND [ARG...]\n")
        return 2
    root, command = Path(arguments[0]), arguments[2:]
    try:
        apply(root)
    except OSError as exc:
        # Never fall through to an unconfined exec: a broken sandbox is an error.
        sys.stderr.write(f"kova: could not establish the command sandbox: {exc}\n")
        return EXIT_UNAVAILABLE
    if status_fd is not None:
        os.write(status_fd, b"ready")
        os.close(status_fd)  # The command cannot forge setup status.
    try:
        os.execvpe(command[0], command, os.environ)
    except OSError as exc:
        sys.stderr.write(f"kova: could not run {command[0]}: {exc}\n")
        return 127


if __name__ == "__main__":
    raise SystemExit(main())
