"""Installable plugin bundles: skills, slash commands, MCP servers and rules.

A plugin is one folder described by a ``kova-plugin.toml`` manifest. Kova
installs it into ``KOVA_HOME/plugins/<name>/`` and, from there, loads the bits
it declares:

- ``skills`` — read-only SKILL.md-style Markdown, loaded through the same
  ``kova.skills`` loader as Kova's own skills, but never written or deleted by
  the model (only ``learned`` skills under the skills root are).
- ``commands`` — slash commands for the terminal chat. Each is a canned prompt
  sent as the user's turn; a plugin command runs nothing by itself.
  - ``mcp`` — MCP server definitions. Kova loads the servers of enabled plugins
    through :func:`plugin_mcp_entries`, giving each a name prefixed with the
    plugin so it cannot collide with a user's own server.
- ``permissions`` — default permission rules installed into
  ``permission-rules.json``, tagged with their plugin, and removed on
  disable/remove. They go through the exact same permission policy and sandbox
  as built-in tools.

Nothing a plugin declares is trusted or run without the user confirming: the
CLI shows exactly what a plugin adds and asks before installing or updating.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import tomllib
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable

from kova import __version__
from kova.config import McpServerEntry, kova_home
from kova.permissions import Rule
from kova.skills import Skill, load_skills

#: The manifest file that makes a folder a plugin.
MANIFEST = "kova-plugin.toml"
#: Per-plugin state (enabled flag, source and version) inside its install dir.
STATE_FILE = ".kova-plugin.json"
#: The permission rules file plugins' default rules are merged into.
RULES_FILE = "permission-rules.json"

#: A plugin name is a filesystem-safe slug: lowercase word, dots/dashes allowed.
_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*$")
#: A slash-command token: a word, no whitespace or path separators.
_COMMAND_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_-]*$")


class PluginError(ValueError):
    """A manifest, install or state change the loader refuses; safe to show."""


@dataclass
class McpServer:
    """One MCP server a plugin declares; mirrors the config shape of #135."""
    name: str
    command: str | None = None
    args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    url: str | None = None
    enabled: bool = True

    def as_dict(self) -> dict:
        return {"name": self.name, "command": self.command, "args": list(self.args),
                "env": dict(self.env), "url": self.url, "enabled": self.enabled}


@dataclass
class PluginCommand:
    """A slash command: a canned prompt sent as the user's turn."""
    name: str
    description: str = ""
    prompt: str = ""


@dataclass
class Plugin:
    """One parsed manifest, plus where it lives and whether it is enabled."""
    name: str
    version: str
    description: str = ""
    author: str = ""
    homepage: str = ""
    min_kova: str | None = None
    mcp: list[McpServer] = field(default_factory=list)
    skills: list[str] = field(default_factory=list)
    commands: list[PluginCommand] = field(default_factory=list)
    permissions: list[dict] = field(default_factory=list)
    root: Path = field(default_factory=Path)
    enabled: bool = True
    source: str | None = None


# ---------------------------------------------------------------------------
# Manifest parsing and validation
# ---------------------------------------------------------------------------

def _text(data: dict, key: str, default: str = "") -> str:
    value = data.get(key, default)
    if value is None:
        return default
    if not isinstance(value, str):
        raise PluginError(f"{MANIFEST}: {key!r} must be a string")
    return value.strip()


def _require_text(data: dict, key: str) -> str:
    value = _text(data, key)
    if not value:
        raise PluginError(f"{MANIFEST}: {key!r} is required")
    return value


def _string_list(data: dict, key: str) -> list:
    value = data.get(key)
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise PluginError(f"{MANIFEST}: {key!r} must be a list of strings")
    return value


def _table_list(data: dict, key: str) -> list:
    value = data.get(key)
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        raise PluginError(f"{MANIFEST}: {key!r} must be a list of tables")
    return value


