"""Detect and redact likely credentials in text.

This is the detector/redactor foundation for the secret guard described in
issue #78. It is deliberately *not* wired into ``ModelClient`` or the server
yet: nothing here inspects, blocks or redacts network traffic. The module only
provides a deterministic, bounded scan that a future model-boundary hook can
call before a request leaves the machine.

The detectors are known shapes, not guesses:

* private key blocks and headers,
* access tokens from common providers (OpenAI, Anthropic, GitHub, Slack,
  Google, AWS, Stripe, SendGrid, npm, GitLab, Hugging Face),
* JWTs and ``Bearer`` tokens,
* credential-bearing URLs such as ``postgres://user:<password>@host``,
* upper-case ``.env``-style assignments whose name looks like a secret
  (``OPENAI_API_KEY=...``, ``export DATABASE_PASSWORD=...``).

There is intentionally no entropy heuristic. Unknown random-looking strings
are not flagged until a measured false-positive test shows it is worth the
noise; known-shape rules and explicit tests are the foundation here.

Scanning is bounded by :data:`MAX_SCAN_CHARS` and :data:`MAX_FINDINGS`.
:func:`redact` replaces anything it could not inspect with an
``[REDACTED:unscanned:N]`` or ``[REDACTED:overflow:N]`` marker rather than
returning a string that may still contain a secret.

Placeholders are deterministic for a given kind and secret, e.g.
``[REDACTED:openai_key:3f9a]``. The four hex characters are a short SHA-256
fingerprint used only to keep repeated occurrences stable; they are not a
claim that the placeholder is safe to publish. :func:`redact` never
un-redacts, and :class:`Finding` never carries the matched secret.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass

#: At most this many characters are inspected; a longer input is truncated and
#: reported as such by :func:`detect` and :func:`redact`.
MAX_SCAN_CHARS = 200_000

#: At most this many findings are accepted. The remainder of the text is
#: replaced in :func:`redact` when this cap is reached.
MAX_FINDINGS = 200

#: Pieces of a variable name that mark its value as a credential.
_SECRET_WORDS = frozenset({
    "SECRET", "TOKEN", "PASSWORD", "PASSWD", "PASS", "APIKEY", "CREDENTIAL",
    "CREDENTIALS",
})
#: The last underscore-separated part of a credential variable name.
_SECRET_LAST = frozenset({
    "SECRET", "TOKEN", "PASSWORD", "PASSWD", "PWD", "PASS",
})
#: Multi-part endings such as ``OPENAI_API_KEY`` or ``AWS_ACCESS_KEY_ID``.
_SECRET_SUFFIXES = (
    ("API", "KEY"),
    ("PRIVATE", "KEY"),
    ("SECRET", "KEY"),
    ("ACCESS", "KEY"),
    ("ACCESS", "KEY", "ID"),
    ("SIGNING", "KEY"),
    ("ENCRYPTION", "KEY"),
    ("CLIENT", "SECRET"),
    ("AUTH", "TOKEN"),
    ("SESSION", "TOKEN"),
    ("REFRESH", "TOKEN"),
)

#: Values that are plainly placeholders or variable references, not secrets.
_PLACEHOLDER_VALUES = frozenset({
    "changeme", "change-me", "change_me", "changeit", "placeholder", "example",
    "example-value", "your-api-key", "your_api_key", "your-key", "your_key",
    "your-token", "your_token", "redacted", "none", "null", "true", "false",
    "undefined", "secret", "password",
})
#: Leading shapes that mean "fill this in" rather than "this is the value".
_PLACEHOLDER_PREFIXES = ("$", "<", "{", "%(", "your-", "your_")

_PEM_BLOCK = re.compile(
    r"(?m)^[ \t]*-----BEGIN (?:[A-Z0-9]+ )*PRIVATE KEY(?: BLOCK)?-----"
    r"[\s\S]{0,32768}?"
    r"-----END (?:[A-Z0-9]+ )*PRIVATE KEY(?: BLOCK)?-----"
)
_PEM_HEADER = re.compile(
    r"(?m)^[ \t]*-----BEGIN (?:[A-Z0-9]+ )*PRIVATE KEY(?: BLOCK)?-----"
)
_CREDENTIAL_URL = re.compile(
    r"(?i)\b(?:postgres(?:ql)?|mysql|mariadb|mongodb(?:\+srv)?|rediss?|amqps?|ftps?|https?)"
    r"://[^\s:/@]{1,64}:(?P<password>[^\s/@]{1,256})@[^\s/@]{1,255}"
)
_ANTHROPIC_KEY = re.compile(r"\bsk-ant-(?:api\d{2}-)?[A-Za-z0-9_-]{16,200}\b")
_OPENAI_KEY = re.compile(r"\bsk-(?:proj-|svcacct-|admin-)?[A-Za-z0-9_-]{20,200}\b")
_GITHUB_TOKEN = re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{36,255}|github_pat_[A-Za-z0-9_]{22,255})\b")
_SLACK_TOKEN = re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,250}\b")
_GOOGLE_API_KEY = re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b")
_AWS_ACCESS_KEY = re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b")
_JWT = re.compile(
    r"\beyJ[A-Za-z0-9_-]{8,2048}\.[A-Za-z0-9_-]{8,2048}\.[A-Za-z0-9_-]{8,2048}\b"
)
_BEARER_TOKEN = re.compile(
    r"(?i)\bBearer[ \t]+(?P<token>[A-Za-z0-9._~+/=-]{16,4096})(?![A-Za-z0-9._~+/=-])"
)
_STRIPE_KEY = re.compile(r"\b(?:sk|rk)_live_[A-Za-z0-9]{16,247}\b")
_SENDGRID_KEY = re.compile(r"\bSG\.[A-Za-z0-9_-]{16,64}\.[A-Za-z0-9_-]{16,64}\b")
_NPM_TOKEN = re.compile(r"\bnpm_[A-Za-z0-9]{36}\b")
_GITLAB_TOKEN = re.compile(r"\bglpat-[A-Za-z0-9_-]{20,64}\b")
_HUGGINGFACE_TOKEN = re.compile(r"\bhf_[A-Za-z0-9]{30,64}\b")
_ENV_ASSIGNMENT = re.compile(
    r"(?m)^[ \t]*(?:export[ \t]+)?(?P<key>[A-Z_][A-Z0-9_]{0,127})="
    r"(?:\"(?P<double>[^\"\r\n]{0,1024})\""
    r"|'(?P<single>[^'\r\n]{0,1024})'"
    r"|(?P<bare>[^\"'\r\n][^\r\n]{0,1023}))"
)


@dataclass(frozen=True)
class Finding:
    """One credential-like span, without the matched secret itself."""

    kind: str
    start: int
    end: int
    placeholder: str


@dataclass(frozen=True)
class Scan:
    """The outcome of one bounded detection pass.

    ``truncated`` means ``text`` was longer than the scan limit. ``overflowed``
    means more findings were present than the cap allowed; ``overflow_at`` is
    where the accepted prefix of the text ends. A clean scan (:attr:`clean`) is
    the only one that proves nothing was found.
    """

    findings: tuple[Finding, ...] = ()
    truncated: bool = False
    overflowed: bool = False
    overflow_at: int | None = None

    @property
    def found(self) -> bool:
        """Whether at least one known credential shape was found."""
        return bool(self.findings)

    @property
    def clean(self) -> bool:
        """Whether the whole input was inspected and nothing was found."""
        return not (self.findings or self.truncated or self.overflowed)

    @property
    def kinds(self) -> tuple[str, ...]:
        """Finding kinds in text order, including duplicates."""
        return tuple(finding.kind for finding in self.findings)


@dataclass(frozen=True)
class Redaction:
    """Redacted text plus the scan that produced it."""

    text: str
    scan: Scan

    @property
    def findings(self) -> tuple[Finding, ...]:
        return self.scan.findings

    @property
    def truncated(self) -> bool:
        return self.scan.truncated

    @property
    def overflowed(self) -> bool:
        return self.scan.overflowed

    @property
    def changed(self) -> bool:
        """Whether the text changed, including a removed unscanned tail."""
        return bool(self.findings or self.truncated or self.overflowed)


@dataclass(frozen=True)
class _Rule:
    kind: str
    pattern: re.Pattern[str]
    locate: Callable[[re.Match[str]], tuple[int, int, str] | None] | None = None


def _whole(match: re.Match[str]) -> tuple[int, int, str]:
    return match.start(), match.end(), match.group(0)


def _secret_key(name: str) -> bool:
    """Whether an assignment name looks like it holds a credential.

    The check is on whole underscore-separated parts, so ``MONKEY``,
    ``TOKENIZER``, ``KEYBOARD`` and ``TOKEN_BUDGET`` are not credentials, while
    ``OPENAI_API_KEY``, ``DATABASE_PASSWORD`` and ``AWS_SECRET_ACCESS_KEY`` are.
    """
    parts = tuple(part for part in re.split(r"[^A-Za-z0-9]+", name.upper()) if part)
    if not parts:
        return False
    if len(parts) == 1:
        return parts[0] in _SECRET_WORDS
    if parts[-1] in _SECRET_LAST:
        return True
    return any(len(parts) >= len(suffix) and parts[-len(suffix):] == suffix
               for suffix in _SECRET_SUFFIXES)


def _usable_value(value: str) -> bool:
    """Reject obvious placeholders, variable references and empty values."""
    value = value.strip()
    if len(value) < 4:
        return False
    lowered = value.lower()
    if lowered in _PLACEHOLDER_VALUES:
        return False
    if lowered.startswith(_PLACEHOLDER_PREFIXES):
        return False
    if "redacted" in lowered:
        return False
    if len(set(lowered)) == 1 and lowered[0] in {"*", "x"}:
        return False
    return True


def _env_locate(match: re.Match[str]) -> tuple[int, int, str] | None:
    if not _secret_key(match.group("key")):
        return None
    for group in ("double", "single", "bare"):
        raw = match.group(group)
        if raw is None:
            continue
        if group == "bare":
            # A bare value may contain '#'; only whitespace before '#' starts
            # an inline comment. Redact the password, not the comment.
            raw = re.split(r"\s+#", raw, maxsplit=1)[0].rstrip()
        if not _usable_value(raw):
            return None
        end = match.start(group) + len(raw)
        return match.start(group), end, raw
    return None


def _url_locate(match: re.Match[str]) -> tuple[int, int, str] | None:
    if not _usable_value(match.group("password")):
        return None
    return _whole(match)


def _token_locate(match: re.Match[str]) -> tuple[int, int, str] | None:
    raw = match.group("token")
    if not _usable_value(raw):
        return None
    return match.start("token"), match.end("token"), raw


_RULES: tuple[_Rule, ...] = (
    _Rule("private_key", _PEM_BLOCK),
    _Rule("private_key", _PEM_HEADER),
    _Rule("credential_url", _CREDENTIAL_URL, _url_locate),
    _Rule("anthropic_key", _ANTHROPIC_KEY),
    _Rule("openai_key", _OPENAI_KEY),
    _Rule("github_token", _GITHUB_TOKEN),
    _Rule("slack_token", _SLACK_TOKEN),
    _Rule("google_api_key", _GOOGLE_API_KEY),
    _Rule("aws_access_key", _AWS_ACCESS_KEY),
    _Rule("jwt", _JWT),
    _Rule("bearer_token", _BEARER_TOKEN, _token_locate),
    _Rule("stripe_key", _STRIPE_KEY),
    _Rule("sendgrid_key", _SENDGRID_KEY),
    _Rule("npm_token", _NPM_TOKEN),
    _Rule("gitlab_token", _GITLAB_TOKEN),
    _Rule("huggingface_token", _HUGGINGFACE_TOKEN),
    _Rule("env_assignment", _ENV_ASSIGNMENT, _env_locate),
)


def _bounded(value: int, maximum: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return maximum
    return max(0, min(number, maximum))


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8", "surrogatepass")).hexdigest()[:4]


def _placeholder(kind: str, value: str) -> str:
    return f"[REDACTED:{kind}:{_digest(value)}]"


def _candidates(text: str) -> Iterable[tuple[int, int, int, str, str]]:
    """Every rule match in ``text`` as ``(start, end, priority, kind, value)``."""
    for priority, rule in enumerate(_RULES):
        for match in rule.pattern.finditer(text):
            located = (rule.locate or _whole)(match)
            if located is None:
                continue
            start, end, value = located
            if start < end:
                yield start, end, priority, rule.kind, value


def detect(text: str, *, max_scan_chars: int = MAX_SCAN_CHARS,
           max_findings: int = MAX_FINDINGS) -> Scan:
    """Scan ``text`` for known credential shapes, bounded in both size and count.

    Findings are ordered by position and never overlap. Longer matches win at
    the same start (a complete PEM block over its header), and rule order
    prefers a specific token over a generic ``.env`` assignment when both cover
    the same value.
    """
    limit = _bounded(max_scan_chars, MAX_SCAN_CHARS)
    cap = _bounded(max_findings, MAX_FINDINGS)
    prefix = text[:limit]
    candidates = sorted(_candidates(prefix), key=lambda item: (item[0], item[2], -item[1]))
    findings: list[Finding] = []
    end = -1
    overflow_at: int | None = None
    for start, stop, _priority, kind, value in candidates:
        if start < end:
            continue
        if len(findings) >= cap:
            overflow_at = start
            break
        findings.append(Finding(kind, start, stop, _placeholder(kind, value)))
        end = stop
    return Scan(tuple(findings), len(text) > limit, overflow_at is not None, overflow_at)


def redact(text: str, *, max_scan_chars: int = MAX_SCAN_CHARS,
           max_findings: int = MAX_FINDINGS) -> Redaction:
    """Replace detected credentials with deterministic placeholders.

    The unscanned tail and any overflow past :data:`MAX_FINDINGS` are replaced
    with a marker instead of being returned untouched, so the caller can treat
    ``truncated``/``overflowed`` as "incomplete inspection" rather than
    assuming the result is clean. Placeholders are never un-redacted.
    """
    scan = detect(text, max_scan_chars=max_scan_chars, max_findings=max_findings)
    limit = _bounded(max_scan_chars, MAX_SCAN_CHARS)
    if scan.overflowed and scan.overflow_at is not None:
        stop = scan.overflow_at
        marker = f"[REDACTED:overflow:{len(text) - stop}]"
    elif scan.truncated:
        stop = limit
        marker = f"[REDACTED:unscanned:{len(text) - stop}]"
    else:
        stop = len(text)
        marker = ""
    pieces: list[str] = []
    position = 0
    for finding in scan.findings:
        if finding.start >= stop:
            break
        pieces.append(text[position:finding.start])
        pieces.append(finding.placeholder)
        position = finding.end
    pieces.append(text[position:stop])
    if marker:
        pieces.append(marker)
    return Redaction("".join(pieces), scan)


def contains_secret(text: str, *, max_scan_chars: int = MAX_SCAN_CHARS) -> bool:
    """Whether a credential was found or the text could not be fully inspected.

    A truncated input is reported as positive on purpose: the caller cannot
    treat a partial scan as proof that the text is clean.
    """
    return not detect(text, max_scan_chars=max_scan_chars).clean
