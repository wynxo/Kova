"""Plugins: installable bundles of skills, slash commands, MCP servers and rules."""

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

import kova.cli as cli
import kova.plugins as plugins
from kova.permissions import Rule
from kova.skills import Skills


MANIFEST = '''\
name = "demo"
version = "1.2.3"
description = "A demo plugin."
author = "Example Author"
homepage = "https://example.com/demo"
min_kova = "0.1.0"
skills = ["skills"]

[[mcp]]
name = "files"
command = "npx"
args = ["-y", "@modelcontextprotocol/server-files"]
env = { FOO = "secret-value" }

[[mcp]]
name = "remote"
url = "https://example.com/mcp"
enabled = false

[[commands]]
name = "demo"
description = "Run the demo"
prompt = "Show me the demo."

[[permissions]]
action = "allow"
tool = "run_command"
pattern = "demo *"
'''

SKILL = "---\nname: Demo skill\ndescription: Do the demo thing.\n---\nBody of the demo.\n"


def write_plugin(root: Path, manifest: str = MANIFEST,
                 skills: dict[str, str] | None = None) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / "kova-plugin.toml").write_text(manifest)
    skills = skills if skills is not None else {"demo": SKILL}
    skills_dir = root / "skills"
    skills_dir.mkdir(parents=True, exist_ok=True)
    for name, body in skills.items():
        (skills_dir / f"{name}.md").write_text(body)
    return root


# ---------------------------------------------------------------------------
# Manifest parsing and validation
# ---------------------------------------------------------------------------

def test_parse_manifest_reads_every_field(tmp_path):
    plugin = plugins.parse_manifest(write_plugin(tmp_path / "src"))

    assert plugin.name == "demo"
    assert plugin.version == "1.2.3"
    assert plugin.description == "A demo plugin."
    assert plugin.author == "Example Author"
    assert plugin.homepage == "https://example.com/demo"
    assert plugin.min_kova == "0.1.0"

    assert [s.name for s in plugin.mcp] == ["files", "remote"]
    files = plugin.mcp[0]
    assert files.command == "npx"
    assert files.args == ["-y", "@modelcontextprotocol/server-files"]
    assert files.env == {"FOO": "secret-value"}
    assert files.enabled is True
    remote = plugin.mcp[1]
    assert remote.url == "https://example.com/mcp" and remote.enabled is False

    assert plugin.skills == ["skills"]
    assert [(c.name, c.description, c.prompt) for c in plugin.commands] == \
        [("demo", "Run the demo", "Show me the demo.")]
    assert plugin.permissions == [{"action": "allow", "tool": "run_command",
                                   "pattern": "demo *", "message": None}]


@pytest.mark.parametrize("name", ["Demo", "demo/../x", "..", "-demo", "demo space"])
def test_parse_rejects_a_bad_name(tmp_path, name):
    with pytest.raises(plugins.PluginError):
        plugins.parse_manifest(write_plugin(tmp_path / "src",
                                            MANIFEST.replace('name = "demo"', f'name = "{name}"')))


def test_parse_requires_a_version(tmp_path):
    manifest = MANIFEST.replace('version = "1.2.3"\n', "")
    with pytest.raises(plugins.PluginError, match="version"):
        plugins.parse_manifest(write_plugin(tmp_path / "src", manifest))


def test_parse_requires_mcp_command_or_url(tmp_path):
    manifest = MANIFEST.replace('command = "npx"\nargs = ["-y", "@modelcontextprotocol/server-files"]\nenv = { FOO = "secret-value" }',
                                "")
    with pytest.raises(plugins.PluginError, match="command.*url"):
        plugins.parse_manifest(write_plugin(tmp_path / "src", manifest))


@pytest.mark.parametrize("action", ["run", "ALLOW"])
def test_parse_rejects_an_unknown_permission_action(tmp_path, action):
    manifest = MANIFEST.replace('action = "allow"', f'action = "{action}"')
    with pytest.raises(plugins.PluginError, match="action"):
        plugins.parse_manifest(write_plugin(tmp_path / "src", manifest))


def test_parse_rejects_a_skill_path_that_escapes(tmp_path):
    manifest = MANIFEST.replace('skills = ["skills"]', 'skills = ["../outside"]')
    with pytest.raises(plugins.PluginError, match="skill"):
        plugins.parse_manifest(write_plugin(tmp_path / "src", manifest))


def test_parse_rejects_a_plugin_that_needs_a_newer_kova(tmp_path):
    manifest = MANIFEST.replace('min_kova = "0.1.0"', 'min_kova = "99.0.0"')
    with pytest.raises(plugins.PluginError, match="requires Kova"):
        plugins.parse_manifest(write_plugin(tmp_path / "src", manifest))


