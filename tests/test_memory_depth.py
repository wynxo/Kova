"""Behavioral guards for indexed recall and evidence-backed memory."""
import pytest

from kova.memory import Memory


def test_index_is_bounded_and_does_not_load_topic_bodies(tmp_path):
    mem = Memory(tmp_path, budget=0)
    for i in range(205):
        mem.add(f'Topic {i}', 'Private long topic body')
    index = mem.index()
    assert len(index.splitlines()) <= 200
    assert 'Private long topic body' not in index
    assert 'topic-0' in index
    assert 'memory_search' in index


def test_hybrid_uses_keyword_weights_and_idf_cosine(tmp_path):
    mem = Memory(tmp_path)
    mem.save('Dog', 'ordinary words')
    mem.save('Other', 'dog')
    assert mem.search('dog')[0].title == 'Dog'
    mem.save('Repetition', 'alpha ' * 20)
    mem.save('Coverage', 'alpha quasar')
    scores = mem.search_scores('alpha quasar')
    by_title = {row['note'].title: row for row in scores}
    assert by_title['Repetition']['keyword'] > by_title['Coverage']['keyword']
    assert by_title['Coverage']['cosine'] > by_title['Repetition']['cosine']
    assert scores[0]['note'].title == 'Coverage'


def test_provenance_survives_patch_and_legacy_is_explicit(tmp_path):
    mem = Memory(tmp_path)
    note = mem.add('Dog', 'Rex', provenance={'source_chat_id': 'abc', 'kind': 'user-stated', 'date': '2026-11-13', 'evidence': [{'message_id': 1, 'quote': 'Rex'}]})
    mem.patch(note.slug, 'Rex', 'Rex the dog')
    assert mem.get(note.slug).provenance == note.provenance
    (tmp_path / 'legacy.md').write_text('Old note')
    assert mem.get('legacy').provenance['kind'] == 'unknown'
    assert mem.get('legacy').provenance['source_chat_id'] is None
