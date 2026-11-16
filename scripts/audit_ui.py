"""Static accessibility checks for the pages and stylesheets the web app serves.

This is the cheap half of #201: the mistakes that appear when a screen is copied
and nobody notices until someone tries to use it with a keyboard. It is not a
substitute for a real audit, and it says so rather than pretending otherwise.

Per HTML file: a declared language, a viewport, a title, no inline event
handlers, and an ``alt`` on every image. Per stylesheet: a ``:focus-visible``
rule, a reduced-motion block, and no focus state that removes its outline
without a visible replacement in the same rule.

Usage: ``python scripts/audit_ui.py`` audits the served assets; pass paths to
audit something else.
"""

from __future__ import annotations

import argparse
import re
import sys
from html.parser import HTMLParser
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ASSETS = ROOT / "src" / "kova" / "web_assets"

INLINE_HANDLER = re.compile(r"^on[a-z]+$")
RULE = re.compile(r"([^{}]+)\{([^{}]*)\}")
OUTLINE_REMOVED = re.compile(r"outline\s*:\s*(?:none|0)\b")
VISIBLE_REPLACEMENT = re.compile(r"box-shadow\s*:|border(?:-color)?\s*:")
SUFFIXES = (".html", ".htm", ".css")


class _Document(HTMLParser):
    """Collects just the facts the checks need; ignores everything else."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.lang = ""
        self.title = False
        self.viewport = False
        self.inline_handlers: list[str] = []
        self.images_without_alt: list[str] = []

    def handle_starttag(self, tag: str, attrs) -> None:
        attributes = dict(attrs)
        if tag == "html":
            self.lang = (attributes.get("lang") or "").strip()
        elif tag == "title":
            self.title = True
        elif tag == "meta" and (attributes.get("name") or "").lower() == "viewport":
            self.viewport = True
        elif tag == "img" and "alt" not in attributes:
            self.images_without_alt.append(attributes.get("src") or "?")
        for name in attributes:
            if INLINE_HANDLER.match(name):
                self.inline_handlers.append(f"<{tag} {name}=...>")


def check_html(text: str, name: str = "<html>") -> list[str]:
    """Every problem in one page's markup, in document order."""
    document = _Document()
    document.feed(text)
    document.close()
    problems: list[str] = []
    if not document.lang:
        problems.append(f"{name}: <html> has no lang attribute")
    if not document.title:
        problems.append(f"{name}: no <title>")
    if not document.viewport:
        problems.append(f'{name}: no <meta name="viewport">')
    problems.extend(f"{name}: inline event handler {handler}" for handler in document.inline_handlers)
    problems.extend(f"{name}: <img src={src!r}> has no alt attribute"
                    for src in document.images_without_alt)
    return problems


TOKEN_SELECTOR = re.compile(r"^(:root|\[data-theme=[^\]]*\])(\s*,\s*(:root|\[data-theme=[^\]]*\]))*$")


def _declares_only_tokens(text: str) -> bool:
    """A generated token sheet holds only custom properties on :root / [data-theme].

    It styles nothing, so a focus ring or a reduced-motion block belongs to the
    stylesheet that does (app.css), not to it.
    """
    selectors = [m.group(1).strip() for m in RULE.finditer(re.sub(r"/\*.*?\*/", "", text, flags=re.S))]
    return bool(selectors) and all(TOKEN_SELECTOR.match(sel) for sel in selectors)


def check_css(text: str, name: str = "<style>") -> list[str]:
    """Every problem in one stylesheet.

    An ``outline`` is only suspicious on a focus state: a container that removes
    its own outline is inert, which is why the check looks at ``:focus`` selectors
    rather than every rule. A removed outline with no ``box-shadow`` or border in
    the same rule means the focus state is invisible.
    """
    problems: list[str] = []
    if not _declares_only_tokens(text):
        if ":focus-visible" not in text:
            problems.append(f"{name}: no :focus-visible rule, so keyboard focus is invisible")
        if "prefers-reduced-motion" not in text:
            problems.append(f"{name}: no @media (prefers-reduced-motion: reduce) block")
    for match in RULE.finditer(text):
        selector, body = match.group(1).strip(), match.group(2)
        if ":focus" not in selector:
            continue
        if OUTLINE_REMOVED.search(body) and not VISIBLE_REPLACEMENT.search(body):
            line = text.count("\n", 0, match.start(2)) + 1
            problems.append(f"{name}:{line}: {selector!r} removes the outline on a focus "
                            "state without a box-shadow or border in the same rule")
    return problems


def audit_file(path: Path) -> list[str]:
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as error:
        return [f"{path}: cannot read ({error})"]
    if path.suffix.lower() == ".css":
        return check_css(text, path.name)
    return check_html(text, path.name)


def _candidates(path: Path) -> list[Path]:
    if path.is_dir():
        return sorted(p for p in path.rglob("*") if p.suffix.lower() in SUFFIXES)
    return [path] if path.suffix.lower() in SUFFIXES else []


def audit(paths: list[Path]) -> list[str]:
    """Every problem in every path, empty when the UI passes."""
    return [problem for path in paths for candidate in _candidates(path)
            for problem in audit_file(candidate)]


def default_paths() -> list[Path]:
    return [ASSETS]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("paths", nargs="*", type=Path,
                        help="files or directories (default: the served web assets)")
    args = parser.parse_args(argv)
    paths = args.paths or default_paths()
    problems = audit(paths)
    for problem in problems:
        print(problem)
    if problems:
        print(f"\n{len(problems)} accessibility problem(s).", file=sys.stderr)
        return 1
    print(f"ui ok: {sum(len(_candidates(path)) for path in paths)} files")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
