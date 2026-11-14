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


def test_hand_written_comments_unknown_keys_and_typos_survive_save(tmp_path, capsys):
    path = tmp_path / "config.toml"
    path.write_text(
        "# my settings\n"
        "port = 7700  # keep this\n"
        "compact_treshold = 0.9      # typo\n"
        "\n[tui]\n"
        'theme = "light"\n'
        "unknown_thing = 1\n"
    )
    cfg = load_config(tmp_path)
    cfg.port = 7788
    save_config(cfg)

    text = path.read_text()
    assert "# my settings" in text
    assert "port = 7788  # keep this" in text
    assert "compact_treshold = 0.9      # typo" in text
    assert "unknown_thing = 1" in text
    # The real key is written next to the preserved typo, as before.
    assert "compact_threshold = 0.75" in text
    assert cfg._extra == {"compact_treshold": 0.9}

    first = capsys.readouterr().err
    assert first.count("unknown key 'compact_treshold'") == 1
    assert "did you mean 'compact_threshold'" in first

    # A later load in the same process says nothing new.
    load_config(tmp_path)
    assert "compact_treshold" not in capsys.readouterr().err


def test_unknown_key_diagnostics_never_print_values(tmp_path, capsys):
    path = tmp_path / "config.toml"
    path.write_text('portt = "placeholder-secret"\n')
    load_config(tmp_path)
    err = capsys.readouterr().err
    assert "portt" in err and "did you mean 'port'" in err
    assert "placeholder-secret" not in err


