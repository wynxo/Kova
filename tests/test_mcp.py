"""MCP client (#135): stdio and streamable HTTP against a tiny fake server."""

from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

import kova.cli as cli
from kova import mcp
from kova.client import KovaClient
from kova.config import McpServerEntry, ensure_token, load_config, save_config
from kova.permissions import Approvals, needs_approval
from kova.server import create_app
from kova.tools import ToolContext, ToolError, build_tools
from tests.fake_mcp_server import TOOLS, HttpFake
from tests.fake_model import FakeModel, text, tool
from tests.test_agent_core import real_server

FAKE = str(Path(__file__).with_name("fake_mcp_server.py"))


def stdio(name: str = "fake", **fields) -> McpServerEntry:
    return McpServerEntry(name=name, command=sys.executable, args=[FAKE], **fields)


@pytest.fixture
def manager(tmp_path):
    managers = []

    def make(*servers, **options):
        made = mcp.McpManager(tmp_path / "home", **options)
        made.reload(servers, timeout=15)
        managers.append(made)
        return made

    yield make
    for made in managers:
        made.close()


def run(tools, name, args=None):
    return next(t for t in tools if t.name == name).run(args or {})


# -- names and validation -------------------------------------------------------

def test_tool_names_are_namespaced_and_function_safe():
    assert mcp.tool_name("github", "create_issue") == "github__create_issue"
    assert mcp.tool_name("fake", "ask.ping/x") == "fake__ask_ping_x"
    assert len(mcp.tool_name("s", "x" * 200)) == mcp.MAX_TOOL_NAME


@pytest.mark.parametrize("server, reason", [
    (McpServerEntry(name="bad name", command="x"), "names use"),
    (McpServerEntry(name="a__b", command="x"), "names use"),
    (McpServerEntry(name="none"), "exactly one"),
    (McpServerEntry(name="both", command="x", url="http://x"), "exactly one"),
    (McpServerEntry(name="web", url="ftp://x"), "http"),
    (McpServerEntry(name="web", url="https://x", sandbox=True), "stdio servers only"),
])
def test_validate_rejects_unusable_entries(server, reason):
    with pytest.raises(ValueError, match=reason):
        mcp.validate_server(server)


def test_format_result_covers_content_kinds():
    assert mcp.format_result({"content": [{"type": "text", "text": "a"},
                                          {"type": "image", "data": "AAAA", "mimeType": "image/png"},
                                          {"type": "resource", "resource": {"uri": "file:///x", "text": "body"}},
                                          {"type": "resource_link", "uri": "file:///y", "name": "y"}]}) == \
        "a\n[image: image/png, about 3 bytes, not shown]\nbody\n[link: file:///y] y"
    assert mcp.format_result({"content": [], "structuredContent": {"sum": 3}}) == '{"sum": 3}'
    assert mcp.format_result({}) == "(no output)"
    with pytest.raises(ToolError, match="nope"):
        mcp.format_result({"content": [{"type": "text", "text": "nope"}], "isError": True})


# -- stdio ----------------------------------------------------------------------

def test_stdio_lists_every_page_and_calls_tools(manager):
    made = manager(stdio(env={"FAKE_MCP_TOKEN": "secret-value"}))
    tools = made.tools()
    assert [t.name for t in tools] == [mcp.tool_name("fake", t["name"]) for t in TOOLS]
    assert all(t.external for t in tools)
    echo = next(t for t in tools if t.name == "fake__echo")
    assert echo.description.startswith("[fake MCP] Echo")
    assert echo.parameters["required"] == ["text"]
    assert next(t for t in tools if t.name == "fake__fail").parameters == {"type": "object", "properties": {}}
    assert run(tools, "fake__echo", {"text": "hello"}) == "hello"
    assert run(tools, "fake__add", {"a": 2, "b": 3}) == '{"sum": 5}'
    assert run(tools, "fake__env") == "secret-value"
    # A server-to-client request (ping) in the middle of a call is answered.
    assert run(tools, "fake__ask_ping_x") == "pong"
    with pytest.raises(ToolError, match="failed on purpose"):
        run(tools, "fake__fail")
    status = made.status()[0]
    assert status["state"] == "connected" and status["server_info"] == {"name": "fake-mcp", "version": "1.0"}
    assert "secret-value" not in repr(status)


