"""Server-side permission policy, adapted from Kaine's agent_tools.py.

The classifier is a heuristic, not a shell sandbox. Manual is the default.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, asdict
from fnmatch import fnmatchcase
import copy
import hashlib
import json
import os
import shlex
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
    r"\bch(own|grp)\s+(-[a-z]+\s+)*[^\s]+\s+/",  # any absolute target, not just /
    r"\b(curl|wget)\b[^|]*\|\s*(sudo\s+)?(ba|z|k|da)?sh\b",
    r"\b(apt|apt-get|dnf|yum|pacman|zypper|snap|flatpak|pip3?|npm|cargo)\b"
    r"[^|;&]*\b(remove|purge|uninstall|autoremove|-R|-Rns)\b",
    r"\bgit\s+(push[^|;&]*--force|reset\s+--hard|clean\s+-[a-z]*f)",
    r"\bcrontab\s+-r\b",
    r"\bfind\b[^|;&]*-(delete|exec\s+rm)\b",
    r"\bkill(all)?\s+(-9\s+)?-1\b",
    r"\bdocker\s+(system\s+prune|volume\s+rm|rm\s+-f)\b",
    r"\btruncate\b[^|;&]*-s\s*0\b",
    r">>?\s*(/etc/|/boot/|/usr/|/bin/|/sbin/|/lib|/sys/|/proc/)",  # redirect into system files
    r">>?\s*(~|\$HOME|\$\{HOME\})?/?(\.ssh/|\.bashrc|\.zshrc|\.profile|\.bash_profile)",
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
    if name in {"job_add", "job_remove"}:
        # Deferred execution needs an explicit grant, even in auto mode.
        return True
    if name in {"skill_add", "skill_patch"}:
        return permission != "auto" and not session_allowed
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
class Rule:
    action: str
    tool: str = "*"
    pattern: str = "*"
    scope: str = "always"
    chat_id: str | None = None
    message: str | None = None
    exact: str | None = None

    def __post_init__(self):
        if not all(isinstance(v, str) for v in (self.action, self.tool, self.pattern, self.scope)):
            raise ValueError("Rule fields must be strings")
        if self.message is not None and not isinstance(self.message, str):
            raise ValueError("Rule message must be text")
        if self.exact is not None and not isinstance(self.exact, str):
            raise ValueError("Exact binding must be text")
        if self.scope == "session" and not self.chat_id:
            raise ValueError("Session rules require chat_id")
        if self.action not in {"deny", "ask", "allow"} or self.scope not in {"session", "always"}:
            raise ValueError("Invalid rule action or scope")


def action_key(name: str, args: dict, cwd: Path | None = None) -> str:
    """Bind all argv, edit contents and working directory, not a display summary."""
    executable = None
    if name == "run_command":
        from kova.tools.commands import shell_command
        from kova.tools.core import fingerprint
        argv = shell_command(str(args.get("command", "")))
        executable = [argv, fingerprint(Path(argv[0]))]
    raw = json.dumps([name, args, str(cwd.resolve()) if cwd else None, executable], sort_keys=True)
    return hashlib.sha256(raw.encode()).hexdigest()


def shell_parts(command: str) -> list[str]:
    """Conservative shell matcher, not a shell parser or sandbox.

    Split even quoted separators (over-asking is safe). Strip substitution
    delimiters so every nested command must independently match an allow.
    Shell syntax we cannot confidently classify never receives a pattern allow.
    """
    parts = []
    for part in re.split(r"[;&|\n`()]+|\$\(", command):
        if not part.strip():
            continue
        try:
            parts.append(" ".join(shlex.split(part)))
        except ValueError:
            parts.append(part.strip())  # unbalanced shell syntax cannot gain trust
    return parts


def evaluate_rules(rules: list[Rule], name: str, args: dict, cwd: Path | None = None) -> str | None:
    values = shell_parts(str(args.get("command", ""))) if name == "run_command" else [str(args.get("path", ""))]
    matched = []
    for rule in rules:
        if not fnmatchcase(name, rule.tool):
            continue
        if rule.exact:
            hit = rule.exact == action_key(name, args, cwd)
        elif rule.action == "allow":
            # Expansion, redirection, wrappers and shell syntax require exact approval.
            unsafe = name == "run_command" and re.search(r"[$<>\\{}]|(^|\s)(eval|exec|source|bash|sh|zsh|env|command)\b", str(args.get("command", "")))
            hit = bool(values) and not unsafe and all(fnmatchcase(v, rule.pattern) for v in values)
        else:
            hit = any(fnmatchcase(v, rule.pattern) for v in values)
        if hit:
            matched.append(rule.action)
    return next((a for a in ("deny", "ask", "allow") if a in matched), None)


@dataclass
class PendingApproval:
    chat_id: str
    risk: str
    future: asyncio.Future
    tool: str
    args: dict
    cwd: Path | None
    key: str


class Approvals:
    """Exact action grants; session grants live only until server restart."""
    def __init__(self, timeout: float = APPROVAL_TIMEOUT, path: Path | None = None):
        self.timeout, self.path = timeout, path
        self.pending: dict[str, PendingApproval] = {}
        self.sessions: set[str] = set()  # compatibility only; never used as authorization
        self.rules: list[Rule] = []
        if path and path.exists():
            self.rules = [Rule(**r) for r in json.loads(path.read_text())]

    def save(self):
        if self.path:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.path.with_suffix(".tmp")
            temporary.write_text(json.dumps([asdict(r) for r in self.rules if r.scope == "always"], indent=2))
            os.chmod(temporary, 0o600)
            temporary.replace(self.path)

    def policy(self, chat_id, name, args, cwd=None):
        rules = [r for r in self.rules if r.scope == "always" or r.chat_id == chat_id]
        action = evaluate_rules(rules, name, args, cwd)
        message = next((r.message for r in rules if r.action == "deny" and evaluate_rules([r], name, args, cwd) == "deny"), None)
        return action, message

    def create(self, chat_id: str, name: str, args: dict, cwd: Path | None = None) -> dict:
        identifier = uuid4().hex
        args = copy.deepcopy(args)
        risk = command_risk(args["command"]) if name == "run_command" else "normal"
        future = asyncio.get_running_loop().create_future()
        self.pending[identifier] = PendingApproval(chat_id, risk, future, name, args, cwd, action_key(name, args, cwd))
        summary = args["command"] if name == "run_command" else f"{name}: {args.get('path', '')}"
        if name == "job_add":
            summary = f"Schedule {args.get('kind')} at {args.get('when')}, repeat every {args.get('every_seconds', 0)}s: {args.get('text', '')}"
        elif name in {"skill_add", "skill_patch"}:
            summary = f"{name}: {args.get('slug', '')}"
        elif name == "job_remove":
            summary = f"Remove scheduled job #{args.get('id')}"
        changes = None
        if name == "edit_file":
            old = str(args.get("old_string", ""))
            new = str(args.get("new_string", ""))
            changes = f"Replace {len(old.splitlines())} lines ({len(old)} chars) with {len(new.splitlines())} lines ({len(new)} chars)"
            summary += "\n" + changes
        return {"type": "approval", "id": identifier, "tool": name, "args": copy.deepcopy(args), "change_summary": changes,
                "risk": risk, "summary": summary, "binding": action_key(name, args, cwd)}

    async def wait(self, identifier: str) -> dict:
        pending = self.pending[identifier]
        try:
            decision = await asyncio.wait_for(pending.future, timeout=self.timeout)
            if decision["decision"] in {"allow_session", "allow_always"}:
                if pending.risk != "destructive":
                    self.sessions.add(pending.chat_id)
                self.rules.append(Rule("allow", pending.tool, scope="always" if decision["decision"] == "allow_always" else "session",
                                       chat_id=pending.chat_id, exact=pending.key,
                                       pattern=str(pending.args.get("command", pending.args.get("path", "")))))
                self.save()
            return decision
        except TimeoutError:
            return {"decision": "deny", "message": "Approval timed out after 10 minutes"}
        finally:
            self.pending.pop(identifier, None)
            if not pending.future.done():
                pending.future.cancel()

    def answer(self, chat_id: str, identifier: str, decision: str, message: str | None = None) -> None:
        if decision not in {"allow", "allow_session", "allow_always", "deny"}:
            raise ValueError("Invalid decision")
        pending = self.pending.get(identifier)
        if pending is None or pending.chat_id != chat_id or pending.future.done():
            raise KeyError("Approval not found or no longer pending")
        pending.future.set_result({"decision": decision, "message": message})
