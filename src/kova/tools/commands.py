"""Bounded, cancellable local shell execution (adapted from Kaine commands.py)."""
from __future__ import annotations

import asyncio
import os
from pathlib import Path
import shutil
import signal
import subprocess
import tempfile

from kova import sandbox

OUTPUT_LIMIT = 32 * 1024


# PowerShell -Command reports only 0/1. Keep a native program's real exit code,
# still fail (1) on a failed cmdlet, and start on a new line so a trailing
# comment in the command cannot swallow it.
_PWSH_EXIT = "\nif (-not $?) { if ($LASTEXITCODE) { exit $LASTEXITCODE } else { exit 1 } }"


def shell_command(command: str) -> list[str]:
    if os.name == "nt":
        shell = shutil.which("pwsh") or shutil.which("powershell")
        if shell:
            return [shell, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", command + _PWSH_EXIT]
        return [os.environ.get("COMSPEC", "cmd.exe"), "/d", "/s", "/c", command]
    return [shutil.which("bash") or "/bin/sh", "-c", command]


async def _kill_tree(process: asyncio.subprocess.Process) -> None:
    if os.name == "nt":
        # taskkill /T terminates descendants, not just the PowerShell wrapper.
        killer = await asyncio.create_subprocess_exec(
            "taskkill", "/PID", str(process.pid), "/T", "/F",
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        await killer.wait()
        if process.returncode is None:
            process.kill()
    else:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    await process.wait()


async def run_command(command: str, cwd: Path, timeout: float = 60,
                      sandboxed: bool = False) -> dict:
    if sandboxed:
        with tempfile.TemporaryFile() as status:
            return await _run_command(command, cwd, timeout, sandboxed, status)
    return await _run_command(command, cwd, timeout, sandboxed)


async def _run_command(command, cwd, timeout, sandboxed, status=None):
    if not command.strip() or "\0" in command:
        raise ValueError("Enter a non-empty command without NUL characters")
    if isinstance(timeout, bool) or not 0 < timeout <= 300:
        raise ValueError("timeout must be greater than 0 and at most 300 seconds")
    argv = shell_command(command)
    if sandboxed:
        wrapped = sandbox.wrap(argv, cwd, status_fd=status.fileno())
        if wrapped is None:
            # Fail closed: an unavailable sandbox is a refusal, never an
            # unconfined run of a command the user was not asked about.
            return {"ok": False, "output": "", "exit_code": None, "truncated": False,
                    "sandboxed": False, "cwd": str(cwd),
                    "error": "Command sandbox is unavailable; the command was not run."}
        argv = wrapped
    options = ({"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
               if os.name == "nt" else {"start_new_session": True})
    if sandboxed:
        options["pass_fds"] = (status.fileno(),)
    spawn = asyncio.create_task(asyncio.create_subprocess_exec(
        *argv, cwd=cwd, stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, **options,
    ))
    # Shield startup so cancellation cannot orphan a process created concurrently.
    try:
        process = await asyncio.shield(spawn)
    except asyncio.CancelledError:
        process = await spawn
        await _kill_tree(process)
        raise
    output = bytearray()
    truncated = False
    timed_out = False

    async def collect():
        nonlocal truncated
        while chunk := await process.stdout.read(8192):
            room = OUTPUT_LIMIT - len(output)
            output.extend(chunk[:room])
            truncated |= len(chunk) > room
        await process.wait()  # handles commands closing stdout before exiting

    collector = asyncio.create_task(collect())
    try:
        await asyncio.wait_for(asyncio.shield(collector), timeout)
    except TimeoutError:
        timed_out = True
    finally:
        # On Stop, timeout, or normal exit, no surviving background children.
        cleanup = asyncio.create_task(_kill_tree(process))
        try:
            await asyncio.shield(cleanup)
        finally:
            await cleanup
            collector.cancel()
            await asyncio.gather(collector, return_exceptions=True)
    decoded = output.decode("utf-8", "replace").encode("utf-8")
    truncated |= len(decoded) > OUTPUT_LIMIT
    result = {"ok": not timed_out and process.returncode == 0,
              "output": decoded[:OUTPUT_LIMIT].decode("utf-8", "ignore"),
              "exit_code": process.returncode, "truncated": truncated,
              "sandboxed": sandboxed, "cwd": str(cwd)}
    if timed_out:
        result["error"] = f"Command timed out after {timeout} seconds; partial changes may remain"
    elif sandboxed and os.pread(status.fileno(), 5, 0) != b"ready":
        result["ok"] = False
        result["error"] = "Command sandbox could not be established; the command did not run."
    elif process.returncode:
        result["error"] = f"Command exited with status {process.returncode}"
    return result