def test_stdio_server_restarts_after_a_crash(manager):
    made = manager(stdio())
    tools = made.tools()
    before = run(tools, "fake__pid")
    with pytest.raises(ToolError, match="stopped"):
        run(tools, "fake__crash")
    # The crashed call is reported, never repeated; the next call restarts it.
    after = run(tools, "fake__pid")
    assert after.isdigit() and after != before


def test_a_server_that_keeps_dying_is_not_restarted_forever(tmp_path):
    client = mcp.McpClient(stdio(), tmp_path)
    try:
        client.start(15)
        for _ in range(mcp.MAX_RESTARTS - 1):
            with pytest.raises(mcp.McpError):
                client.call_tool("crash", {})
            client.call_tool("pid", {})  # restarts
        with pytest.raises(mcp.McpError):
            client.call_tool("crash", {})
        with pytest.raises(mcp.McpError, match="keeps stopping"):
            client.call_tool("pid", {})
    finally:
        client.close()


def test_failed_servers_are_reported_without_blocking_others(manager, tmp_path):
    broken = McpServerEntry(name="broken", command=sys.executable,
                            args=["-c", "import sys; sys.stderr.write('missing token\\n'); sys.exit(2)"])
    missing = McpServerEntry(name="missing", command=str(tmp_path / "no-such-program"))
    made = manager(stdio(), broken, missing)
    rows = {row["name"]: row for row in made.status()}
    assert rows["fake"]["state"] == "connected"
    assert rows["broken"]["state"] == "error" and "missing token" in rows["broken"]["error"]
    assert rows["missing"]["state"] == "error" and "could not start" in rows["missing"]["error"]
    assert {t.name.split("__")[0] for t in made.tools()} == {"fake"}


def test_stop_reason_waits_for_stderr_to_drain(manager, monkeypatch):
    # Stderr has its own reader thread, which can lag behind the process exit.
    # The stop reason must still quote what the server printed.
    real_drain = mcp._Stdio._drain

    def slow_drain(self):
        time.sleep(0.3)
        real_drain(self)

    monkeypatch.setattr(mcp._Stdio, "_drain", slow_drain)
    broken = McpServerEntry(name="broken", command=sys.executable,
                            args=["-c", "import sys; sys.stderr.write('missing token\\n'); sys.exit(2)"])
    made = manager(broken)
    row = next(row for row in made.status() if row["name"] == "broken")
    assert row["state"] == "error"
    assert "missing token" in row["error"]


def test_disabled_tools_and_servers_offer_nothing(manager):
    made = manager(stdio(disabled_tools=["crash", "fail"]), stdio("off", enabled=False))
    names = {t.name for t in made.tools()}
    assert "fake__echo" in names and "fake__crash" not in names and "fake__fail" not in names
    rows = {row["name"]: row for row in made.status()}
    assert rows["off"]["state"] == "off" and rows["off"]["tools"] == []
    assert {t["name"]: t["enabled"] for t in rows["fake"]["tools"]}["crash"] is False


def test_reload_keeps_unchanged_servers_and_restarts_changed_ones(manager):
    made = manager(stdio())
    pid = run(made.tools(), "fake__pid")
    made.reload([stdio(disabled_tools=["echo"])])  # a switch, not a new connection
    assert run(made.tools(), "fake__pid") == pid
    made.reload([stdio(env={"FAKE_MCP_TOKEN": "new"})])
    assert run(made.tools(), "fake__pid") != pid
    made.reload([stdio(env={"FAKE_MCP_TOKEN": "new"})], restart=["fake"])
    assert run(made.tools(), "fake__env") == "new"
    made.reload([])
    assert made.tools() == [] and made.status() == []


def test_sandboxed_server_fails_closed_without_landlock(manager, monkeypatch):
    from kova import sandbox
    monkeypatch.setattr(sandbox, "available", lambda: False)
    made = manager(stdio(sandbox=True))
    row = made.status()[0]
    assert row["state"] == "error" and "sandbox" in row["error"]


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Landlock is Linux-only")
def test_sandboxed_server_runs_in_its_own_folder(manager, tmp_path):
    from kova import sandbox
    if not sandbox.available():
        pytest.skip("Landlock is unavailable here")
    made = manager(stdio(sandbox=True))
    assert run(made.tools(), "fake__echo", {"text": "confined"}) == "confined"
    assert (tmp_path / "home" / "mcp" / "fake").is_dir()


