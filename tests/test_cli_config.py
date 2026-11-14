"""``kova config``: path, redacted show, and schema-validated set."""

import json
from dataclasses import fields
from pathlib import Path

import pytest
from typer.testing import CliRunner

import kova.cli as cli
from kova.config import (
    CONFIG_SCHEMA,
    CONFIG_SECTION_KEYS,
    TOP_LEVEL_CONFIG_KEYS,
    Config,
    load_config,
    save_config,
)

SECRET_KEY = "not-a-real-api-key-0001"
SECRET_HEADER = "not-a-real-header-secret-0002"
SECRET_TOKEN = "not-a-real-oauth-token-0003"
SECRET_REFRESH = "not-a-real-refresh-token-0004"
SECRETS = (SECRET_KEY, SECRET_HEADER, SECRET_TOKEN, SECRET_REFRESH)

runner = CliRunner()


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("KOVA_HOME", str(tmp_path))
    load_config()
    return tmp_path


def invoke(*args):
    return runner.invoke(cli.app, list(args))


def test_config_path_does_not_start_the_server(home, monkeypatch):
    monkeypatch.setattr(cli, "get_client", lambda: pytest.fail("started the server"))
    result = invoke("config", "path")
    assert result.exit_code == 0, result.output
    assert result.stdout.strip() == str(home / "config.toml")


def test_config_show_json_is_complete_and_redacts_credentials(home):
    cfg = load_config()
    cfg.providers[0].api_key = SECRET_KEY
    cfg.providers[0].headers = {"Authorization": f"Bearer {SECRET_HEADER}", "X-Trace": SECRET_HEADER}
    cfg.providers[0].auth = {"access_token": SECRET_TOKEN, "refresh_token": SECRET_REFRESH}
    save_config(cfg)

    result = invoke("config", "show", "--json")
    assert result.exit_code == 0, result.output
    data = json.loads(result.stdout)
    assert data["path"] == str(home / "config.toml")
    assert data["schema_version"] == CONFIG_SCHEMA
    assert data["memory"]["budget"] == 8000
    assert data["tui"]["theme"] == "dark"
    assert data["persona"]["name"] == "Kova"
    assert data["models"][0]["vision"] is False
    provider = data["providers"][0]
    assert provider["api_key"] == cli.REDACTED
    assert provider["headers"] == {"Authorization": cli.REDACTED, "X-Trace": cli.REDACTED}
    assert provider["auth"] == {"access_token": cli.REDACTED, "refresh_token": cli.REDACTED}
    for secret in SECRETS:
        assert secret not in result.stdout


def test_config_show_human_output_never_dumps_credentials(home):
    cfg = load_config()
    cfg.providers[0].api_key = SECRET_KEY
    cfg.providers[0].auth = {"access_token": SECRET_TOKEN}
    save_config(cfg)

    result = invoke("config", "show")
    assert result.exit_code == 0, result.output
    assert "memory.budget" in result.output
    assert "providers.0" in result.output
    assert "key set" in result.output
    assert "OAuth set" in result.output
    for secret in SECRETS:
        assert secret not in result.output


def test_redaction_covers_unknown_credential_shaped_keys():
    value = {
        "api_key": SECRET_KEY,
        "nested": {"refresh_token": SECRET_REFRESH, "safe": 1},
        "items": [{"password": SECRET_TOKEN}, "visible"],
    }
    redacted = cli._redact_extra(value)
    assert redacted["api_key"] == cli.REDACTED
    assert redacted["nested"] == {"refresh_token": cli.REDACTED, "safe": 1}
    assert redacted["items"] == [{"password": cli.REDACTED}, "visible"]
    assert all(secret not in json.dumps(redacted) for secret in SECRETS)


@pytest.mark.parametrize(
    "key,value,attr,expected",
    [
        ("port", "7788", "port", 7788),
        ("compact_threshold", "0.9", "compact_threshold", 0.9),
        ("memory.budget", "0", "memory_budget", 0),
        ("memory.index_lines", "50", "memory_index_lines", 50),
        ("memory.review", "on", "memory_review", True),
        ("memory.review_every", "3", "memory_review_every", 3),
        ("memory.consolidate", "true", "memory_consolidate", True),
        ("memory.consolidate_hour", "23", "memory_consolidate_hour", 23),
        ("memory.consolidate_min_confidence", "0.5", "memory_consolidate_min_confidence", 0.5),
        ("memory.consolidate_max_loss", "1", "memory_consolidate_max_loss", 1.0),
        ("memory.write_subdir", "Kova", "memory_write_subdir", "Kova"),
        ("tui.theme", "light", "tui_theme", "light"),
        ("persona.name", "Nova", "persona_name", "Nova"),
        ("persona.tone", "playful", "persona_tone", "playful"),
        ("persona.configured", "yes", "persona_configured", True),
        ("bind", "lan", "bind", "lan"),
        ("active", "local", "active", "local"),
    ],
)
def test_config_set_valid_values_round_trip(home, key, value, attr, expected):
    result = invoke("config", "set", key, value)
    assert result.exit_code == 0, result.output
    assert getattr(load_config(), attr) == expected