def test_parse_rejects_a_command_with_spaces(tmp_path):
    manifest = MANIFEST.replace('name = "demo"\ndescription = "Run the demo"',
                                'name = "demo cmd"\ndescription = "Run the demo"')
    with pytest.raises(plugins.PluginError, match="slash command"):
        plugins.parse_manifest(write_plugin(tmp_path / "src", manifest))


def test_parse_manifest_from_a_directory_without_one_fails(tmp_path):
    with pytest.raises(plugins.PluginError, match="No kova-plugin.toml"):
        plugins.parse_manifest(tmp_path)


# ---------------------------------------------------------------------------
# Install, list, enable, disable, remove, update
# ---------------------------------------------------------------------------

def test_install_copies_into_kova_home_and_records_state(tmp_path):
    plugin = plugins.install(write_plugin(tmp_path / "src"), tmp_path, confirm=lambda p: True)

    assert plugin.name == "demo" and plugin.enabled is True
    target = tmp_path / "plugins" / "demo"
    assert (target / "kova-plugin.toml").is_file()
    assert (target / "skills" / "demo.md").is_file()
    state = json.loads((target / ".kova-plugin.json").read_text())
    assert state["enabled"] is True and state["version"] == "1.2.3"


def test_install_shows_confirm_and_can_cancel(tmp_path):
    seen = []

    def confirm(plugin):
        seen.append(plugin.name)
        return False

    assert plugins.install(write_plugin(tmp_path / "src"), tmp_path, confirm=confirm) is None
    assert seen == ["demo"]
    assert not (tmp_path / "plugins" / "demo").exists()


def test_install_refuses_a_duplicate_name(tmp_path):
    plugins.install(write_plugin(tmp_path / "src"), tmp_path, confirm=lambda p: True)
    with pytest.raises(plugins.PluginError, match="already installed"):
        plugins.install(write_plugin(tmp_path / "src2"), tmp_path, confirm=lambda p: True)


def test_install_from_a_git_url(tmp_path, monkeypatch):
    source = write_plugin(tmp_path / "src")

    def fake_clone(url, destination):
        (destination / "kova-plugin.toml").write_text(
            (source / "kova-plugin.toml").read_text())
        (destination / "skills").mkdir()
        (destination / "skills" / "demo.md").write_text(SKILL)
        return destination

    monkeypatch.setattr(plugins, "_clone", fake_clone)
    plugin = plugins.install("https://example.com/demo.git", tmp_path, confirm=lambda p: True)

    assert plugin.name == "demo"
    assert (tmp_path / "plugins" / "demo" / "kova-plugin.toml").is_file()


def test_installed_lists_sorted_and_reads_enabled_state(tmp_path):
    plugins.install(write_plugin(tmp_path / "a", MANIFEST.replace('name = "demo"', 'name = "zeta"')),
                    tmp_path, confirm=lambda p: True)
    plugins.install(write_plugin(tmp_path / "b", MANIFEST.replace('name = "demo"', 'name = "alpha"')),
                    tmp_path, confirm=lambda p: True)
    plugins.disable("zeta", tmp_path)

    installed = plugins.installed(tmp_path)
    assert [p.name for p in installed] == ["alpha", "zeta"]
    assert [p.enabled for p in installed] == [True, False]
    assert [p.name for p in plugins.enabled_plugins(tmp_path)] == ["alpha"]


def test_disable_removes_and_enable_restores_permission_rules(tmp_path):
    plugins.install(write_plugin(tmp_path / "src"), tmp_path, confirm=lambda p: True)
    rules_file = tmp_path / "permission-rules.json"
    assert "plugin:demo" in rules_file.read_text()

    plugins.disable("demo", tmp_path)
    assert "plugin:demo" not in rules_file.read_text()
    assert not plugins.enabled_plugins(tmp_path)

    plugins.enable("demo", tmp_path)
    assert "plugin:demo" in rules_file.read_text()
    assert [p.name for p in plugins.enabled_plugins(tmp_path)] == ["demo"]


def test_remove_deletes_the_folder_and_its_rules(tmp_path):
    plugins.install(write_plugin(tmp_path / "src"), tmp_path, confirm=lambda p: True)
    assert (tmp_path / "permission-rules.json").exists()

    plugins.remove("demo", tmp_path)

    assert not (tmp_path / "plugins" / "demo").exists()
    assert not plugins.installed(tmp_path)
    assert "plugin:demo" not in (tmp_path / "permission-rules.json").read_text()