def test_build_mcp_tools_and_build_tools_merge(tmp_path):
    made = mcp.McpManager(tmp_path)
    try:
        tools = mcp.build_mcp_tools([stdio()], made, timeout=15)
        assert "fake__echo" in {t.name for t in tools}
        cfg = load_config(tmp_path / "home")
        from kova.memory import Memory
        from kova.store import Store
        from datetime import datetime
        ctx = ToolContext(cfg, Store(tmp_path / "kova.db"), Memory(tmp_path / "memory"), datetime.now, mcp=made)
        registry = build_tools(ctx)
        assert "fake__echo" in registry and "web_search" in registry
        assert not registry["web_search"].external and registry["fake__echo"].external
    finally:
        made.close()


# -- streamable HTTP ------------------------------------------------------------

@pytest.fixture
def http_server():
    servers = []

    def make(**options):
        server = HttpFake(**options)
        servers.append(server)
        return server

    yield make
    for server in servers:
        server.close()


@pytest.mark.parametrize("sse", [False, True])
def test_http_lists_and_calls_tools_with_sessions(manager, http_server, sse):
    fake = http_server(sse=sse, require={"Authorization": "Bearer t0ken"})
    made = manager(McpServerEntry(name="web", url=fake.url, headers={"Authorization": "Bearer t0ken"}))
    tools = made.tools()
    assert len(tools) == len(TOOLS)
    assert run(tools, "web__echo", {"text": "over http"}) == "over http"
    with pytest.raises(ToolError, match="failed on purpose"):
        run(tools, "web__fail")
    assert fake.initializes == 1
    # The server forgets the session: Kova initializes again and retries once.
    fake.expire()
    assert run(tools, "web__echo", {"text": "again"}) == "again"
    assert fake.initializes == 2
    made.close()
    assert len(fake.deleted) == 1


def test_http_errors_are_reported(manager, http_server):
    fake = http_server(require={"Authorization": "Bearer right"})
    made = manager(McpServerEntry(name="web", url=fake.url, headers={"Authorization": "Bearer wrong"}),
                   McpServerEntry(name="gone", url="http://127.0.0.1:9/mcp"))
    rows = {row["name"]: row for row in made.status()}
    assert "HTTP 401" in rows["web"]["error"]
    assert "cannot reach" in rows["gone"]["error"]
    assert "wrong" not in repr(rows)


# -- permissions ----------------------------------------------------------------

def test_external_tools_ask_unless_full_access(tmp_path):
    for permission in ("manual", "auto"):
        assert needs_approval("fake__echo", {"text": "x"}, permission, tmp_path, tmp_path, external=True)
    assert not needs_approval("fake__echo", {"text": "x"}, "full", tmp_path, tmp_path, external=True)


async def test_approval_card_shows_mcp_arguments():
    approvals = Approvals()
    event = approvals.create("c", "fake__echo", {"text": "hello"})
    assert event["summary"] == 'fake__echo: {"text": "hello"}'


# -- config ---------------------------------------------------------------------

def test_config_round_trip_keeps_comments_and_unknown_keys(tmp_path):
    home = tmp_path / "home"
    load_config(home)
    path = home / "config.toml"
    path.write_text(path.read_text() + '''
# my MCP servers
[[mcp]]
name = "github"  # the GitHub one
command = "npx"
args = ["-y", "github-mcp"]
env = { GITHUB_TOKEN = "ghp_example" }
future_key = "kept"

[[mcp]]
name = "docs"
url = "https://example.com/mcp"
enabled = false
''')
    cfg = load_config(home)
    assert [s.name for s in cfg.mcp] == ["github", "docs"]
    assert cfg.mcp[0].env == {"GITHUB_TOKEN": "ghp_example"} and cfg.mcp[1].enabled is False
    assert "ghp_example" not in repr(cfg.mcp[0])
    cfg.mcp[0].disabled_tools = ["delete_repo"]
    save_config(cfg)
    saved = path.read_text()
    assert "# my MCP servers" in saved and "# the GitHub one" in saved and 'future_key = "kept"' in saved
    assert 'disabled_tools = ["delete_repo"]' in saved
    assert "url" not in saved.split("[[mcp]]")[1]  # a stdio entry grows no empty url
    again = load_config(home)
    assert again.mcp == cfg.mcp
    again.mcp = []
    save_config(again)
    assert "[[mcp]]" not in path.read_text() and load_config(home).mcp == []


