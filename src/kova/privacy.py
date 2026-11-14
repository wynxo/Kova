"""Opt-in protection for text Kova sends to a non-local model.

The detector lives in :mod:`kova.secrets`. This module applies it at the single
outbound boundary in :class:`kova.models.ModelClient`, on a deep copy of the
request messages, so stored chats, tool arguments and authentication headers are
never rewritten. Only message text is inspected: system prompts, user and
assistant text, and tool results. Tool-call arguments are deliberately left
alone because they are execution instructions, not prose.

Loopback bypass is decided from a parsed URL hostname, never from a name that
merely looks local. Literal loopback addresses and the exact name ``localhost``
are local; every other host, including a LAN name or a public suffix, is remote.
No DNS lookup is performed, so a hostname cannot become trusted by resolving it
to a loopback address.

Modes are ``off`` (default), ``redact`` and ``block``. There is no warn mode:
sending a likely secret without a user decision would defeat the guard. In
``redact`` mode a bounded scan replaces findings with stable placeholders and
replaces an unscanned tail or overflow with a marker, never returning it
untouched. Placeholders are not re-detected, so redacting an already redacted
request produces the same text. In ``block`` mode any finding or incomplete
scan raises before a request is made.
"""

from __future__ import annotations

import copy
import ipaddress
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

from kova.secrets import (
    MAX_FINDINGS as _DEFAULT_MAX_FINDINGS,
    MAX_SCAN_CHARS as _DEFAULT_MAX_SCAN_CHARS,
    detect,
    redact,
)

#: Highest accepted scan limits, mirrored from the detector.
MAX_SCAN_CHARS = _DEFAULT_MAX_SCAN_CHARS
MAX_FINDINGS = _DEFAULT_MAX_FINDINGS

MODES = ("off", "redact", "block")
DEFAULT_MODE = "off"


def normalize_mode(value: object) -> str:
    """Return a valid mode, treating anything unrecognised as ``block``.

    ``None`` means the setting is absent and keeps the compatible default
    ``off``. A hand-edited typo fails closed instead of silently disabling
    protection. ``kova config set`` rejects unknown values before saving.
    """
    if value is None:
        return DEFAULT_MODE
    text = str(value).strip().lower()
    if text in MODES:
        return text
    return "block"


def is_loopback_url(url: str) -> bool:
    """Whether a base URL points at loopback without trusting a hostname.

    Parsing follows the URL standard through :func:`urllib.parse.urlsplit`; the
    decision uses only ``parts.hostname``. Userinfo cannot spoof the host
    (``http://127.0.0.1@example.test`` is remote), a LAN name is never resolved,
    and malformed input is treated as remote. The exact name ``localhost`` and
    literal loopback IPs (including an IPv4-mapped IPv6 loopback) are local.
    """
    try:
        parts = urlsplit(url)
        host = parts.hostname
    except (TypeError, ValueError):
        return False
    if not host:
        return False
    host = host.rstrip(".")
    if host.lower() == "localhost":
        return True
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    if address.is_loopback:
        return True
    mapped = getattr(address, "ipv4_mapped", None)
    return bool(mapped is not None and mapped.is_loopback)


@dataclass
class Protection:
    """A sanitized request copy plus a value-free summary of what was found."""

    messages: list[dict]
    mode: str
    count: int = 0
    kinds: tuple[str, ...] = ()
    incomplete: bool = False
    blocked: bool = False
    _reasons: tuple[str, ...] = field(default_factory=tuple, repr=False)

    def status(self) -> dict[str, Any]:
        """Safe fields for a UI event; never a matched value."""
        return {
            "mode": self.mode,
            "count": self.count,
            "kinds": list(self.kinds),
            "incomplete": self.incomplete,
        }

    def block_message(self) -> str:
        """A clear, value-free error for ``block`` mode."""
        reasons = list(self._reasons)
        if not reasons:
            reasons.append("the request contains protected text")
        return ("Blocked by privacy.secrets=block: " + "; ".join(reasons) +
                ". Nothing was sent to the model.")

    def status_text(self) -> str:
        """One readable line for the terminal and web UI."""
        return status_text(self.status())


