from kova.config import ModelEntry, ensure_token, kova_home, load_config, save_config


def test_default_config_created(tmp_path):
    cfg = load_config(tmp_path)
    assert (tmp_path / "config.toml").exists()
    m = cfg.active_model()
    assert (m.name, m.base_url, m.model, m.context) == (
        "tiel",
        "http://192.168.178.29:8080/v1",
        "cyber-tiel-coder-35b-a3b-mtp-ud-iq3_xxs",
        131072,
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