def test_unknown_mcp_keys_are_reported():
    from kova.config import unknown_config_keys
    assert unknown_config_keys({"mcp": [{"name": "x", "comand": "y"}]}) == [("mcp[0].comand", "mcp[0].command")]


def test_config_show_and_backups_hide_mcp_secrets(tmp_path, monkeypatch):
    monkeypatch.setenv("KOVA_HOME", str(tmp_path / "home"))
    cfg = load_config()
    cfg.mcp = [stdio(env={"FAKE_MCP_TOKEN": "env-secret"}),
               McpServerEntry(name="web", url="https://example.com/mcp", headers={"Authorization": "Bearer hdr-secret"})]
    save_config(cfg)
    result = CliRunner().invoke(cli.app, ["config", "show", "--json"])
    assert result.exit_code == 0, result.output
    assert "env-secret" not in result.output and "hdr-secret" not in result.output
    assert "FAKE_MCP_TOKEN" in result.output
    table = CliRunner().invoke(cli.app, ["config", "show"])
    assert "mcp.0" in table.output and "env-secret" not in table.output
    from kova.backup import _config_bytes
    exported = _config_bytes(load_config()).decode()
    assert "env-secret" not in exported and "hdr-secret" not in exported and "[[mcp]]" in exported


# -- server, CLI and an end-to-end turn ------------------------------------------

@pytest.fixture
def api(tmp_path, monkeypatch):
    monkeypatch.setenv("KOVA_HOME", str(tmp_path / "home"))
    cfg = load_config()
    app = create_app(cfg, transport=FakeModel([]).transport)
    with TestClient(app) as http:
        client = KovaClient("http://127.0.0.1:7700", ensure_token(cfg.home), http=http)
        monkeypatch.setattr(cli, "get_client", lambda: client)
        yield client, cfg, app


def stdio_body(name="fake"):
    return {"name": name, "command": sys.executable, "args": [FAKE]}


def test_routes_add_switch_test_and_remove(api):
    client, cfg, app = api
    row = client.mcp_add(stdio_body())
    assert row["state"] == "connected" and len(row["tools"]) == len(TOOLS)
    assert "fake__echo" in app.state.agent.tools
    assert load_config(cfg.home).mcp[0].command == sys.executable
    with pytest.raises(Exception, match="already exists"):
        client.mcp_add(stdio_body())
    with pytest.raises(Exception, match="names use"):
        client.mcp_add(stdio_body("bad name"))
    patched = client._request("PATCH", "/mcp/fake/tools/crash", json={"enabled": False, "always_allow": False}).json()
    assert {t["name"]: t["enabled"] for t in patched["tools"]}["crash"] is False
    assert "fake__crash" not in app.state.agent.tools
    assert load_config(cfg.home).mcp[0].disabled_tools == ["crash"]
    allowed = client._request("PATCH", "/mcp/fake/tools/echo", json={"always_allow": True}).json()
    assert {t["name"]: t["always_allow"] for t in allowed["tools"]}["echo"] is True
    assert app.state.agent.approvals.policy("c", "fake__echo", {"text": "x"})[0] == "allow"
    assert client.mcp_update("fake", enabled=False)["state"] == "off"
    assert not any(name.startswith("fake__") for name in app.state.agent.tools)
    assert client.mcp_update("fake", enabled=True)["state"] == "connected"
    assert client.mcp_test("fake")["state"] == "connected"
    client.mcp_remove("fake")
    assert client.mcp_list() == [] and load_config(cfg.home).mcp == []
    # Its always-allow grant went with it: a new "fake" must be asked about again.
    assert app.state.agent.approvals.policy("c", "fake__echo", {"text": "x"})[0] is None


def test_a_paired_phone_cannot_add_a_server(api):
    """Adding a server picks a program to run here, so it is local-only."""
    from starlette.requests import Request
    from kova.pairing import require_local
    client, cfg, app = api

    def walk(routes):  # include_router is lazy: descend into included routers
        for route in routes:
            inner = getattr(route, "original_router", None)
            yield from walk(inner.routes) if inner is not None else [route]

    route = next(r for r in walk(app.routes) if getattr(r, "path", "") == "/mcp" and "POST" in r.methods)
    assert require_local in [dependency.call for dependency in route.dependant.dependencies]
    with pytest.raises(Exception, match="computer Kova runs on"):
        require_local(Request({"type": "http", "kova_lan": True}))


