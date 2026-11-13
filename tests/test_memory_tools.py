"""Model-facing memory tools: add/patch/remove/list and budget errors."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from kova.config import load_config
from kova.memory import Memory, MemoryBudgetError, MemoryExistsError, note_chars
from kova.store import Store
from kova.tools import ToolContext, build_tools


@pytest.fixture
def ctx(tmp_path):
    config = load_config(tmp_path)
    config.files_path = tmp_path / 'files'
    now = datetime(2026, 11, 13, 10, tzinfo=timezone(timedelta(hours=1)))
    memory = Memory(tmp_path / 'memory', budget=config.memory_budget)
    return ToolContext(config, Store(tmp_path / 'db'), memory, lambda: now)


def test_add_list_patch_remove_roundtrip(ctx):
    tools = build_tools(ctx)
    assert tools['memory_add'].run({'title': 'Dog', 'body': 'Rex is a good boy',
                                    'tags': ['pets']}) == 'Saved memory: Dog'

    listed = json.loads(tools['memory_list'].run({}))
    assert listed['used'] == note_chars('Dog', 'Rex is a good boy', ['pets'])
    assert listed['budget'] == 8000
    assert listed['percent'] == 0
    assert listed['total'] == 1
    assert listed['notes'][0] == {
        'slug': 'dog', 'title': 'Dog', 'body': 'Rex is a good boy', 'tags': ['pets'],
        'updated': listed['notes'][0]['updated'],
        # A model-facing write is recorded as volunteered by the model, never as
        # something the user was proven to have said.
        'origin': 'model', 'source': '', 'created': listed['notes'][0]['created'],
        'chars': note_chars('Dog', 'Rex is a good boy', ['pets']),
    }

    assert tools['memory_patch'].run(
        {'slug': 'dog', 'old_text': 'good boy', 'new_text': 'very good boy'}
    ) == 'Patched memory: Dog'
    assert json.loads(tools['memory_search'].run({'query': 'Rex'}))[0]['body'] == \
        'Rex is a very good boy'

    assert tools['memory_remove'].run({'slug': 'dog'}) == 'Removed memory: dog'
    assert json.loads(tools['memory_list'].run({}))['notes'] == []


def test_add_refuses_to_replace_an_existing_note(ctx):
    tools = build_tools(ctx)
    tools['memory_add'].run({'title': 'Dog', 'body': 'Rex is a good boy'})
    with pytest.raises(MemoryExistsError, match='already exists'):
        tools['memory_add'].run({'title': 'Dog', 'body': 'Something else'})
    assert ctx.memory.get('dog').body == 'Rex is a good boy'


def test_legacy_memory_save_name_keeps_working_but_does_not_overwrite(ctx):
    tools = build_tools(ctx)
    tools['memory_save'].run({'title': 'Dog', 'body': 'Rex'})
    with pytest.raises(MemoryExistsError):
        tools['memory_save'].run({'title': 'Dog', 'body': 'Fido'})
    assert ctx.memory.get('dog').body == 'Rex'
    # Already-existing users keep the tool name in their prompts.
    assert 'memory_save' in tools and 'memory_add' in tools


def test_patch_errors_are_clear(ctx):
    tools = build_tools(ctx)
    tools['memory_add'].run({'title': 'Dog', 'body': 'Rex likes Rex'})
    with pytest.raises(Exception, match='matches 2 times'):
        tools['memory_patch'].run({'slug': 'dog', 'old_text': 'Rex', 'new_text': 'Fido'})
    with pytest.raises(Exception, match='No note named'):
        tools['memory_patch'].run({'slug': 'cat', 'old_text': 'x', 'new_text': 'y'})
    with pytest.raises(Exception, match='No note named'):
        tools['memory_remove'].run({'slug': 'cat'})


def test_over_budget_add_reports_the_overflow(ctx):
    ctx.memory.budget = 20
    tools = build_tools(ctx)
    tools['memory_add'].run({'title': 'Dog', 'body': 'Rex'})  # 6 chars
    with pytest.raises(MemoryBudgetError) as caught:
        tools['memory_add'].run({'title': 'Cat', 'body': 'x' * 30})
    assert caught.value.overflow == 6 + note_chars('Cat', 'x' * 30, []) - 20
    assert 'over budget by' in str(caught.value)
    # Nothing was truncated or partially written.
    assert [n.slug for n in ctx.memory.list()] == ['dog']


def test_memory_list_caps_entries_but_still_counts_them(ctx):
    from kova.tools import memory as memory_tools

    for index in range(memory_tools.MAX_LISTED_NOTES + 3):
        ctx.memory.add(f'Note {index}', 'body')
    listed = json.loads(build_tools(ctx)['memory_list'].run({}))
    assert len(listed['notes']) == memory_tools.MAX_LISTED_NOTES
    assert listed['total'] == memory_tools.MAX_LISTED_NOTES + 3
    assert listed['notes'][0]['slug'] == 'note-0'