def _validate_name(name: str) -> None:
    if not _NAME_RE.match(name) or ".." in name:
        raise PluginError(
            f"Invalid plugin name {name!r}: use a lowercase slug (letters, digits, "
            "dots, dashes, underscores).")


def _validate_skill_path(entry: str) -> None:
    if not entry or entry.startswith("/") or entry.startswith(".") or ".." in entry.split("/"):
        raise PluginError(
            f"Invalid skill path {entry!r}: a relative path inside the plugin, no '..'.")


def _parse_mcp(item: dict) -> McpServer:
    name = _require_text(item, "name")
    command = item.get("command")
    if command is not None and not isinstance(command, str):
        raise PluginError(f"{MANIFEST}: mcp[{name!r}].command must be a string")
    url = item.get("url")
    if url is not None and not isinstance(url, str):
        raise PluginError(f"{MANIFEST}: mcp[{name!r}].url must be a string")
    if not (command and command.strip()) and not (url and url.strip()):
        raise PluginError(f"{MANIFEST}: mcp[{name!r}] needs a 'command' or a 'url'.")
    args = item.get("args", [])
    if not isinstance(args, list) or not all(isinstance(arg, str) for arg in args):
        raise PluginError(f"{MANIFEST}: mcp[{name!r}].args must be a list of strings")
    env = item.get("env", {})
    if not isinstance(env, dict) or not all(
            isinstance(k, str) and isinstance(v, str) for k, v in env.items()):
        raise PluginError(f"{MANIFEST}: mcp[{name!r}].env must be a table of strings")
    enabled = item.get("enabled", True)
    if not isinstance(enabled, bool):
        raise PluginError(f"{MANIFEST}: mcp[{name!r}].enabled must be true or false")
    return McpServer(name=name.strip(), command=(command or "").strip() or None,
                     args=list(args), env=dict(env), url=(url or "").strip() or None,
                     enabled=enabled)


def _parse_command(item: dict) -> PluginCommand:
    name = _require_text(item, "name")
    if not _COMMAND_RE.match(name):
        raise PluginError(
            f"Invalid slash command {name!r}: a single word of letters, digits, '-' or '_'.")
    prompt = _require_text(item, "prompt")
    return PluginCommand(name=name, description=_text(item, "description"), prompt=prompt)


def _parse_permission(item: dict) -> dict:
    action = _require_text(item, "action")
    if action not in {"allow", "ask", "deny"}:
        raise PluginError(f"{MANIFEST}: permission action must be allow, ask or deny, not {action!r}")
    scope = _text(item, "scope", "always") or "always"
    if scope != "always":
        raise PluginError(f"{MANIFEST}: plugin permissions are always-on; scope must be 'always'")
    message = item.get("message")
    if message is not None and not isinstance(message, str):
        raise PluginError(f"{MANIFEST}: permission message must be a string")
    return {"action": action, "tool": _text(item, "tool", "*") or "*",
            "pattern": _text(item, "pattern", "*") or "*",
            "message": message or None}


def _version_key(version: str) -> tuple[int, ...]:
    """Numeric parts of a version, so '0.1.0' < '0.2' < '1.0'. Pre-release dropped."""
    core = re.split(r"[-+]", (version or "").strip())[0]
    parts = []
    for part in core.split("."):
        match = re.match(r"\d+", part.strip())
        parts.append(int(match.group()) if match else 0)
    return tuple(parts)


def _check_min_kova(min_kova: str | None) -> None:
    if not min_kova:
        return
    if _version_key(min_kova) > _version_key(__version__):
        raise PluginError(
            f"Plugin requires Kova >= {min_kova}, but this is Kova {__version__}.")