def test_update_reinstalls_from_the_recorded_source(tmp_path):
    source = write_plugin(tmp_path / "src")
    plugins.install(source, tmp_path, confirm=lambda p: True)

    # Bump the source's version, then update.
    (source / "kova-plugin.toml").write_text(MANIFEST.replace("1.2.3", "1.3.0"))
    updated = plugins.update("demo", tmp_path, confirm=lambda p: True)

    assert updated.version == "1.3.0"
    assert plugins.installed(tmp_path)[0].version == "1.3.0"


def test_update_preserves_disabled_state(tmp_path):
    source = write_plugin(tmp_path / "src")
    plugins.install(source, tmp_path, confirm=lambda p: True)
    plugins.disable("demo", tmp_path)

    (source / "kova-plugin.toml").write_text(MANIFEST.replace("1.2.3", "1.3.0"))
    plugins.update("demo", tmp_path, confirm=lambda p: True)

    assert plugins.installed(tmp_path)[0].enabled is False
    assert "plugin:demo" not in (tmp_path / "permission-rules.json").read_text()


def test_update_refuses_a_source_that_changed_identity(tmp_path):
    source = write_plugin(tmp_path / "src")
    plugins.install(source, tmp_path, confirm=lambda p: True)
    (source / "kova-plugin.toml").write_text(MANIFEST.replace('name = "demo"', 'name = "other"'))
    with pytest.raises(plugins.PluginError, match="refusing"):
        plugins.update("demo", tmp_path, confirm=lambda p: True)


def test_missing_plugin_name_is_an_error(tmp_path):
    with pytest.raises(plugins.PluginError, match="No plugin named"):
        plugins.disable("nope", tmp_path)


# ---------------------------------------------------------------------------
# What a plugin contributes
# ---------------------------------------------------------------------------

def test_plugin_mcp_servers_returns_only_enabled_servers_of_enabled_plugins(tmp_path):
    plugins.install(write_plugin(tmp_path / "src"), tmp_path, confirm=lambda p: True)

    servers = plugins.plugin_mcp_servers(tmp_path)
    assert servers == [{"name": "files", "command": "npx",
                        "args": ["-y", "@modelcontextprotocol/server-files"],
                        "env": {"FOO": "secret-value"}, "url": None, "enabled": True}]

    plugins.disable("demo", tmp_path)
    assert plugins.plugin_mcp_servers(tmp_path) == []


def test_plugin_commands_are_keyed_by_name_and_only_when_enabled(tmp_path):
    plugins.install(write_plugin(tmp_path / "src"), tmp_path, confirm=lambda p: True)

    commands = plugins.plugin_commands(tmp_path)
    assert commands["demo"].prompt == "Show me the demo."

    plugins.disable("demo", tmp_path)
    assert plugins.plugin_commands(tmp_path) == {}


def test_plugin_skills_load_through_the_skills_loader_read_only(tmp_path):
    plugins.install(write_plugin(tmp_path / "src"), tmp_path, confirm=lambda p: True)

    skills = Skills.for_home(tmp_path, extra_roots=plugins.plugin_skill_roots(tmp_path))
    listed = skills.list()
    assert [skill.name for skill in listed] == ["Demo skill"]
    assert skills.get("Demo skill").body == "Body of the demo."

    # Read-only: the model cannot remove or overwrite a plugin's skill.
    assert skills.remove("Demo skill") is False
    assert (tmp_path / "plugins" / "demo" / "skills" / "demo.md").exists()


def test_plugin_skills_do_not_leak_into_learned_writes(tmp_path):
    plugins.install(write_plugin(tmp_path / "src"), tmp_path, confirm=lambda p: True)
    skills = Skills.for_home(tmp_path, extra_roots=plugins.plugin_skill_roots(tmp_path))

    skills.save("My own skill", "A skill Kova learned.", "steps")
    assert (tmp_path / "skills" / "Kova" / "my-own-skill.md").is_file()
    # The plugin skill stays untouched and separate from Kova's own writes.
    assert (tmp_path / "plugins" / "demo" / "skills" / "demo.md").read_text() == SKILL


def test_describe_shows_exactly_what_the_plugin_adds_without_secrets(tmp_path):
    plugin = plugins.parse_manifest(write_plugin(tmp_path / "src"))
    info = plugins.describe(plugin)

    assert info["name"] == "demo" and info["version"] == "1.2.3"
    assert [s["name"] for s in info["skills"]] == ["Demo skill"]
    assert [c["name"] for c in info["commands"]] == ["demo"]
    assert info["permissions"] == [{"action": "allow", "tool": "run_command",
                                    "pattern": "demo *", "message": None}]
    mcp = info["mcp"][0]
    assert mcp["command"] == "npx"
    assert mcp["env_keys"] == ["FOO"]
    # Env values may hold secrets: they never appear in the description.
    assert "secret-value" not in json.dumps(info)


