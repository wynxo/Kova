"""docs/ARCHITECTURE.md must list the routes and CLI commands the code really has.

The arch doc is the first file CONTRIBUTING sends a contributor to, so its route
table is checked against the live app in both directions: a route added without a
row fails, and a row left behind by a deleted route fails too.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from fastapi.routing import APIRoute
from starlette.routing import Mount
from typer.main import get_command

from kova.cli import app as cli
from kova.config import Config
from kova.server import create_app

DOC_PATH = Path(__file__).resolve().parents[1] / "docs" / "ARCHITECTURE.md"

# Routes that carry their credential in the body or a pairing secret instead of
# a dependency, so the auth column names them explicitly.
BODY_CREDENTIALS = {"/app/login": "ticket", "/app/pair": "pairing claim"}

# | `GET/PATCH /thing` | token or session | what it is for |
ROW = re.compile(
    r"^\|\s*`(?P<methods>[A-Z]+(?:/[A-Z]+)*)\s+(?P<path>[^`\s]+)`\s*"
    r"\|\s*(?P<auth>[a-z][a-z, \-]*?)\s*\|\s*(?P<purpose>\S.*?)\s*\|\s*$",
    re.MULTILINE,
)

def _doc_rows() -> list[tuple[str, str, str, str]]:
    """Every route row of the doc as (method, path, auth, purpose)."""
    return [(method, match.group("path"), match.group("auth"), match.group("purpose"))
            for match in ROW.finditer(DOC_PATH.read_text(encoding="utf-8"))
            for method in match.group("methods").split("/")]


def _dependency(route: APIRoute) -> list[str]:
    return [getattr(getattr(dep, "call", None), "__name__", "")
            for dep in route.dependant.dependencies]


def _auth(deps: tuple[str, ...], path: str) -> str:
    if "require_local" in deps:
        return "token or session, local only"
    if "_require_token" in deps:
        return "token or session"
    if "_require_bearer" in deps:
        return "bearer token"
    if "require_browser_session" in deps:
        return "browser session"
    return BODY_CREDENTIALS.get(path, "public")


def _walk(routes, inherited: tuple[str, ...] = (), prefix: str = ""):
    """Yield (method, path, auth) for every HTTP route, included routers too.

    FastAPI keeps ``include_router`` lazy — an ``_IncludedRouter`` in
    ``app.routes`` — so the walk descends into it and carries that router's own
    dependencies and prefix down with the paths.
    """
    for route in routes:
        included = getattr(route, "original_router", None)
        if included is not None:
            context = route.include_context
            yield from _walk(included.routes,
                             inherited + tuple(_dependency(d) for d in context.dependencies),
                             prefix + (context.prefix or ""))
            continue
        if isinstance(route, Mount):
            yield "GET", f"{prefix}{route.path}/*", "public"
            continue
        if not isinstance(route, APIRoute):
            continue
        deps = inherited + tuple(_dependency(route))
        path = prefix + route.path
        for method in sorted(route.methods):
            yield method, path, _auth(deps, path)


@pytest.fixture(scope="module")
def live_routes(tmp_path_factory) -> dict[tuple[str, str], str]:
    """Every (method, path) the app answers, and how it authenticates."""
    home = tmp_path_factory.mktemp("architecture-home")
    config = Config(home=home, models=[], active="",
                    memory_path=home / "memory", files_path=home / "files")
    app = create_app(config)
    return {(method, path): auth for method, path, auth in _walk(app.routes)}


def test_every_route_is_documented(live_routes):
    documented = {(method, path) for method, path, _auth_label, _purpose in _doc_rows()}
    missing = sorted(documented.symmetric_difference(live_routes))
    assert not missing, ("docs/ARCHITECTURE.md and the app disagree about these routes "
                         "(add or delete the row):\n  " +
                         "\n  ".join(f"{method} {path}" for method, path in missing))


def test_route_table_lists_each_route_once(live_routes):
    seen = [(method, path) for method, path, _auth_label, _purpose in _doc_rows()]
    duplicates = sorted({key for key in seen if seen.count(key) > 1})
    assert not duplicates, f"documented more than once: {duplicates}"
    assert len(seen) == len(live_routes)


def test_documented_auth_matches_the_app(live_routes):
    wrong = [(method, path, auth, live_routes[(method, path)])
             for method, path, auth, _purpose in _doc_rows()
             if live_routes.get((method, path)) != auth]
    assert not wrong, "auth column drifted from the app's dependencies:\n  " + "\n  ".join(
        f"{method} {path}: doc says {auth!r}, app says {real!r}"
        for method, path, auth, real in wrong)


def test_every_documented_row_explains_itself():
    empty = [(method, path) for method, path, _auth_label, purpose in _doc_rows()
             if len(purpose) < 8]
    assert not empty, f"rows without a purpose: {empty}"


def test_cli_section_lists_every_command():
    doc = DOC_PATH.read_text(encoding="utf-8")
    section = doc.split("### CLI (`src/kova/cli.py`)", 1)[1].split("\n### ", 1)[0]
    # Hidden commands (the `chat` back-compat alias) are deliberately not in `--help`.
    visible = [name for name, command in get_command(cli).commands.items() if not command.hidden]
    missing = sorted(name for name in visible if f"`{name}`" not in section)
    assert not missing, f"`kova --help` offers commands the doc never names: {missing}"