def _build_plugin(data: dict, root: Path) -> Plugin:
    name = _require_text(data, "name")
    _validate_name(name)
    version = _require_text(data, "version")
    if any(ch in version for ch in "/\\\0 ") or not version:
        raise PluginError(f"{MANIFEST}: invalid version {version!r}")
    min_kova = _text(data, "min_kova") or None
    _check_min_kova(min_kova)
    skills = _string_list(data, "skills")
    for entry in skills:
        _validate_skill_path(entry)
    plugin = Plugin(
        name=name,
        version=version,
        description=_text(data, "description"),
        author=_text(data, "author"),
        homepage=_text(data, "homepage"),
        min_kova=min_kova,
        mcp=[_parse_mcp(item) for item in _table_list(data, "mcp")],
        skills=list(skills),
        commands=[_parse_command(item) for item in _table_list(data, "commands")],
        permissions=[_parse_permission(item) for item in _table_list(data, "permissions")],
        root=root,
    )
    return plugin


def parse_manifest(path: Path | str) -> Plugin:
    """Parse and validate a plugin from a directory or its manifest file."""
    path = Path(path)
    if path.is_dir():
        root, manifest = path, path / MANIFEST
    else:
        root, manifest = path.parent, path
    if not manifest.is_file():
        raise PluginError(f"No {MANIFEST} found in {root}")
    try:
        data = tomllib.loads(manifest.read_text())
    except (tomllib.TOMLDecodeError, OSError) as exc:
        raise PluginError(f"Could not read {MANIFEST}: {exc}") from exc
    if not isinstance(data, dict):
        raise PluginError(f"{MANIFEST} must be a TOML table")
    return _build_plugin(data, root)


def find_manifest(root: Path) -> Path | None:
    """The manifest under ``root``, at the root or one directory level down."""
    direct = root / MANIFEST
    if direct.is_file():
        return direct
    matches = [p for p in root.rglob(MANIFEST) if ".git" not in p.parts and p.is_file()]
    return matches[0] if matches else None


def _manifest_dir(root: Path) -> Path:
    found = find_manifest(root)
    if found is None:
        raise PluginError(f"No {MANIFEST} found under {root}")
    return found.parent


# ---------------------------------------------------------------------------
# Plugin state and the install directory
# ---------------------------------------------------------------------------

def plugins_dir(home: Path | None = None) -> Path:
    return (Path(home) if home else kova_home()) / "plugins"


def _state_path(plugin_dir: Path) -> Path:
    return plugin_dir / STATE_FILE


def _read_state(plugin_dir: Path) -> dict:
    path = _state_path(plugin_dir)
    if path.is_file():
        try:
            data = json.loads(path.read_text())
            if isinstance(data, dict):
                return data
        except (OSError, json.JSONDecodeError):
            pass
    return {}


def _atomic_json(path: Path, data: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".kova-", suffix=".json", dir=path.parent)
    try:
        with open(fd, "w") as stream:
            json.dump(data, stream, indent=2)
            stream.write("\n")
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        if Path(temporary).exists():
            Path(temporary).unlink()


def _write_state(plugin_dir: Path, *, enabled: bool, source: str | None, version: str) -> None:
    _atomic_json(_state_path(plugin_dir), {
        "enabled": bool(enabled), "source": source, "version": version,
        "installed_at": datetime.now().astimezone().isoformat(),
    })


def installed(home: Path | None = None) -> list[Plugin]:
    """Every installed plugin (enabled and disabled), sorted by name."""
    root = plugins_dir(home)
    plugins = []
    if not root.is_dir():
        return plugins
    for child in sorted(root.iterdir()):
        if not child.is_dir():
            continue
        try:
            plugin = parse_manifest(child)
        except PluginError:
            continue  # a leftover or unrelated folder is not a plugin
        state = _read_state(child)
        plugin.enabled = bool(state.get("enabled", True))
        plugin.source = state.get("source") or plugin.source
        plugins.append(plugin)
    return plugins


def enabled_plugins(home: Path | None = None) -> list[Plugin]:
    return [plugin for plugin in installed(home) if plugin.enabled]


def _find(home: Path, name: str) -> tuple[Plugin, Path]:
    for plugin in installed(home):
        if plugin.name == name:
            return plugin, plugin.root
    raise PluginError(f"No plugin named {name!r} is installed. Run: kova plugin list")


