"""Server-side permission policy, adapted from Kaine's agent_tools.py.

The classifier is a heuristic, not a shell sandbox. Manual is the default.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
import re
from uuid import uuid4

PERMISSIONS = ("manual", "auto", "full")
APPROVAL_TIMEOUT = 600

_DESTRUCTIVE_PATTERNS = (
    r"\brm\s+(-[a-z]*[rf][a-z]*\s+)+",         # rm -rf / rm -f, any flag order
    r"\brmdir\s+/",
    r"\bmkfs(\.[a-z0-9]+)?\b",
    r"\b(fdisk|sfdisk|parted|wipefs|shred|blkdiscard)\b",
    r"\bdd\b[^|;&]*\bof=/dev/",
    r">\s*/dev/(sd|nvme|vd|hd|mmcblk)",
    r"\b(shutdown|reboot|poweroff|halt)\b",
    r"\bsystemctl\s+(poweroff|reboot|halt|isolate)\b",
    r"\b(sudo|doas|pkexec|su)\s",
    r"\b(userdel|usermod|groupdel|chpasswd|visudo)\b",
    r"(^|[;&|]\s*)passwd\b",                 # reading /etc/passwd is not this
    r"\bchmod\s+(-[a-z]+\s+)*(777|-R\s+777)",
    r"\bcho(wn|rp)\s+(-[a-z]+\s+)*[^\s]+\s+/(\s|$)",
    r"\b(curl|wget)\b[^|]*\|\s*(sudo\s+)?(ba|z|k|da)?sh\b",
    r"\b(apt|apt-get|dnf|yum|pacman|zypper|snap|flatpak|pip3?|npm|cargo)\b"
    r"[^|;&]*\b(remove|purge|uninstall|autoremove|-R|-Rns)\b",
    r"\bgit\s+(push[^|;&]*--force|reset\s+--hard|clean\s+-[a-z]*f)",
    r"\bcrontab\s+-r\b",
    r"\bfind\b[^|;&]*-(delete|exec\s+rm)\b",
    r"\bkill(all)?\s+(-9\s+)?-1\b",
    r"\bdocker\s+(system\s+prune|volume\s+rm|rm\s+-f)\b",
    r"\btruncate\b[^|;&]*-s\s*0\b",
    r":\(\)\s*\{[^}]*\|[^}]*&[^}]*\}",       # the fork bomb
    r"\b(init\s+0|telinit\s+[06])\b",
)
_DESTRUCTIVE = tuple(re.compile(pattern, re.IGNORECASE) for pattern in _DESTRUCTIVE_PATTERNS)


# Cover ordinary deletion, git work discard, and native Windows commands too.
_EXTRA_DESTRUCTIVE = tuple(re.compile(p, re.I) for p in (
    r"\brm\s+", r"\brmdir\s+", r"\b(unlink|remove-item|clear-content)\b",
    r"(^|[;&|]\s*)(del|erase|rd)\s+", r"\b(format(-volume)?|diskpart|clear-disk|initialize-disk)\b",
    r"\b(start-process|powershell|pwsh)\b[^;\n]*(-verb\s+runas|runas)",
    r"\brunas\s+", r"\b(invoke-expression|iex)\b", r"\b(invoke-webrequest|iwr|irm)\b[^|]*\|\s*(powershell|pwsh)",
    r"\bgit\s+(checkout\b[^;\n]*\s--\s|restore\b|reset\s+--(hard|merge|keep)|push\b[^;\n]*\s-f\b)",
    r"\b(winget|choco|scoop)\b[^;\n]*\b(uninstall|remove)\b",
))


def command_risk(command: str) -> str:
    command = str(command or "")
    return "destructive" if any(p.search(command) for p in (*_DESTRUCTIVE, *_EXTRA_DESTRUCTIVE)) else "normal"


def needs_approval(name: str, args: dict, permission: str, cwd: Path, files: Path,
                   session_allowed: bool = False) -> bool:
    if permission == "full":
        return False
    if name == "run_command":
        if command_risk(args["command"]) == "destructive":
            return True
        return permission != "auto" and not session_allowed
    if name in {"write_file", "edit_file"}:
        from kova.tools.code_files import resolve_path
        target = resolve_path(args["path"], str(cwd))
        return permission != "auto" and not session_allowed and not target.is_relative_to(files.resolve())
    return False


@dataclass
class PendingApproval:
    chat_id: str
    risk: str
    future: asyncio.Future


class Approvals:
    """In-memory, chat-scoped approvals. Nothing survives server restart."""
    def __init__(self, timeout: float = APPROVAL_TIMEOUT):
        self.timeout = timeout
        self.pending: dict[str, PendingApproval] = {}
        self.sessions: set[str] = set()

    def create(self, chat_id: str, name: str, args: dict) -> dict:
        identifier = uuid4().hex
        risk = command_risk(args["command"]) if name == "run_command" else "normal"
        future = asyncio.get_running_loop().create_future()
        self.pending[identifier] = PendingApproval(chat_id, risk, future)
        summary = args["command"] if name == "run_command" else f"{name}: {args['path']}"
        return {"type": "approval", "id": identifier, "tool": name, "args": args,
                "risk": risk, "summary": " ".join(summary.split())[:500]}

    async def wait(self, identifier: str) -> dict:
        pending = self.pending[identifier]
        try:
            decision = await asyncio.wait_for(pending.future, timeout=self.timeout)
            if decision["decision"] == "allow_session" and pending.risk != "destructive":
                self.sessions.add(pending.chat_id)
            return decision
        except TimeoutError:
            return {"decision": "deny", "message": "Approval timed out after 10 minutes"}
        finally:
            self.pending.pop(identifier, None)
            if not pending.future.done():
                pending.future.cancel()

    def answer(self, chat_id: str, identifier: str, decision: str, message: str | None = None) -> None:
        pending = self.pending.get(identifier)
        if pending is None or pending.chat_id != chat_id or pending.future.done():
            raise KeyError("Approval not found or no longer pending")
        # For a destructive call allow_session approves only this one call.
        pending.future.set_result({"decision": decision, "message": message})
