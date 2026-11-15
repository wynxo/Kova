"""Plugin MCP servers: prefixed names, load/unload on enable/disable, refusing remove."""

import json
import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

import kova.cli as cli
from kova import mcp, plugins
from kova.permissions import Rule

FAKE = str(Path(__file__).with_name("fake_mcp_server.py"))


def make_plugin(root: Path, name: str = "My Plugin", server: str = "fake") -> Path:
    root.mkdir(parents=True)
    # JSON strings are valid TOML basic strings, so paths and names are escaped safely.
    (root / "kova-plugin.toml").write_text(
        f'name = {json.dumps(name.lower().replace(" ", "-"))}\nversion = "1.0.0"\n\n'
        f'[[mcp]]\nname = {json.dumps(server)}\ncommand = {json.dumps(sys.executable)}\n'
        f'args = [{json.dumps(FAKE)}]\n')
    return root


@pytest.fixture
def home(tmp_path):
    return tmp_path / "home"


@pytest.fixture
def load(home):
    managers = []

    def reload():
        manager = mcp.McpManager(home)
        manager.reload(plugins.plugin_mcp_entries(home), timeout=15)
        managers.append(manager)
        return manager

    yield reload
    for manager in managers:
        manager.close()


def test_enabled_plugin_tool_appears_with_prefixed_name_and_disable_removes_it(tmp_path, home, load):
    plugins.install(make_plugin(tmp_path / "src", "My Plugin", "Fake Srv"), home, confirm=lambda p: True)
    names = {tool.name for tool in load().tools()}
    assert "my-plugin_fake_srv__echo" in names
    assert all(mcp_name.replace("_", "").replace("-", "").isalnum() for mcp_name in names)

    plugins.disable("my-plugin", home)
    assert load().tools() == []


def test_disable_drops_always_allow_rules_for_the_plugins_tools(tmp_path, home):
    plugins.install(make_plugin(tmp_path / "src", "demo", "fake"), home, confirm=lambda p: True)
    rules = home / "permission-rules.json"
    existing = json.loads(rules.read_text()) if rules.exists() else []
    mine = Rule("allow", "demo_fake__echo", "*", scope="always")
    other = Rule("allow", "user__echo", "*", scope="always")
    from dataclasses import asdict
    rules.write_text(json.dumps(existing + [asdict(mine), asdict(other)]))

    plugins.disable("demo", home)
    tools = [entry["tool"] for entry in json.loads(rules.read_text())]
    assert "demo_fake__echo" not in tools and "user__echo" in tools


def test_mcp_remove_of_a_plugin_server_is_refused_with_a_hint(monkeypatch):
    class Client:
        def mcp_remove(self, name):
            return {"ok": True, "plugin": "demo"}

    monkeypatch.setattr(cli, "get_client", lambda: Client())
    result = CliRunner().invoke(cli.app, ["mcp", "remove", "demo_fake"])
    assert result.exit_code != 0
    assert "kova plugin disable demo" in result.output
