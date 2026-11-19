"""The optional user stylesheet, served same-origin and sanitised.

Custom CSS is off by default: the switch is the ``custom_css`` UI preference,
and the stylesheet itself is ``KOVA_HOME/custom.css``. When it is on, the
server serves it at ``/app/custom.css`` so the strict CSP (``style-src 'self'``)
still holds. It is never inlined and never fetched from another origin.

A stylesheet can make the browser fetch things on its own, so the file is
bounded and stripped of the two constructs that reach outside the app:
``@import`` and any ``url(...)`` that does not point at the packaged
``/app/assets/`` tree. A stylesheet that hides the whole UI can be bypassed
with ``?safe=1``, which loads the page without this sheet.
"""

from __future__ import annotations

import re
from pathlib import Path

#: The largest stylesheet Kova will read; a comment or rule past this is refused.
MAX_BYTES = 64 * 1024
#: The file's fixed name in KOVA_HOME.
FILE_NAME = "custom.css"
#: The only URL prefix a stylesheet may reference: packaged, same-origin assets.
ASSET_PREFIX = "/app/assets/"

_IMPORT = re.compile(r"@import\b[^;]*;?", re.IGNORECASE)
_URL = re.compile(
    r"""url\s*\(\s*(?P<quote>['"]?)(?P<target>.*?)(?P=quote)\s*\)""",
    re.IGNORECASE | re.DOTALL,
)


class CustomCssError(ValueError):
    """The stylesheet cannot be read as text CSS."""


class CustomCssTooLarge(CustomCssError):
    """The stylesheet is past the size cap."""


def path_for(home: Path | str) -> Path:
    """Where the user keeps their stylesheet."""
    return Path(home) / FILE_NAME


def _allowed(target: str) -> bool:
    """Only a same-origin asset path may be requested."""
    return target.strip().startswith(ASSET_PREFIX)


def sanitize(text: str) -> str:
    """Drop ``@import`` and every ``url(...)`` that leaves the packaged assets."""
    text = _IMPORT.sub("", text)

    def replace(match: re.Match[str]) -> str:
        return match.group(0) if _allowed(match.group("target")) else "none"

    return _URL.sub(replace, text)


def load(home: Path | str) -> str:
    """Read and sanitise ``KOVA_HOME/custom.css``.

    An absent file is an empty sheet: it is not an error, because the switch can
    be on before the first edit. An oversized file is refused rather than
    silently cut, so the user sees why their rules did not apply.
    """
    target = path_for(home)
    try:
        with target.open("rb") as stream:
            raw = stream.read(MAX_BYTES + 1)
    except FileNotFoundError:
        return ""
    except OSError as exc:
        raise CustomCssError(f"Could not read {FILE_NAME}: {exc.strerror or exc}") from exc
    if len(raw) > MAX_BYTES:
        raise CustomCssTooLarge(f"{FILE_NAME} must be at most 64 KiB.")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise CustomCssError(f"{FILE_NAME} must be UTF-8 text.") from exc
    return sanitize(text)
