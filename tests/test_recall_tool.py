"""The recall tool: FTS search over past chats, excluding the current one."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone

import pytest

from kova.config import load_config
from kova.memory import Memory
from kova.permissions import needs_approval
from kova.store import Store
from kova.tools import ToolContext, build_tools
from kova.tools.sessions import (
    MAX_RESULTS,
    MAX_SNIPPET_CHARS,
    current_chat,
    set_current_chat,
)


@pytest.fixture(autouse=True)
def _no_current_chat():
    set_current_chat(None)
    yield
    set_current_chat(None)


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / 'db')


@pytest.fixture
def tools(tmp_path, store):
    config = load_config(tmp_path)
    config.files_path = tmp_path / 'files'
    now = datetime(2026, 11, 13, 10, tzinfo=timezone(timedelta(hours=1)))
    return build_tools(ToolContext(config, store, Memory(tmp_path / 'memory'), lambda: now))


def _chat(store, text, title='Chat'):
    chat = store.create_chat(title)
    store.add_message(chat['id'], 'user', text)
    return chat


def test_recall_excludes_the_current_chat_by_default(store, tools):
    current = _chat(store, 'the needle is here too', 'Current')
    older = _chat(store, 'the needle was discussed before', 'Older')
    set_current_chat(current['id'])

    payload = json.loads(tools['recall'].run({'query': 'needle'}))
    assert [row['chat_id'] for row in payload['results']] == [older['id']]
    assert payload['count'] == 1
    assert payload['results'][0]['title'] == 'Older'
    assert 'needle' in payload['results'][0]['snippet']
    assert payload['results'][0]['role'] == 'user'
    assert payload['results'][0]['when']

    everything = json.loads(tools['recall'].run({'query': 'needle',
                                                 'include_current': True}))
    assert {row['chat_id'] for row in everything['results']} == {current['id'], older['id']}


def test_current_chat_is_isolated_per_async_task(tools):
    async def in_task(value):
        set_current_chat(value)
        await asyncio.sleep(0)
        return current_chat()

    async def run():
        return await asyncio.gather(in_task('first'), in_task('second'))

    assert asyncio.run(run()) == ['first', 'second']
    assert current_chat() is None


def test_recall_is_capped_and_snippets_are_sized(store, tools):
    long_text = 'needle ' + ('x' * 400)
    for index in range(8):
        _chat(store, f'{long_text} {index}', f'Chat {index}')

    default = json.loads(tools['recall'].run({'query': 'needle'}))
    assert default['count'] == 5  # default page size

    capped = json.loads(tools['recall'].run({'query': 'needle', 'limit': 500}))
    assert capped['count'] == min(8, MAX_RESULTS)
    assert all(len(row['snippet']) <= MAX_SNIPPET_CHARS for row in capped['results'])
    assert all(row['snippet'].endswith('…') for row in capped['results'])


def test_recall_bad_limit_and_empty_results(store, tools):
    _chat(store, 'apples', 'Apples')
    fallback = json.loads(tools['recall'].run({'query': 'apples', 'limit': 'lots'}))
    assert fallback['count'] == 1

    empty = json.loads(tools['recall'].run({'query': 'nothing-matches-this'}))
    assert empty['results'] == [] and empty['count'] == 0
    assert 'No matching past conversations' in empty['hint']


def test_search_chats_is_an_alias(store, tools):
    _chat(store, 'apples', 'Apples')
    assert (json.loads(tools['search_chats'].run({'query': 'apples'}))
            == json.loads(tools['recall'].run({'query': 'apples'})))


def test_recall_is_read_only(store, tools):
    _chat(store, 'apples', 'Apples')
    before = store.list_chats()
    tools['recall'].run({'query': 'apples'})
    assert store.list_chats() == before


@pytest.mark.parametrize('name', ['recall', 'search_chats'])
@pytest.mark.parametrize('permission', ['manual', 'auto', 'full'])
def test_recall_needs_no_approval(name, permission, tmp_path):
    assert not needs_approval(name, {'query': 'anything'}, permission,
                              tmp_path, tmp_path / 'files')


def test_store_excludes_before_the_result_cap(store):
    older = _chat(store, 'needle in older', 'Older')
    newer = _chat(store, 'needle in newer', 'Newer')
    assert [r['id'] for r in store.search_chats('needle', limit=1)] == [newer['id']]
    assert [r['id'] for r in store.search_chats('needle', limit=1,
                                                exclude_chat_id=newer['id'])] == [older['id']]
    assert store.search_chats('needle', exclude_chat_id=older['id'],
                              limit=5)[0]['id'] == newer['id']