def test_nested_known_and_unknown_settings_round_trip(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text(
        "[memory]\n"
        "budget = 8000\n"
        "# keep this note\n"
        'mystery_setting = "keep"\n'
        "\n[tui]\n"
        'theme = "light"\n'
        'my_theme_name = "custom"\n'
        "\n[tui.keys]\n"
        'submit = "enter"  # keep this binding\n'
        "\n[persona]\n"
        'name = "Nova"\n'
        'nickname = "N"\n'
    )
    cfg = load_config(tmp_path)
    cfg.memory_budget = 4321
    cfg.tui_theme = "dark"
    cfg.persona_name = "Kova"
    save_config(cfg)

    text = path.read_text()
    assert "# keep this note" in text
    assert 'mystery_setting = "keep"' in text
    assert "budget = 4321" in text
    assert 'my_theme_name = "custom"' in text
    assert 'theme = "dark"' in text
    assert 'submit = "enter"  # keep this binding' in text
    assert 'nickname = "N"' in text

    again = load_config(tmp_path)
    assert again.memory_budget == 4321
    assert again.tui_theme == "dark"
    assert again.tui_keys["submit"] == "enter"
    assert again.persona_name == "Kova"


def test_provider_and_model_saves_keep_unknown_entry_keys_and_comments(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text(
        "[[providers]]\n"
        'id = "lab"\n'
        'name = "Lab"\n'
        'base_url = "http://127.0.0.1:8080/v1"\n'
        "# local provider metadata\n"
        'owner = "you"  # keep owner\n'
        "\n[[models]]\n"
        'name = "lab/model-a"\n'
        'provider = "lab"\n'
        'base_url = "http://127.0.0.1:8080/v1"\n'
        'model = "model-a"\n'
        "# local alias metadata\n"
        'display_name = "Model A"  # keep alias\n'
    )
    cfg = load_config(tmp_path)
    cfg.providers[0].name = "Lab Updated"
    cfg.providers[0].api_key = "placeholder-key"
    cfg.models[0].context = 65536
    save_config(cfg)

    text = path.read_text()
    assert 'name = "Lab Updated"' in text
    assert 'api_key = "placeholder-key"' in text
    assert "context = 65536" in text
    assert "# local provider metadata" in text
    assert 'owner = "you"  # keep owner' in text
    assert "# local alias metadata" in text
    assert 'display_name = "Model A"  # keep alias' in text

    again = load_config(tmp_path)
    assert again.providers[0].name == "Lab Updated"
    assert again.providers[0].api_key == "placeholder-key"
    assert again.models[0].context == 65536


def test_installing_a_provider_preserves_other_unknown_keys(tmp_path):
    from kova.config import ProviderEntry
    from kova.providers import install_provider

    path = tmp_path / "config.toml"
    path.write_text(
        "# keep the top of the file\n"
        'custom_accent = "kept"\n'
        "\n[[providers]]\n"
        'id = "lab"\n'
        'name = "Lab"\n'
        'base_url = "http://127.0.0.1:8080/v1"\n'
        'custom_provider_key = "kept"\n'
        "\n[[models]]\n"
        'name = "lab/model-a"\n'
        'provider = "lab"\n'
        'base_url = "http://127.0.0.1:8080/v1"\n'
        'model = "model-a"\n'
        'custom_model_key = "kept"\n'
    )
    cfg = load_config(tmp_path)
    install_provider(cfg, ProviderEntry("second", "Second", "http://127.0.0.1:8090/v1"),
                     ["model-b"])

    text = path.read_text()
    assert "# keep the top of the file" in text
    assert 'custom_accent = "kept"' in text
    assert 'custom_provider_key = "kept"' in text
    assert 'custom_model_key = "kept"' in text
    assert 'id = "second"' in text
    assert load_config(tmp_path).providers[-1].id == "second"


def test_optional_keys_are_removed_when_unset(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text('bind = "lan"\n\n[memory]\nwrite_subdir = "Kova"\n')
    cfg = load_config(tmp_path)
    assert cfg.bind == "lan" and cfg.memory_write_subdir == "Kova"
    cfg.bind = "loopback"
    cfg.memory_write_subdir = None
    save_config(cfg)
    text = path.read_text()
    assert "bind" not in text
    assert "write_subdir" not in text


def test_reordering_models_and_providers_is_saved_with_their_extras(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text(
        "[[models]]\n"
        'name = "a"\n'
        'provider = "p"\n'
        'base_url = "http://127.0.0.1:1/v1"\n'
        'model = "a"\n'
        'a_mystery = "keep-a"  # a note\n'
        "\n[[models]]\n"
        'name = "b"\n'
        'provider = "p"\n'
        'base_url = "http://127.0.0.1:1/v1"\n'
        'model = "b"\n'
        'b_mystery = "keep-b"  # b note\n'
        "\n[[providers]]\n"
        'id = "p"\n'
        'name = "P"\n'
        'base_url = "http://127.0.0.1:1/v1"\n'
        'p_mystery = "keep-p"\n'
        "\n[[providers]]\n"
        'id = "q"\n'
        'name = "Q"\n'
        'base_url = "http://127.0.0.1:2/v1"\n'
        'q_mystery = "keep-q"\n'
    )
    cfg = load_config(tmp_path)
    cfg.models.reverse()
    cfg.providers.reverse()
    save_config(cfg)

    again = load_config(tmp_path)
    assert [model.name for model in again.models] == ["b", "a"]
    assert [provider.id for provider in again.providers] == ["q", "p"]
    text = path.read_text()
    assert 'a_mystery = "keep-a"  # a note' in text
    assert 'b_mystery = "keep-b"  # b note' in text
    assert 'p_mystery = "keep-p"' in text
    assert 'q_mystery = "keep-q"' in text
    # The moved table kept its own extra, rather than donating it to a neighbour.
    assert text.index('b_mystery = "keep-b"') < text.index('a_mystery = "keep-a"')


def test_unknown_key_helper_reports_paths_and_suggestions_only():
    from kova.config import config_key_warnings, unknown_config_keys

    data = {
        "compact_treshold": "placeholder-secret",
        "memory": {"budgett": "placeholder-secret"},
        "models": [{"contex": "placeholder-secret"}],
    }
    assert unknown_config_keys(data) == [
        ("compact_treshold", "compact_threshold"),
        ("memory.budgett", "memory.budget"),
        ("models[0].contex", "models[0].context"),
    ]
    warnings = config_key_warnings(data)
    assert len(warnings) == 3
    assert all("placeholder-secret" not in line for line in warnings)