def test_plugin_rules_are_tagged_with_the_plugin(tmp_path):
    plugin = plugins.parse_manifest(write_plugin(tmp_path / "src"))
    rules = plugins.plugin_rules(plugin)

    assert rules == [Rule("allow", "run_command", "demo *", scope="always", source="plugin:demo")]


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

@pytest.fixture
def cli_runner(tmp_path, monkeypatch):
    monkeypatch.setenv("KOVA_HOME", str(tmp_path))
    return CliRunner()


def test_cli_install_yes_lists_and_remove(cli_runner, tmp_path):
    source = write_plugin(tmp_path / "src")

    result = cli_runner.invoke(cli.app, ["plugin", "install", str(source), "--yes"])
    assert result.exit_code == 0, result.output
    assert "Installed demo 1.2.3." in result.output
    assert "files" in result.output and "npx" in result.output  # the summary is shown

    result = cli_runner.invoke(cli.app, ["plugin", "list"])
    assert result.exit_code == 0, result.output
    assert "demo" in result.output and "enabled" in result.output

    result = cli_runner.invoke(cli.app, ["plugin", "remove", "demo"])
    assert result.exit_code == 0, result.output
    assert not (tmp_path / "plugins" / "demo").exists()


def test_cli_install_asks_before_installing(cli_runner, tmp_path):
    source = write_plugin(tmp_path / "src")

    result = cli_runner.invoke(cli.app, ["plugin", "install", str(source)], input="n\n")
    assert result.exit_code == 0, result.output
    assert "Cancelled." in result.output
    assert not (tmp_path / "plugins" / "demo").exists()

    result = cli_runner.invoke(cli.app, ["plugin", "install", str(source)], input="y\n")
    assert result.exit_code == 0, result.output
    assert "Installed demo 1.2.3." in result.output


def test_cli_disable_and_enable(cli_runner, tmp_path):
    source = write_plugin(tmp_path / "src")
    cli_runner.invoke(cli.app, ["plugin", "install", str(source), "--yes"])

    result = cli_runner.invoke(cli.app, ["plugin", "disable", "demo"])
    assert result.exit_code == 0, result.output
    assert plugins.installed(tmp_path)[0].enabled is False

    result = cli_runner.invoke(cli.app, ["plugin", "enable", "demo"])
    assert result.exit_code == 0, result.output
    assert plugins.installed(tmp_path)[0].enabled is True


def test_cli_list_empty_state(cli_runner):
    result = cli_runner.invoke(cli.app, ["plugin", "list"])
    assert result.exit_code == 0, result.output
    assert "No plugins installed." in result.output


# ---------------------------------------------------------------------------
# Integration with the agent prompt and the chat's slash commands
# ---------------------------------------------------------------------------

def test_plugin_skill_appears_in_the_agent_prompt(tmp_path):
    import asyncio
    from datetime import datetime, timezone

    from kova.agent import Agent
    from kova.config import load_config
    from kova.memory import Memory
    from kova.store import Store
    from kova.tools import ToolContext, build_tools
    from tests.fake_model import FakeModel, text

    plugins.install(write_plugin(tmp_path / "src"), tmp_path, confirm=lambda p: True)
    cfg = load_config(tmp_path)
    store = Store(tmp_path / "kova.db", default_cwd=str(tmp_path))
    memory = Memory(tmp_path / "memory")
    now = lambda: datetime(2026, 11, 12, 10, 0, tzinfo=timezone.utc)
    fake = FakeModel([text("ok")])
    agent = Agent(cfg, store, memory, build_tools(ToolContext(cfg, store, memory, now)),
                  transport=fake.transport, now=now)

    chat = store.create_chat(title="t", cwd=str(tmp_path))

    async def run():
        async for _event in agent.run_turn(chat["id"], "hello"):
            pass

    asyncio.run(run())
    prompt = fake.requests[0]["messages"][0]["content"]
    assert "Demo skill: Do the demo thing." in prompt
    assert "Body of the demo." not in prompt


def test_chat_help_and_completion_include_plugin_commands(tmp_path, monkeypatch):
    monkeypatch.setenv("KOVA_HOME", str(tmp_path))
    plugins.install(write_plugin(tmp_path / "src"), tmp_path, confirm=lambda p: True)

    import kova.ui as ui

    commands = ui._all_commands()
    assert "/demo" in commands
    assert commands["/demo"] == "Run the demo"