def test_cli_add_list_test_remove(api):
    client, cfg, app = api
    runner = CliRunner()
    empty = runner.invoke(cli.app, ["mcp", "list"])
    assert empty.exit_code == 0 and "No MCP servers yet" in empty.output
    added = runner.invoke(cli.app, ["mcp", "add", "fake", "--env", "FAKE_MCP_TOKEN=abc", "--", sys.executable, FAKE])
    assert added.exit_code == 0, added.output
    assert "Added fake" in added.output and "fake__echo" in added.output
    assert load_config(cfg.home).mcp[0].env == {"FAKE_MCP_TOKEN": "abc"}
    listed = runner.invoke(cli.app, ["mcp", "list"])
    assert listed.exit_code == 0 and "connected" in listed.output and f"{len(TOOLS)}/{len(TOOLS)}" in listed.output
    tested = runner.invoke(cli.app, ["mcp", "test", "fake"])
    assert tested.exit_code == 0 and "fake is working (fake-mcp 1.0)" in tested.output
    neither = runner.invoke(cli.app, ["mcp", "add", "x"])
    assert neither.exit_code == 1 and "either a command" in neither.output
    bad_env = runner.invoke(cli.app, ["mcp", "add", "x", "--env", "oops", "--", "y"])
    assert bad_env.exit_code == 1 and "NAME=VALUE" in bad_env.output
    broken = runner.invoke(cli.app, ["mcp", "add", "broken", "--", sys.executable, "-c", "raise SystemExit(4)"])
    assert broken.exit_code == 0 and "not connected" in broken.output
    failing = runner.invoke(cli.app, ["mcp", "test", "broken"])
    assert failing.exit_code == 1 and "exit code 4" in failing.output
    removed = runner.invoke(cli.app, ["mcp", "remove", "fake"])
    assert removed.exit_code == 0 and "Removed fake" in removed.output
    missing = runner.invoke(cli.app, ["mcp", "remove", "fake"])
    assert missing.exit_code == 1 and "No MCP server" in missing.output


def _wait_for(events, kind):
    deadline = time.monotonic() + 15
    while not any(event["type"] == kind for event in events):
        assert time.monotonic() < deadline, f"the turn never sent {kind}"
        time.sleep(.01)
    return next(event for event in events if event["type"] == kind)


def test_turn_asks_before_an_mcp_tool_and_runs_it(tmp_path):
    scripts = [tool("fake__echo", {"text": "from the model"}), text("Done once"),
               tool("fake__echo", {"text": "second"}, "call_2"), text("Done twice")]
    with real_server(tmp_path, scripts) as (client, cfg, fake, app):
        client.mcp_add(stdio_body())
        chat = client.create_chat()
        events = []
        thread = threading.Thread(target=lambda: events.extend(client.stream_message(chat["id"], "echo")),
                                  daemon=True)
        thread.start()
        approval = _wait_for(events, "approval")
        assert approval["tool"] == "fake__echo" and "from the model" in approval["summary"]
        client.answer_approval(chat["id"], approval["id"], "allow")
        thread.join(15)
        result = next(e for e in events if e["type"] == "tool_result")
        assert result["ok"] and result["output"] == "from the model"
        assert "fake__echo" in {t["function"]["name"] for t in fake.requests[0]["tools"]}
        # Always allow this tool: the next call runs without a card.
        client._request("PATCH", "/mcp/fake/tools/echo", json={"always_allow": True})
        second = list(client.stream_message(chat["id"], "again"))
        assert not any(e["type"] == "approval" for e in second)
        assert next(e for e in second if e["type"] == "tool_result")["output"] == "second"


def test_server_connects_configured_mcp_servers_at_startup(tmp_path):
    cfg = load_config(tmp_path / "home")
    cfg.mcp = [stdio()]
    save_config(cfg)
    app = create_app(load_config(tmp_path / "home"), transport=FakeModel([]).transport)
    with TestClient(app):
        deadline = time.monotonic() + 15
        while "fake__echo" not in app.state.agent.tools:
            assert time.monotonic() < deadline, "MCP tools never joined the registry"
            time.sleep(.05)
        assert app.state.mcp.status()[0]["state"] == "connected"
    # Shutdown stops every server it started.
    assert app.state.mcp._clients == {}