def test_config_set_paths_are_expanded_and_resolved(home):
    result = invoke("config", "set", "files_path", str(home / "files"))
    assert result.exit_code == 0, result.output
    result = invoke("config", "set", "memory.path", str(home / "notes"))
    assert result.exit_code == 0, result.output
    cfg = load_config()
    assert cfg.files_path == (home / "files").resolve()
    assert cfg.memory_path == (home / "notes").resolve()


def test_config_set_empty_write_subdir_clears_it(home):
    cfg = load_config()
    cfg.memory_write_subdir = "Kova"
    save_config(cfg)
    result = invoke("config", "set", "memory.write_subdir", "")
    assert result.exit_code == 0, result.output
    assert load_config().memory_write_subdir is None


def test_config_set_tui_key_and_refuses_a_duplicate(home):
    result = invoke("config", "set", "tui.keys.palette", "ctrl-g")
    assert result.exit_code == 0, result.output
    assert load_config().tui_keys == {"palette": "ctrl-g"}

    result = invoke("config", "set", "tui.keys.palette", "ctrl-t")
    assert result.exit_code == 1
    assert "unique" in " ".join(result.output.split())
    assert load_config().tui_keys == {"palette": "ctrl-g"}

    result = invoke("config", "set", "tui.keys.palette", "ctrl-p")
    assert result.exit_code == 0, result.output
    assert load_config().tui_keys == {}


@pytest.mark.parametrize(
    "key,value,needle",
    [
        ("compact_threshold", "1.5", "between 0.1 and 0.99"),
        ("compact_threshold", "nope", "must be a number"),
        ("port", "0", "between 1 and 65535"),
        ("port", "65536", "between 1 and 65535"),
        ("port", "http", "must be a whole number"),
        ("memory.budget", "-1", "at least 0"),
        ("memory.review", "maybe", "on or off"),
        ("memory.review_every", "0", "at least 1"),
        ("memory.consolidate_hour", "24", "between 0 and 23"),
        ("memory.consolidate_min_confidence", "1.1", "between 0 and 1"),
        ("memory.consolidate_max_loss", "-0.1", "between 0 and 1"),
        ("tui.theme", "neon", "one of: dark, light"),
        ("persona.tone", "snarky", "one of"),
        ("persona.name", "", "1-40"),
        ("active", "missing", "not a configured model"),
        ("bind", "internet", "one of: loopback, lan"),
        ("memory.write_subdir", "../outside", "without separators"),
        ("tui.keys.nope", "ctrl-g", "unknown TUI action"),
    ],
)
def test_config_set_invalid_values_are_refused_without_saving(home, key, value, needle):
    before = (home / "config.toml").read_text()
    args = ["config", "set", key]
    if value.startswith("-"):
        args.append("--")
    result = runner.invoke(cli.app, [*args, value])
    assert result.exit_code == 1, result.output
    assert needle in " ".join(result.output.split())
    assert (home / "config.toml").read_text() == before


def test_config_set_preserves_comments_and_unknown_keys(home):
    path = home / "config.toml"
    path.write_text(
        "# hand-written settings\n"
        "port = 7700  # keep this\n"
        "compact_treshold = 0.9  # typo\n"
        "\n[tui]\n"
        'theme = "dark"\n'
        "unknown_thing = 1\n"
    )
    result = invoke("config", "set", "port", "7788")
    assert result.exit_code == 0, result.output
    text = path.read_text()
    assert "# hand-written settings" in text
    assert "port = 7788  # keep this" in text
    assert "compact_treshold = 0.9  # typo" in text
    assert "unknown_thing = 1" in text
    assert load_config().port == 7788


def test_config_set_unknown_key_suggests_the_closest_real_key(home):
    result = invoke("config", "set", "compact_treshold", "0.9")
    assert result.exit_code == 1
    assert "Unknown config key" in result.output
    assert "compact_threshold" in " ".join(result.output.split())


def test_config_set_unknown_nested_key_uses_the_shared_suggestion_helper(home):
    result = invoke("config", "set", "memory.budgett", "9000")
    assert result.exit_code == 1
    output = " ".join(result.output.split())
    assert "Unknown config key" in output
    assert "memory.budget" in output


def test_config_set_models_points_at_the_model_commands(home):
    result = invoke("config", "set", "models.0.model", "other")
    assert result.exit_code == 1
    output = " ".join(result.output.split())
    assert "Unknown config key" in output
    assert "kova model" in output


def test_config_docs_cover_every_config_field():
    docs = (Path(__file__).resolve().parents[1] / "docs" / "CONFIG.md").read_text()
    for item in fields(Config):
        assert f"`{item.name}`" in docs, item.name


def test_config_cli_keys_are_a_subset_of_the_shared_schema():
    for key in cli._CONFIG_FIELDS:
        if "." in key:
            section, leaf = key.split(".", 1)
            assert section in CONFIG_SECTION_KEYS, key
            assert leaf in CONFIG_SECTION_KEYS[section], key
        else:
            assert key in TOP_LEVEL_CONFIG_KEYS, key


def test_help_links_the_config_documentation():
    result = invoke("--help")
    assert result.exit_code == 0, result.output
    assert "docs/CONFIG.md" in result.output
    group = invoke("config", "--help")
    assert group.exit_code == 0, group.output
    assert "docs/CONFIG.md" in group.output
