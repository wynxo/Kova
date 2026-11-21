"""Choosing the active provider and model: one picker, shared by CLI and chat.

The rows come from the server's ``/models`` list, so these tests cover the
resolution rules, the saved choice, the piped fallback and the help text that
makes the two command groups self-explanatory.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

import kova.cli as cli
import kova.model_picker as model_picker
from kova.client import KovaClient
from kova.config import ModelEntry, ProviderEntry, ensure_token, load_config, save_config
from kova.server import create_app
from tests.fake_model import FakeModel

OPENAI = 'https://api.openai.com/v1'
LOCAL = 'http://127.0.0.1:11434/v1'


def configured_home(tmp_path, monkeypatch):
    """A home with two providers, three models and a saved active model."""
    monkeypatch.setenv('KOVA_HOME', str(tmp_path))
    cfg = load_config()
    cfg.providers[:] = [
        ProviderEntry('openai', 'OpenAI', OPENAI, kind='openai', adapter='openai', api_key='test-key'),
        ProviderEntry('local', 'Local server', LOCAL, kind='local'),
    ]
    cfg.models[:] = [
        ModelEntry('openai/gpt-4o', OPENAI, 'gpt-4o', provider='openai'),
        ModelEntry('openai/gpt-4o-mini', OPENAI, 'gpt-4o-mini', provider='openai'),
        ModelEntry('local/qwen3:8b', LOCAL, 'qwen3:8b', provider='local'),
    ]
    cfg.active = 'local/qwen3:8b'
    save_config(cfg)
    return cfg


@pytest.fixture
def runner(tmp_path, monkeypatch):
    """A CliRunner wired to a server over the configured home."""
    cfg = configured_home(tmp_path, monkeypatch)
    testapp = create_app(cfg, transport=FakeModel([]).transport)
    with TestClient(testapp) as http:
        client = KovaClient('http://127.0.0.1:7700', ensure_token(cfg.home), http=http)
        monkeypatch.setattr(cli, 'get_client', lambda: client)
        yield CliRunner(), cfg


# ------------------------------------------------------------------- the rows
def models():
    return [
        {'name': 'openai/gpt-4o', 'model': 'gpt-4o', 'provider': 'openai', 'provider_name': 'OpenAI'},
        {'name': 'local/qwen3:8b', 'model': 'qwen3:8b', 'provider': 'local', 'provider_name': 'Local server'},
        {'name': 'openai/gpt-4o-mini', 'model': 'gpt-4o-mini', 'provider': 'openai', 'provider_name': 'OpenAI'},
    ]


def test_rows_sort_by_provider_and_mark_the_current_choice():
    rows = model_picker.choices(models())
    assert [row.name for row in rows] == ['local/qwen3:8b', 'openai/gpt-4o', 'openai/gpt-4o-mini']
    assert model_picker.label(rows[0], active='local/qwen3:8b').startswith('● Local server')
    assert '(current)' in model_picker.label(rows[0], active='local/qwen3:8b')
    assert model_picker.label(rows[1], active='local/qwen3:8b').startswith('  OpenAI')
    assert model_picker.current(rows, 'openai/gpt-4o') == 1
    assert model_picker.current(rows, 'gone') == 0


def test_a_row_is_named_by_entry_provider_model_or_id():
    rows = model_picker.choices(models())
    assert model_picker.resolve(rows, 'openai/gpt-4o').name == 'openai/gpt-4o'
    assert model_picker.resolve(rows, 'OpenAI/gpt-4o-mini').name == 'openai/gpt-4o-mini'
    assert model_picker.resolve(rows, 'qwen3:8b').name == 'local/qwen3:8b'
    # A provider with one model is enough to name it; with two it is ambiguous.
    assert model_picker.resolve(rows, 'local').name == 'local/qwen3:8b'
    assert model_picker.resolve(rows, 'openai') is None
    assert model_picker.resolve(rows, 'nothing') is None


def test_the_first_run_placeholder_is_not_a_choice():
    """An entry with no model id is not callable, so the picker never offers it."""
    assert model_picker.choices([{'name': 'local', 'model': '', 'provider': 'local',
                                  'provider_name': 'Local server'}]) == []


def test_the_piped_listing_is_numbered_and_names_every_provider():
    rows = model_picker.choices(models())
    listing = model_picker.numbered(rows, active='local/qwen3:8b')
    assert listing[0].startswith(' 1') and 'qwen3:8b' in listing[0]
    assert 'OpenAI' in listing[1] and 'gpt-4o' in listing[1]
    assert all(len(line) <= 80 for line in listing)


def test_similar_names_are_short_references():
    rows = model_picker.choices(models())
    assert 'openai/gpt-4o' in model_picker.similar(rows, 'gpt-4o')
    assert model_picker.similar(rows, 'nothing') == []


# ----------------------------------------------------------------- the command
def test_model_use_takes_provider_slash_model(runner):
    cli_runner, _ = runner
    result = cli_runner.invoke(cli.app, ['model', 'use', 'openai/gpt-4o'])
    assert result.exit_code == 0, result.output
    assert 'Now using gpt-4o on OpenAI.' in result.output
    assert load_config().active == 'openai/gpt-4o'


def test_model_use_takes_a_provider_with_one_model(runner):
    cli_runner, cfg = runner
    result = cli_runner.invoke(cli.app, ['model', 'use', 'local'])
    assert result.exit_code == 0, result.output
    assert 'Now using qwen3:8b on Local server.' in result.output
    assert load_config().active == 'local/qwen3:8b'
    assert cfg.active == 'local/qwen3:8b'


def test_model_use_opens_the_picker_when_interactive(runner, monkeypatch):
    cli_runner, _ = runner
    picked = {'rows': None, 'active': None}

    def fake_pick(rows, *, active='', **_):
        picked.update(rows=rows, active=active)
        return next(row for row in rows if row.name == 'openai/gpt-4o-mini')

    monkeypatch.setattr(model_picker, 'pick', fake_pick)
    result = cli_runner.invoke(cli.app, ['model', 'use'])
    assert result.exit_code == 0, result.output
    assert picked['active'] == 'local/qwen3:8b'  # the picker opens on the saved choice
    assert {row.name for row in picked['rows']} == {'openai/gpt-4o', 'openai/gpt-4o-mini', 'local/qwen3:8b'}
    assert 'Now using gpt-4o-mini on OpenAI.' in result.output
    assert load_config().active == 'openai/gpt-4o-mini'


def test_model_use_without_a_name_in_a_pipe_lists_the_models(runner):
    cli_runner, _ = runner
    result = cli_runner.invoke(cli.app, ['model', 'use'], input='')
    assert result.exit_code == 1
    assert 'OpenAI' in result.output and 'gpt-4o' in result.output
    assert 'Local server' in result.output and 'qwen3:8b' in result.output
    assert 'kova model use openai/gpt-4o' in result.output  # the hint names the way out


def test_model_use_unknown_name_suggests_the_configured_ones(runner):
    cli_runner, _ = runner
    result = cli_runner.invoke(cli.app, ['model', 'use', 'gpt-4o-nope'])
    assert result.exit_code == 1
    assert 'No model named "gpt-4o-nope"' in result.output
    assert 'kova model list' in result.output

    ambiguous = cli_runner.invoke(cli.app, ['model', 'use', 'openai'])
    assert ambiguous.exit_code == 1
    assert 'openai/gpt-4o' in ambiguous.output and 'openai/gpt-4o-mini' in ambiguous.output


# -------------------------------------------------------------- the list view
def test_providers_list_fits_a_narrow_terminal(runner):
    cli_runner, cfg = runner
    cfg.providers.append(ProviderEntry(
        'gemini', 'Google Gemini', 'https://generativelanguage.googleapis.com/v1beta/openai',
        kind='gemini', api_key='test-key'))
    save_config(cfg)
    result = cli_runner.invoke(cli.app, ['providers', 'list'])
    assert result.exit_code == 0, result.output
    # The address is shown in full: no ellipsis where the old table cut it off.
    assert 'https://generativelanguage.googleapis.com/v1beta/openai' in result.output
    assert '…' not in result.output
    assert all(len(line) <= 80 for line in result.output.splitlines())
    assert 'Google Gemini' in result.output and 'Connected' in result.output
    assert 'No credentials' in result.output  # the local provider has no key
    assert 'kova model use' in result.output


# ------------------------------------------------------------------- the help
@pytest.mark.parametrize('args, expected', [
    (['providers', 'list'], 'One short block per provider'),
    (['providers', 'add'], 'asks for the key only when the provider needs'),
    (['providers', 'remove'], 'models selected from it'),
    (['providers', 'login'], 're-authenticate'),
    (['providers', 'logout'], 'forget its stored credential'),
    (['model', 'list'], 'mark the active one'),
    (['model', 'add'], 'Add a model server by hand'),
    (['model', 'use'], 'picker of every connected provider'),
    (['model', 'remove-local'], 'weights and connection'),
])
def test_every_provider_and_model_subcommand_explains_itself(tmp_path, monkeypatch, args, expected):
    monkeypatch.setenv('KOVA_HOME', str(tmp_path))
    result = CliRunner().invoke(cli.app, [*args, '--help'])
    assert result.exit_code == 0, result.output
    assert expected in ' '.join(result.output.split())


# ------------------------------------------------------------------- in chat
def test_chat_model_and_provider_commands_switch_and_list(runner):
    cli_runner, _ = runner
    result = cli_runner.invoke(cli.app, ['--new'],
                               input='/model\n/provider openai/gpt-4o\n/provider local\n/model\n/exit\n')
    assert result.exit_code == 0, result.output
    assert 'Type /model <provider>/<model> to switch.' in result.output
    assert 'Now using gpt-4o on OpenAI. This and every new chat use it.' in result.output
    assert 'Now using qwen3:8b on Local server.' in result.output
    assert load_config().active == 'local/qwen3:8b'


def test_chat_model_picker_switches_the_current_and_future_chats(runner, monkeypatch):
    cli_runner, _ = runner

    def fake_pick(rows, *, active='', **_):
        assert active == 'local/qwen3:8b'
        return next(row for row in rows if row.name == 'openai/gpt-4o')

    monkeypatch.setattr(model_picker, 'pick', fake_pick)
    result = cli_runner.invoke(cli.app, ['--new'], input='/model\n/exit\n')
    assert result.exit_code == 0, result.output
    assert 'Now using gpt-4o on OpenAI.' in result.output
    assert load_config().active == 'openai/gpt-4o'


def test_chat_provider_command_helps_with_an_unknown_name(runner):
    cli_runner, _ = runner
    result = cli_runner.invoke(cli.app, ['--new'], input='/provider nope\n/exit\n')
    assert result.exit_code == 0, result.output
    assert 'No model named "nope". Type /model to see them.' in result.output
