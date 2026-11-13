"""Background memory passes: hybrid recall, review gates and consolidation gates.

These pin the non-obvious logic: the lexical signal reorders but never invents
candidates, review facts need a verbatim user quote, and consolidation can only
regroup lines that already exist, behind score, evidence and loss gates.
"""

from datetime import datetime, timedelta, timezone

import pytest

from kova.memory import Memory
from kova.review import (
    MemoryReview,
    ReviewSettings,
    gate_operations,
    parse_operations,
    review_fact,
    undo,
    verbatim_only,
)
from kova.store import Store

NOW = datetime(2026, 11, 12, 10, 0, tzinfo=timezone.utc)


@pytest.fixture
def memory(tmp_path):
    return Memory(tmp_path / "memory")


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / "kova.db", default_cwd=str(tmp_path))


def _note(memory, title, body, **kwargs):
    return memory.save(title, body, **kwargs)


# -- hybrid search ------------------------------------------------------


def test_hybrid_never_invents_a_candidate(memory):
    _note(memory, "Dog", "Rex is a good boy.")
    assert memory.search("platypus taxonomy", mode="hybrid") == []
    assert memory.search("platypus taxonomy", mode="keyword") == []


def test_hybrid_ranks_on_both_signals(memory):
    # Same keyword score twice (a tie), so the newest note wins on keywords —
    # but only the older note shares the rare word with the query.
    _note(memory, "Older", "Edinburgh xenon")
    _note(memory, "Newer", "Edinburgh Edinburgh")
    keyword = memory.search("edinburgh xenon", mode="keyword")
    assert [note.title for note in keyword] == ["Newer", "Older"]
    hybrid = memory.search("edinburgh xenon", mode="hybrid")
    assert [note.title for note in hybrid] == ["Older", "Newer"]


def test_index_text_caps_and_disables(memory):
    _note(memory, "index", "\n".join(f"topic {index}" for index in range(10)))
    assert len(memory.index_text().splitlines()) == 10
    capped = memory.index_text(3)
    assert len(capped.splitlines()) == 4  # three lines plus the truncation marker
    assert "more lines" in capped
    assert memory.index_text(0) == ""


def test_missing_index_is_empty(memory):
    assert memory.index() is None
    assert memory.index_text() == ""


# -- review facts --------------------------------------------------------


def test_review_fact_needs_a_verbatim_user_quote():
    quotes = ["My dog Rex loves swimming in the lake every morning."]
    fact, _ = review_fact(
        {"title": "Dog", "body": "Rex swims daily.",
         "quote": "My dog Rex loves swimming in the lake every morning."}, quotes)
    assert fact is not None and fact.title == "Dog"

    paraphrase, reason = review_fact(
        {"title": "Dog", "body": "Rex swims daily.", "quote": "Rex enjoys a swim"}, quotes)
    assert paraphrase is None and "not found in a user message" in reason


def test_review_fact_refuses_short_quotes_and_negative_lessons():
    quotes = ["The deploy script is broken and unreliable, do not use it."]
    short, reason = review_fact({"title": "T", "body": "B", "quote": "short"}, quotes)
    assert short is None and "at least" in reason
    negative, reason = review_fact(
        {"title": "Deploy script", "body": "The deploy script is broken.",
         "quote": "The deploy script is broken and unreliable, do not use it."}, quotes)
    assert negative is None and "broken or unreliable" in reason


# -- consolidation gates --------------------------------------------------


def _notes(memory):
    first = _note(memory, "Groceries", "Buy oat milk.\nBuy eggs.")
    second = _note(memory, "Shopping", "Buy oat milk.\nBuy bread.")
    return first, second


def test_verbatim_only_blocks_invented_text(memory):
    first, second = _notes(memory)
    assert verbatim_only("Buy oat milk.", [first.body, second.body]) is True
    assert verbatim_only("Buy oat milk and cheese.", [first.body, second.body]) is False
    assert verbatim_only("", [first.body]) is False


def _op(kind, slugs, **kwargs):
    base = {"op": kind, "slugs": slugs, "evidence": "Buy oat milk.",
            "confidence": 0.95, "reason": "same list twice", **kwargs}
    if kind in ("merge", "rewrite"):
        base.setdefault("title", "Lists")
        base.setdefault("body", "Buy oat milk.")
    return base


