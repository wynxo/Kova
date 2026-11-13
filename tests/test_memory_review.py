"""Evidence gates, write-ahead undo, and extractive-only consolidation."""
import hashlib
import json

import pytest

from kova.memory import Memory, MemoryStoreError
from kova.store import Store


@pytest.fixture
def context(tmp_path):
    store = Store(tmp_path / 'db')
    chat = store.create_chat()
    store.add_message(chat['id'], 'user', 'My dog is called Rex.')
    store.add_message(chat['id'], 'assistant', 'You seem impatient; tool X is broken.')
    return store, Memory(tmp_path / 'memory'), chat['id']


def reviewer(context):
    from kova.review import MemoryReview
    store, memory, _ = context
    return MemoryReview(store, memory)


def test_review_is_opt_in_and_exact_user_evidence_only(context):
    service = reviewer(context)
    store, memory, chat_id = context
    snapshot = store.memory_evidence(chat_id)
    claim = {'message_id': snapshot[0]['id'], 'quote': 'My dog is called Rex.'}
    with pytest.raises(MemoryStoreError, match='opt'):
        service.learn(chat_id, snapshot, claim)
    store.memory_settings({'review_interval': 3})
    for invalid in [dict(claim, quote='My dog is called Fido.'),
                    {'message_id': snapshot[1]['id'], 'quote': 'You seem impatient; tool X is broken.'}]:
        with pytest.raises(MemoryStoreError, match='user'):
            service.learn(chat_id, snapshot, invalid)
    note, audit_id = service.learn(chat_id, snapshot, claim)
    assert memory.get(note.slug).body == claim['quote']
    assert note.provenance['source_chat_id'] == chat_id
    assert note.provenance['evidence'][0]['message_id'] == claim['message_id']
    assert store.memory_diary()[0]['state'] == 'applied'
    service.undo(audit_id)
    assert memory.get(note.slug) is None
    assert store.memory_diary()[0]['state'] == 'undone'


def test_undo_refuses_to_clobber_human_edits(context):
    service = reviewer(context)
    store, memory, chat_id = context
    store.memory_settings({'review_interval': 1})
    snapshot = store.memory_evidence(chat_id)
    note, audit_id = service.learn(chat_id, snapshot, {'message_id': snapshot[0]['id'], 'quote': 'Rex'})
    memory.edit(note.slug, 'Human corrected this')
    with pytest.raises(MemoryStoreError, match='changed'):
        service.undo(audit_id)
    assert memory.get(note.slug).body == 'Human corrected this'


def test_review_claim_counts_new_turns_and_survives_restart(context):
    store, _, chat_id = context
    cutoff = store.memory_evidence(chat_id)[-1]['id']
    assert not store.memory_review_claim(chat_id, cutoff, 2)
    store.add_message(chat_id, 'user', 'Another turn')
    cutoff = store.memory_evidence(chat_id)[-1]['id']
    assert store.memory_review_claim(chat_id, cutoff, 2)
    reopened = Store(store.path)
    assert not reopened.memory_review_claim(chat_id, cutoff, 2)


def test_consolidation_gates_extract_only_and_reversible(context):
    service = reviewer(context)
    store, memory, chat_id = context
    snapshot = store.memory_evidence(chat_id)
    provenance = {'source_chat_id': chat_id, 'kind': 'user-stated',
                  'evidence': [{'message_id': snapshot[0]['id'], 'quote': 'My dog is called Rex.'}]}
    a = memory.add('A', 'My dog is called Rex.', provenance=provenance)
    memory.add('B', a.body, provenance=provenance)
    memory.add('Padding', 'Other content ' * 100)
    digest = hashlib.sha256(a.path.read_bytes()).hexdigest()
    operation = {'op': 'deduplicate', 'slug': 'a', 'target': 'b', 'sha256': digest, 'score': .99}
    with pytest.raises(MemoryStoreError, match='opt'):
        service.consolidate([operation])
    store.memory_settings({'nightly': True})
    for invalid in [dict(operation, score=.1), dict(operation, sha256='stale'),
                    dict(operation, op='replace', text='Hallucinated text')]:
        with pytest.raises(MemoryStoreError):
            service.consolidate([invalid])
    ids = service.consolidate([operation])
    assert memory.get('a') is None
    assert memory.get('b').body == a.body
    assert 'deduplicate' in store.memory_diary()[0]['diary']
    service.undo(ids[0])
    assert a.path.read_bytes() == store.memory_diary()[0]['preimage'].encode()


def test_consolidation_requires_valid_provenance_and_caps_total_loss(context):
    service = reviewer(context)
    store, memory, chat_id = context
    store.memory_settings({'nightly': True})
    a = memory.add('A', 'same fact')
    memory.add('B', 'same fact')
    operation = {'op': 'deduplicate', 'slug': 'a', 'target': 'b',
                 'sha256': hashlib.sha256(a.path.read_bytes()).hexdigest(), 'score': .99}
    with pytest.raises(MemoryStoreError, match='provenance'):
        service.consolidate([operation])
    evidence = {'source_chat_id': chat_id, 'kind': 'user-stated',
                'evidence': [{'message_id': store.memory_evidence(chat_id)[0]['id'], 'quote': 'Rex'}]}
    a = memory.save('A', 'Rex', provenance=evidence)
    memory.save('B', 'Rex', provenance=evidence)
    operation['sha256'] = hashlib.sha256(a.path.read_bytes()).hexdigest()
    with pytest.raises(MemoryStoreError, match='loss'):
        service.consolidate([operation])
    assert len(memory.list()) == 2


async def test_background_review_has_restricted_tools_and_emits_undo_notice(context):
    from kova.models import ModelEvent
    from kova.review import MemoryReview
    store, memory, chat_id = context
    store.memory_settings({'review_interval': 1})
    snapshot = store.memory_evidence(chat_id)
    emitted = []

    class Client:
        async def stream(self, messages, tools=None):
            assert {t['function']['name'] for t in tools} == {'memory_list', 'memory_search', 'memory_write'}
            assert 'frozen_conversation' in messages[1]['content']
            yield ModelEvent('tool_call', tool_call={'id': 'one', 'name': 'memory_write',
                             'arguments': json.dumps({'message_id': snapshot[0]['id'], 'quote': 'My dog is called Rex.'})})

    service = MemoryReview(store, memory, emitted.append)
    service.after_turn(chat_id, Client())
    await __import__('asyncio').gather(*service.tasks)
    assert emitted[0]['text'] == 'Learned: My dog is called Rex. (say undo)'
    assert service.undo_request(chat_id, 'undo').startswith('Undid learned memory:')
    assert memory.list() == []


def test_model_suggested_writes_require_opt_in(context, tmp_path):
    from datetime import datetime
    from kova.config import Config
    from kova.tools import ToolContext
    from kova.tools.memory import tools
    from kova.tools.sessions import set_current_chat
    store, memory, chat_id = context
    context_tools = tools(ToolContext(Config(tmp_path, [], '', tmp_path / 'memory', tmp_path / 'files'), store, memory, datetime.now))
    add = next(t for t in context_tools if t.name == 'memory_add')
    set_current_chat(chat_id)
    try:
        with pytest.raises(MemoryStoreError, match='opt'):
            add.run({'title': 'Opinion', 'body': 'Seems impatient'})
        store.memory_settings({'model_writes': True})
        add.run({'title': 'Fact', 'body': 'My dog is called Rex.'})
        assert memory.get('fact').provenance['source_chat_id'] == chat_id
    finally:
        set_current_chat(None)