# ---------------------------------------------------------------------------
# What a plugin contributes, for the loader and the confirm screen
# ---------------------------------------------------------------------------

def plugin_rules(plugin: Plugin) -> list[Rule]:
    """The default permission rules a plugin declares, tagged with its name."""
    rules = []
    for entry in plugin.permissions:
        rules.append(Rule(
            entry["action"], entry.get("tool", "*"), entry.get("pattern", "*"),
            scope="always", message=entry.get("message"),
            source=f"plugin:{plugin.name}",
        ))
    return rules


def _resolved_skill_paths(plugin: Plugin) -> list[Path]:
    """The files/directories a plugin's ``skills`` entries point to, in place."""
    base = plugin.root.resolve()
    paths = []
    for entry in plugin.skills:
        path = (plugin.root / entry).resolve()
        try:
            path.relative_to(base)
        except ValueError:
            continue  # escapes the plugin root; never load it
        if path.is_file() or path.is_dir():
            paths.append(path)
    return paths


def plugin_skill_roots(home: Path | None = None) -> list[Path]:
    """Skill files/directories of every enabled plugin, for the skills loader."""
    roots = []
    for plugin in enabled_plugins(home):
        roots.extend(_resolved_skill_paths(plugin))
    return roots


def plugin_commands(home: Path | None = None) -> dict[str, PluginCommand]:
    """The slash commands of every enabled plugin, keyed by command name."""
    commands: dict[str, PluginCommand] = {}
    for plugin in enabled_plugins(home):
        for command in plugin.commands:
            commands[command.name] = command
    return commands


def plugin_mcp_servers(home: Path | None = None) -> list[dict]:
    """MCP servers of every enabled plugin, as ``{name, command, args, env, url, enabled}``.

    The MCP client (issue #135) consumes these; Kova itself does not start them.
    """
    servers = []
    for plugin in enabled_plugins(home):
        for server in plugin.mcp:
            if server.enabled:
                servers.append(server.as_dict())
    return servers


def _plugin_server_name(plugin_name: str, server_name: str) -> str:
    """A Kova server name for one of a plugin's MCP servers.

    Prefixed with the plugin so it can never collide with a user's own server,
    and cleaned to the characters a tool name allows (letters, digits, '-' and
    '_'), so the resulting ``<server>__<tool>`` is always a valid function
    name. Falls back to ``x`` when a side is empty.
    """
    def clean(value: str) -> str:
        value = re.sub(r"[^a-z0-9-]", "_", (value or "").lower())
        value = re.sub(r"_+", "_", value).strip("_")
        return value or "x"
    return f"{clean(plugin_name)}_{clean(server_name)}"


def plugin_mcp_entries(home: Path | None = None) -> list[McpServerEntry]:
    """MCP servers of every enabled plugin, as manager-ready ``McpServerEntry`` values.

    Each server's name is prefixed with its plugin (see
    :func:`_plugin_server_name`) so it cannot collide with a user's own
    ``[[mcp]]`` server, and the plugin's name rides on the ``plugin`` field so
    :func:`McpManager.status` and the CLI can mark it as coming from a plugin.
    Disabled servers are skipped, matching :func:`plugin_mcp_servers`.
    """
    entries: list[McpServerEntry] = []
    for plugin in enabled_plugins(home):
        for server in plugin.mcp:
            if not server.enabled:
                continue
            entries.append(McpServerEntry(
                name=_plugin_server_name(plugin.name, server.name),
                command=server.command, args=list(server.args),
                env=dict(server.env), url=server.url, headers={},
                enabled=server.enabled, disabled_tools=[], sandbox=False,
                plugin=plugin.name))
    return entries