def test_gates_accept_a_good_merge(memory):
    first, second = _notes(memory)
    operations = parse_operations(
        '{"operations": [%s]}' % str(_op("merge", [first.slug, second.slug])).replace("'", '"'))
    # Merging two small lists drops most of their characters; the loss cap is
    # the point, so this acceptance test runs with the cap wide open.
    accepted, refused = gate_operations(operations, memory.list(), max_loss=1.0)
    assert len(accepted) == 1 and refused == []


def test_gates_refuse_low_confidence_missing_evidence_and_unknown_notes(memory):
    first, second = _notes(memory)
    operations = parse_operations('{"operations": ['
                                  '{"op": "forget", "slugs": ["nope"], "evidence": "Buy oat milk.", "confidence": 0.99},'
                                  '{"op": "rewrite", "slug": "%s", "title": "Lists", "body": "Buy oat milk.", "evidence": "", "confidence": 0.99},'
                                  '{"op": "forget", "slugs": ["%s"], "evidence": "Buy oat milk.", "confidence": 0.1}'
                                  ']}' % (first.slug, second.slug))
    accepted, refused = gate_operations(operations, memory.list())
    assert accepted == [] and len(refused) == 3


def test_gates_refuse_invented_text_and_double_use(memory):
    first, second = _notes(memory)
    operations = parse_operations('{"operations": ['
                                  '{"op": "merge", "slugs": ["%s", "%s"], "title": "Lists", "body": "Buy cheese.", "evidence": "Buy oat milk.", "confidence": 0.99},'
                                  '{"op": "forget", "slugs": ["%s"], "evidence": "Buy oat milk.", "confidence": 0.99}'
                                  ']}' % (first.slug, second.slug, first.slug))
    accepted, refused = gate_operations(operations, memory.list())
    assert accepted == [] and len(refused) == 2
    # Malformed entries never reach the gates at all.
    assert parse_operations("no json here") == []
    assert parse_operations('{"operations": [{"op": "nap", "slugs": []}]}') == []


def test_loss_cap_refuses_a_run_that_drops_too_much(memory):
    first, _ = _notes(memory)
    operations = parse_operations(
        '{"operations": [{"op": "forget", "slugs": ["%s"], '
        '"evidence": "Buy oat milk.", "confidence": 0.99}]}' % first.slug)
    accepted, refused = gate_operations(operations, memory.list(), max_loss=0.01)
    assert accepted == [] and any("whole run" in reason for reason in refused)


# -- undo -----------------------------------------------------------------


def test_undo_restores_a_rewrite_and_removes_a_review_note(memory, store):
    note = _note(memory, "Dog", "Rex is a good boy.")
    before = (memory.write_dir / f"{note.slug}.md").read_text()
    patched = memory.save("Dog", "Rex is a very good boy.")
    assert patched.body != note.body
    from kova.review import record_revision

    record_revision(store, memory, note.slug, "consolidate", before=before)
    created = memory.add("Lake", "Rex swims there.", origin="user", source="chat-1")
    record_revision(store, memory, created.slug, "review", before=None, chat_id="chat-1")

    touched = undo(memory, store)
    assert set(touched) == {note.slug, created.slug}
    assert memory.get(note.slug).body == "Rex is a good boy."
    assert memory.get(created.slug) is None
    # Undoing twice changes nothing: revisions are marked undone.
    assert undo(memory, store) == []


# -- scheduling ------------------------------------------------------------


def test_review_runs_every_n_turns_and_nightly_runs_once_a_day(store):
    review = MemoryReview(store, ReviewSettings(review=True, every=3,
                                                consolidate=True, consolidate_hour=9))
    messages = [{"role": "user", "content": "hello"},
                {"role": "assistant", "content": "hi"}] * 2
    assert review.review_due("chat-1", messages) is False
    messages += [{"role": "user", "content": "more"}]
    assert review.review_due("chat-1", messages) is True
    store.set_memory_review_turns("chat-1", 3)
    assert review.review_due("chat-1", messages) is False

    morning = datetime.now().astimezone().replace(hour=10, minute=0, second=0, microsecond=0)
    assert review.nightly_due(morning) is True
    store.add_memory_diary("consolidate", "merged two notes")
    assert review.nightly_due(morning) is False
    assert review.nightly_due(morning - timedelta(hours=5)) is False