def status_text(status: dict[str, Any]) -> str:
    """A value-free privacy status line from a UI event payload."""
    try:
        count = int(status.get("count") or 0)
    except (TypeError, ValueError):
        count = 0
    kinds = [str(kind) for kind in status.get("kinds") or []]
    parts = []
    if count:
        noun = "secret" if count == 1 else "secrets"
        suffix = f" ({', '.join(kinds)})" if kinds else ""
        parts.append(f"redacted {count} likely {noun}{suffix}")
    if status.get("incomplete"):
        parts.append("removed the unscanned rest")
    if not parts:
        return "Privacy guard is on; nothing matched."
    return "Privacy: " + "; ".join(parts) + " before sending."


def _scan_text(text: str, mode: str, limit: int, cap: int) -> tuple[str, list, bool, bool]:
    """Return ``(text, findings, truncated, overflowed)`` for one string."""
    if mode == "redact":
        result = redact(text, max_scan_chars=limit, max_findings=cap)
        return result.text, list(result.findings), result.truncated, result.overflowed
    scan = detect(text, max_scan_chars=limit, max_findings=cap)
    return text, list(scan.findings), scan.truncated, scan.overflowed


def _protect_value(value: Any, mode: str, limit: int, cap: int,
                   findings: list, incomplete: list[bool]) -> Any:
    if isinstance(value, str):
        text, found, truncated, overflowed = _scan_text(value, mode, limit, cap)
        findings.extend(found)
        incomplete.append(truncated or overflowed)
        return text
    if isinstance(value, list):
        output = []
        for part in value:
            if isinstance(part, dict) and isinstance(part.get("text"), str):
                updated = dict(part)
                text, found, truncated, overflowed = _scan_text(part["text"], mode, limit, cap)
                updated["text"] = text
                findings.extend(found)
                incomplete.append(truncated or overflowed)
                output.append(updated)
            elif isinstance(part, str):
                text, found, truncated, overflowed = _scan_text(part, mode, limit, cap)
                findings.extend(found)
                incomplete.append(truncated or overflowed)
                output.append(text)
            else:
                output.append(part)
        return output
    return value


def protect_messages(messages: list[dict], mode: object = DEFAULT_MODE, *,
                     max_scan_chars: int | None = None,
                     max_findings: int | None = None) -> Protection:
    """Scan a deep copy of outgoing message text.

    The input list and every object in it are left untouched. ``content`` may be
    a string or a list of content parts; only text parts are changed. A tool
    call's ``function.arguments`` is not touched. Limits are per text field, so a
    very large history cannot make one scan unbounded.
    """
    selected = normalize_mode(mode)
    if selected == "off":
        return Protection(messages, selected)
    limit = MAX_SCAN_CHARS if max_scan_chars is None else max(0, int(max_scan_chars))
    cap = MAX_FINDINGS if max_findings is None else max(0, int(max_findings))
    copied = copy.deepcopy(messages)
    findings: list = []
    incomplete: list[bool] = []
    for message in copied:
        if isinstance(message, dict) and "content" in message:
            message["content"] = _protect_value(
                message["content"], selected, limit, cap, findings, incomplete)
    kinds = tuple(dict.fromkeys(finding.kind for finding in findings))
    truncated = any(incomplete)
    reasons = []
    if findings:
        noun = "secret" if len(findings) == 1 else "secrets"
        kinds_text = f" ({', '.join(kinds)})" if kinds else ""
        reasons.append(f"found {len(findings)} likely {noun}{kinds_text}")
    if truncated:
        reasons.append("part of the request could not be fully inspected")
    blocked = selected == "block" and bool(findings or truncated)
    return Protection(
        messages=messages if blocked else copied,
        mode=selected,
        count=len(findings),
        kinds=kinds,
        incomplete=truncated,
        blocked=blocked,
        _reasons=tuple(reasons),
    )