def describe(plugin: Plugin) -> dict:
    """Exactly what a plugin adds, for the install-time confirmation screen.

    MCP ``env`` values may hold secrets, so only the keys are included.
    """
    skills = []
    for skill in load_skills(_resolved_skill_paths(plugin)):
        skills.append({"name": skill.name, "description": skill.description})
    return {
        "name": plugin.name,
        "version": plugin.version,
        "description": plugin.description,
        "author": plugin.author,
        "homepage": plugin.homepage,
        "min_kova": plugin.min_kova,
        "mcp": [{"name": s.name, "command": s.command, "args": list(s.args),
                 "env_keys": sorted(s.env), "url": s.url, "enabled": s.enabled}
                for s in plugin.mcp],
        "skills": skills,
        "commands": [{"name": c.name, "description": c.description, "prompt": c.prompt}
                     for c in plugin.commands],
        "permissions": [dict(entry) for entry in plugin.permissions],
    }


# ---------------------------------------------------------------------------
# Install, update, enable, disable, remove
# ---------------------------------------------------------------------------

def _looks_like_git(source: str) -> bool:
    return source.startswith(("http://", "https://", "git@", "ssh://", "git://"))


def _clone(url: str, destination: Path) -> Path:
    from kova.egress import check_external_process
    check_external_process()
    result = subprocess.run(
        ["git", "clone", "--depth", "1", "--", url, str(destination)],
        capture_output=True, text=True)
    if result.returncode != 0:
        raise PluginError(f"Could not clone {url}: {result.stderr.strip() or 'git clone failed'}")
    return destination


def _copy_tree(source: Path, target: Path) -> None:
    shutil.copytree(source, target, ignore=shutil.ignore_patterns(
        ".git", "__pycache__", ".venv", "*.pyc"))


def install(source: str | Path, home: Path | None = None, *,
            confirm: Callable[[Plugin], bool] | None = None,
            source_label: str | None = None) -> Plugin | None:
    """Install a plugin from a local path or a git URL; ``confirm`` gates it.

    ``confirm`` sees the parsed plugin before anything is written and may
    refuse; returning ``False`` (or raising) cancels the install. Never trust
    a plugin without showing the user exactly what it adds. ``source_label``
    replaces the recorded source (the catalog records ``catalog:<name>``).
    """
    home = Path(home) if home else kova_home()
    source = str(source)
    temporary: Path | None = None
    try:
        if _looks_like_git(source):
            temporary = Path(tempfile.mkdtemp(prefix="kova-plugin-"))
            manifest_dir = _manifest_dir(_clone(source, temporary))
            spec = source
        else:
            manifest_dir = _manifest_dir(_local_root(Path(source)))
            spec = str(manifest_dir.resolve())
        plugin = parse_manifest(manifest_dir)
        target = plugins_dir(home) / plugin.name
        if target.exists():
            raise PluginError(
                f"A plugin named {plugin.name!r} is already installed. "
                f"Run: kova plugin remove {plugin.name}")
        if confirm is not None and not confirm(plugin):
            return None
        _copy_tree(manifest_dir, target)
        _write_state(target, enabled=True, source=source_label or spec, version=plugin.version)
        _sync_rules(home, plugin.name, plugin_rules(plugin))
        return parse_manifest(target)
    finally:
        if temporary is not None:
            shutil.rmtree(temporary, ignore_errors=True)


def _local_root(path: Path) -> Path:
    path = path.expanduser()
    if path.is_file():
        path = path.parent
    if not path.is_dir():
        raise PluginError(f"No such directory: {path}")
    return path


