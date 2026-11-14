import os
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
    assert cfg.memory_budget == 8000


def test_roundtrip(tmp_path):
    cfg = load_config(tmp_path)
    cfg.models.append(ModelEntry("ollama", "http://localhost:11434/v1", "qwen3"))
    cfg.active = "ollama"
    cfg.memory_write_subdir = "Kova"
    cfg.memory_budget = 5000
    save_config(cfg)
    again = load_config(tmp_path)
    assert again.active_model().model == "qwen3"
    assert again.memory_write_subdir == "Kova"
    assert again.memory_budget == 5000


def test_kova_home_env(monkeypatch, tmp_path):
    monkeypatch.setenv("KOVA_HOME", str(tmp_path))
    assert kova_home() == tmp_path


def test_token_stable_and_private(tmp_path):
    t = ensure_token(tmp_path)
    assert ensure_token(tmp_path) == t and len(t) >= 32
    if os.name != "nt":
        assert (tmp_path / "token").stat().st_mode & 0o777 == 0o600


def test_config_file_private(tmp_path):
    load_config(tmp_path)
    if os.name != "nt":
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


def test_persona_config_round_trip_and_prompt(tmp_path):
    from datetime import datetime
    from kova.agent import build_system_prompt
    from kova.persona import Persona

    cfg = load_config(tmp_path)
    cfg.persona_name, cfg.persona_tone = 'Nova', 'concise'
    save_config(cfg)
    cfg = load_config(tmp_path)
    assert (cfg.persona_name, cfg.persona_tone) == ('Nova', 'concise')
    prompt = build_system_prompt(datetime.now(), [], [], persona=Persona(cfg.persona_name, cfg.persona_tone))
    assert 'You are Nova' in prompt and 'concise' in prompt.lower()
    assert 'You are Kova' not in prompt


def test_persona_names_are_trimmed_and_validated():
    from kova.persona import Persona, TONES
    assert Persona('  Nova ').name == 'Nova'
    for bad in ('', '   ', 'x' * 41, 'bad\x02name'):
        with pytest.raises(ValueError):
            Persona(bad)
    with pytest.raises(ValueError):
        Persona('Nova', 'snarky')
    assert all(tone in TONES for tone in ('warm', 'concise', 'professional', 'playful'))


def test_empty_memory_path_falls_back_to_the_default(tmp_path):
    """An empty [memory] path must not root notes at the process directory (#90)."""
    (tmp_path / "config.toml").write_text('[memory]\npath = ""\n')
    cfg = load_config(tmp_path)
    assert cfg.memory_path == tmp_path / "memory"
