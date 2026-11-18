"""Static accessibility guards for the shipped web UI (#201).

These parse the served pages and the scripts that build dialogs, and assert
the parts of WCAG 1.1.1 (text alternatives) and 4.1.2 (name, role, value)
that a static scan can see: every button has an accessible name, every image
has an alt attribute, every visible form control has a label, every dialog is
named, ARIA id references point at real elements, and every popup toggle
reports its state. No browser and no dependencies.

Focus order, focus return and live-region timing are dynamic and are not
covered here.
"""

from __future__ import annotations

import re
from html.parser import HTMLParser
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
ASSETS = ROOT / "src" / "kova" / "web_assets"
PAGES = [ASSETS / "index.html", ASSETS / "token-sheet.html"]

VOID = {
    "area", "base", "br", "col", "embed", "hr", "img", "input", "link",
    "meta", "param", "source", "track", "wbr",
}


class _Node:
    __slots__ = ("tag", "attrs", "children", "text", "parent")

    def __init__(self, tag, attrs, parent):
        self.tag = tag
        self.attrs = attrs or {}
        self.children = []
        self.text = ""
        self.parent = parent

    def ancestors(self):
        node = self.parent
        while node is not None:
            yield node
            node = node.parent

    def visible_text(self):
        """Descendant text, skipping subtrees marked aria-hidden."""
        out = []

        def walk(node, hidden):
            hidden = hidden or str(node.attrs.get("aria-hidden", "")).lower() == "true"
            for child in node.children:
                if child.tag is None:
                    if not hidden:
                        out.append(child.text)
                else:
                    walk(child, hidden)

        walk(self, False)
        return "".join(out)


class _Builder(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root = _Node("#root", {}, None)
        self.stack = [self.root]

    def handle_starttag(self, tag, attrs):
        node = _Node(tag, {k: v for k, v in attrs}, self.stack[-1])
        self.stack[-1].children.append(node)
        if tag not in VOID:
            self.stack.append(node)

    def handle_startendtag(self, tag, attrs):
        node = _Node(tag, {k: v for k, v in attrs}, self.stack[-1])
        self.stack[-1].children.append(node)

    def handle_endtag(self, tag):
        for i in range(len(self.stack) - 1, 0, -1):
            if self.stack[i].tag == tag:
                del self.stack[i:]
                return

    def handle_data(self, data):
        if data:
            self.stack[-1].children.append(_Node(None, {}, self.stack[-1]))
            self.stack[-1].children[-1].text = data

    def handle_comment(self, data):
        pass


def _build(text: str) -> _Node:
    parser = _Builder()
    parser.feed(text)
    parser.close()
    return parser.root


def _walk(node):
    yield node
    for child in node.children:
        if child.tag is not None:
            yield from _walk(child)


def _named(node) -> bool:
    if node.attrs.get("aria-label") or node.attrs.get("aria-labelledby"):
        return True
    return bool(node.visible_text().strip())


def _has_label(node, labels) -> bool:
    if node.attrs.get("aria-label") or node.attrs.get("aria-labelledby"):
        return True
    for anc in node.ancestors():
        if anc.tag == "label":
            return True
    control_id = node.attrs.get("id")
    if control_id and any(l.attrs.get("for") == control_id for l in labels):
        return True
    return False


def _is_hidden(node) -> bool:
    """A control the accessibility tree does not expose."""
    if "hidden" in node.attrs:
        return True
    if node.tag == "input" and str(node.attrs.get("type") or "").lower() == "hidden":
        return True
    return False


_REF_ATTRS = ("aria-labelledby", "aria-describedby", "aria-controls", "for")


def _problems_for(page: str, root: _Node) -> list[str]:
    problems = []
    nodes = list(_walk(root))
    labels = [n for n in nodes if n.tag == "label"]
    ids = {n.attrs["id"] for n in nodes if n.attrs.get("id")}
    for node in nodes:
        where = f"{page}: <{node.tag} id=\"{node.attrs.get('id', '')}\">"
        if node.tag == "img" and "alt" not in node.attrs:
            problems.append(f"{where} is missing an alt attribute")
        elif node.tag == "button" and not _named(node):
            problems.append(f"{where} has no accessible name")
        elif node.tag in ("input", "textarea", "select"):
            if not _is_hidden(node) and not _has_label(node, labels):
                problems.append(f"{where} has no label")
        elif node.tag == "dialog" and not (
            node.attrs.get("aria-label") or node.attrs.get("aria-labelledby")
        ):
            problems.append(f"{where} has no accessible name")
        if node.attrs.get("aria-haspopup") and "aria-expanded" not in node.attrs:
            problems.append(f"{where} opens a popup but has no aria-expanded")
        for attr in _REF_ATTRS:
            for ref in str(node.attrs.get(attr) or "").split():
                if ref not in ids:
                    problems.append(f"{where} {attr} points at missing id \"{ref}\"")
    return problems


@pytest.mark.parametrize("page", PAGES, ids=lambda p: p.name)
def test_shipped_markup_is_accessible(page: Path) -> None:
    problems = _problems_for(page.name, _build(page.read_text(encoding="utf-8")))
    assert not problems, "\n".join(problems)


# A dialog built in a script is named on the line that creates it or within
# the next few lines (aria-label / aria-labelledby).
_DIALOG = re.compile(r"(?:el\(\s*'dialog'|createElement\(\s*'dialog'\s*\))")


def test_script_built_dialogs_are_named() -> None:
    problems = []
    for script in sorted(ASSETS.glob("*.js")):
        lines = script.read_text(encoding="utf-8").splitlines()
        for number, line in enumerate(lines):
            if not _DIALOG.search(line):
                continue
            window = "\n".join(lines[number:number + 4])
            if "aria-label" not in window:
                problems.append(f"{script.name}:{number + 1}: dialog has no accessible name")
    assert not problems, "\n".join(problems)


def test_parser_catches_problems() -> None:
    """The guard itself works: a broken page reports every kind of problem."""
    bad = (
        '<button><span aria-hidden="true">x</span></button><img src="a.png">'
        '<input id="q"><dialog></dialog><button aria-haspopup="menu">Menu</button>'
        '<div aria-labelledby="nope"></div>'
    )
    problems = _problems_for("bad.html", _build(bad))
    assert len(problems) == 6, problems
    good = (
        '<label for="q">Search</label><input id="q"><img src="a.png" alt="">'
        '<button aria-label="Close"><span aria-hidden="true">x</span></button>'
        '<input type="hidden"><select hidden></select>'
    )
    assert _problems_for("good.html", _build(good)) == []
