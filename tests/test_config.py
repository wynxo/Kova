import pytest

from kova.config import ModelEntry, ensure_token, kova_home, load_config, save_config


@pytest.mark.real_default
def test_default_config_created(tmp_path):
    cfg = load_config(tmp_path)
    assert (tmp_path / "config.toml").exists()
    m = cfg.active_model()
    assert (m.name, m.base_url, m.model, m.context) == (
        "local",
        "http://127.0.0.1:11434/v1",
        "",
        32768,
    )
    assert cfg.port == 7700
    assert cfg.memory_path == tmp_path / "memory"
    assert cfg.memory_write_subdir is None


def test_roundtrip(tmp_path):
    cfg = load_config(tmp_path)
    cfg.models.append(ModelEntry("ollama", "http://localhost:11434/v1", "qwen3"))
    cfg.active = "ollama"
    cfg.memory_write_subdir = "Kova"
    save_config(cfg)
    again = load_config(tmp_path)
    assert again.active_model().model == "qwen3"
    assert again.memory_write_subdir == "Kova"


def test_kova_home_env(monkeypatch, tmp_path):
    monkeypatch.setenv("KOVA_HOME", str(tmp_path))
    assert kova_home() == tmp_path


def test_token_stable_and_private(tmp_path):
    t = ensure_token(tmp_path)
    assert ensure_token(tmp_path) == t and len(t) >= 32
    assert (tmp_path / "token").stat().st_mode & 0o777 == 0o600


def test_config_file_private(tmp_path):
    load_config(tmp_path)
    assert (tmp_path / "config.toml").stat().st_mode & 0o777 == 0o600


def test_provider_key_is_authoritative_over_hydrated_model_keys(tmp_path):
    cfg = load_config(tmp_path)
    cfg.providers[0].api_key = 'changed-on-provider'
    save_config(cfg)
    assert load_config(tmp_path).active_model().api_key == 'changed-on-provider'


def test_legacy_key_assignment_updates_all_models_sharing_provider(tmp_path):
    cfg = load_config(tmp_path)
    cfg.models.append(ModelEntry('second', cfg.models[0].base_url, 'another'))
    save_config(cfg)
    cfg.models[0].api_key = 'new-key'
    save_config(cfg)
    again = load_config(tmp_path)
    assert all(m.api_key == 'new-key' for m in again.models)