def update(name: str, home: Path | None = None, *,
           confirm: Callable[[Plugin], bool] | None = None,
           from_dir: Path | None = None) -> Plugin | None:
    """Re-install a plugin from its recorded source, preserving enable state.

    ``from_dir`` replaces it from an already fetched folder instead (the
    catalog passes its checksum-verified download) and keeps the source.
    """
    home = Path(home) if home else kova_home()
    plugin, target = _find(home, name)
    if not plugin.source and from_dir is None:
        raise PluginError(f"Plugin {name!r} has no recorded source to update from.")
    enabled = plugin.enabled
    source = plugin.source
    temporary: Path | None = None
    try:
        if from_dir is not None:
            manifest_dir = _manifest_dir(Path(from_dir))
        elif source.startswith("catalog:"):
            raise PluginError(
                f"{name!r} came from the catalog. Update it with: kova catalog install {name} --upgrade")
        elif _looks_like_git(source):
            temporary = Path(tempfile.mkdtemp(prefix="kova-plugin-"))
            manifest_dir = _manifest_dir(_clone(source, temporary))
        else:
            manifest_dir = _manifest_dir(_local_root(Path(source)))
        updated = parse_manifest(manifest_dir)
        if updated.name != name:
            raise PluginError(
                f"The source for {name!r} is now a plugin named {updated.name!r}; "
                "refusing to replace it.")
        if confirm is not None and not confirm(updated):
            return None
        shutil.rmtree(target)
        _copy_tree(manifest_dir, target)
        _write_state(target, enabled=enabled, source=source, version=updated.version)
        _sync_rules(home, name, plugin_rules(updated) if enabled else [])
        return parse_manifest(target)
    finally:
        if temporary is not None:
            shutil.rmtree(temporary, ignore_errors=True)


def enable(name: str, home: Path | None = None) -> Plugin:
    home = Path(home) if home else kova_home()
    plugin, root = _find(home, name)
    _write_state(root, enabled=True, source=plugin.source, version=plugin.version)
    _sync_rules(home, name, plugin_rules(plugin))
    return parse_manifest(root)


def disable(name: str, home: Path | None = None) -> Path:
    home = Path(home) if home else kova_home()
    plugin, root = _find(home, name)
    _write_state(root, enabled=False, source=plugin.source, version=plugin.version)
    _sync_rules(home, name, [])  # disabling fully removes the plugin's rules
    _drop_tool_grants(home, plugin)
    return root


def remove(name: str, home: Path | None = None) -> str:
    """Delete a plugin and everything it added: folder, skills, commands, rules."""
    home = Path(home) if home else kova_home()
    plugin, root = _find(home, name)
    _sync_rules(home, name, [])
    _drop_tool_grants(home, plugin)
    shutil.rmtree(root)
    return name


def _drop_tool_grants(home: Path, plugin: Plugin) -> None:
    """Delete saved rules (e.g. always-allow) for the tools of a plugin's MCP servers."""
    path = Path(home) / RULES_FILE
    if not path.is_file():
        return
    try:
        loaded = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return
    if not isinstance(loaded, list):
        return
    prefixes = tuple(f"{_plugin_server_name(plugin.name, server.name)}__" for server in plugin.mcp)
    kept = [entry for entry in loaded
            if not (isinstance(entry, dict) and str(entry.get("tool", "")).startswith(prefixes))]
    if len(kept) != len(loaded):
        _atomic_json(path, kept)


def _sync_rules(home: Path, name: str, rules: list[Rule]) -> None:
    """Merge a plugin's default rules into permission-rules.json, by source tag."""
    path = Path(home) / RULES_FILE
    existing: list[dict] = []
    if path.is_file():
        try:
            loaded = json.loads(path.read_text())
            if isinstance(loaded, list):
                existing = [entry for entry in loaded if isinstance(entry, dict)]
        except (OSError, json.JSONDecodeError):
            existing = []
    source = f"plugin:{name}"
    kept = [entry for entry in existing if entry.get("source") != source]
    _atomic_json(path, kept + [asdict(rule) for rule in rules])


__all__ = [
    "MANIFEST", "McpServer", "Plugin", "PluginCommand", "PluginError",
    "describe", "disable", "enable", "enabled_plugins", "find_manifest", "install",
    "installed", "parse_manifest", "plugin_commands", "plugin_mcp_entries", "plugin_mcp_servers",
    "plugin_rules", "plugin_skill_roots", "plugins_dir", "remove", "update",
]
