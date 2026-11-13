"""Bounded memory: add/patch/remove semantics and exact over-budget failures."""

from __future__ import annotations

import pytest

from kova.memory import (
    DEFAULT_MEMORY_BUDGET,
    Memory,
    MemoryBudgetError,
    MemoryStoreError,
    MemoryExistsError,
    note_chars,
)


def test_default_budget_is_sane_and_usage_counts_title_tags_and_body(tmp_path):
    assert DEFAULT_MEMORY_BUDGET == 8000
    mem = Memory(tmp_path)
    assert mem.budget == DEFAULT_MEMORY_BUDGET
    assert mem.usage() == 0
    note = mem.add("Dog", "Rex is a good boy", ["pets", "rex"])
    assert mem.usage() == note_chars(note.title, note.body, note.tags)
    assert mem.stats() == {"used": mem.usage(), "budget": 8000, "percent": 0, "notes": 1}


def test_add_never_overwrites_an_existing_note(tmp_path):
    mem = Memory(tmp_path)
    mem.add("Dog", "Rex is a good boy")
    with pytest.raises(MemoryExistsError, match="already exists"):
        mem.add("Dog", "Something entirely different")
    assert mem.get("dog").body == "Rex is a good boy"
    # The legacy save path keeps its upsert behaviour for the CLI and API.
    mem.save("Dog", "Rex turned three")
    assert mem.get("dog").body == "Rex turned three"


def test_slug_collisions_are_treated_as_the_same_note(tmp_path):
    mem = Memory(tmp_path)
    mem.add("My Dog's Name!", "Rex")
    with pytest.raises(MemoryExistsError):
        mem.add("My Dog's Name", "another Rex")
    assert [n.title for n in mem.list()] == ["My Dog's Name!"]


def test_over_budget_add_reports_the_exact_overflow_and_writes_nothing(tmp_path):
    mem = Memory(tmp_path, budget=40)
    mem.add("Dog", "Rex is a good boy")
    used = mem.usage()
    with pytest.raises(MemoryBudgetError) as caught:
        mem.add("Cat", "x" * 40)
    error = caught.value
    assert error.overflow == used + note_chars("Cat", "x" * 40, []) - 40
    assert error.used == used and error.budget == 40
    assert f"over budget by {error.overflow} characters" in str(error)
    assert f"{used} used, 40 allowed" in str(error)
    assert mem.get("cat") is None
    assert mem.get("dog").body == "Rex is a good boy"
    assert [n.slug for n in mem.list()] == ["dog"]


def test_a_write_exactly_at_the_budget_succeeds(tmp_path):
    title, body = "Dog", "Rex"
    budget = note_chars(title, body, [])
    mem = Memory(tmp_path, budget=budget)
    mem.add(title, body)
    assert mem.stats()["used"] == budget
    assert mem.stats()["percent"] == 100


def test_removing_frees_budget_for_a_new_note(tmp_path):
    mem = Memory(tmp_path, budget=60)
    mem.add("Dog", "Rex is a good boy")
    with pytest.raises(MemoryBudgetError):
        mem.add("Cat", "x" * 40)
    assert mem.remove("dog") is True
    assert mem.remove("dog") is False
    mem.add("Cat", "x" * 40)
    assert mem.get("cat").body == "x" * 40


def test_patch_replaces_one_targeted_span_and_keeps_siblings(tmp_path):
    mem = Memory(tmp_path)
    mem.add("Dog", "Rex is a good boy. Rex likes the park.", ["pets"])
    patched = mem.patch("dog", "Rex is a good boy.", "Rex is a very good boy.")
    assert patched.body == "Rex is a very good boy. Rex likes the park."
    assert patched.title == "Dog" and patched.tags == ["pets"]
    assert mem.get("dog").body == "Rex is a very good boy. Rex likes the park."


def test_patch_requires_a_unique_non_empty_match(tmp_path):
    mem = Memory(tmp_path)
    mem.add("Dog", "Rex likes Rex")

    with pytest.raises(MemoryStoreError, match="matches 2 times"):
        mem.patch("dog", "Rex", "Fido")
    with pytest.raises(MemoryStoreError, match="exactly once"):
        mem.patch("dog", "Rex", "Fido")
    with pytest.raises(MemoryStoreError, match="not found"):
        mem.patch("dog", "missing", "Fido")
    with pytest.raises(MemoryStoreError, match="must not be empty"):
        mem.patch("dog", "", "Fido")
    with pytest.raises(MemoryStoreError, match='No note named "cat"'):
        mem.patch("cat", "Rex", "Fido")
    assert mem.get("dog").body == "Rex likes Rex"  # every refusal left the note alone


def test_patch_cannot_escape_the_kova_write_folder(tmp_path):
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "Dog.md").write_text("---\ntitle: Dog\n---\nMy own note.\n")
    mem = Memory(vault, "Kova")
    with pytest.raises(MemoryStoreError, match="outside Kova's write folder"):
        mem.patch("Dog", "My own note.", "changed")
    assert (vault / "Dog.md").read_text() == "---\ntitle: Dog\n---\nMy own note.\n"


def test_patch_enforces_the_budget_and_leaves_the_note_intact(tmp_path):
    mem = Memory(tmp_path, budget=30)
    mem.add("Dog", "Rex is a good boy")
    used = mem.usage()
    with pytest.raises(MemoryBudgetError) as caught:
        mem.patch("dog", "good", "very extremely good")
    expected = used - len("good") + len("very extremely good") - 30
    assert caught.value.overflow == expected
    assert mem.get("dog").body == "Rex is a good boy"


def test_patch_can_shrink_a_note_that_is_already_over_budget(tmp_path):
    mem = Memory(tmp_path, budget=0)
    mem.add("Dog", "Rex " * 100)
    tight = Memory(tmp_path, budget=10)
    assert tight.usage() > tight.budget  # reading existing notes still works
    assert [n.slug for n in tight.list()] == ["dog"]
    tight.patch("dog", "Rex " * 100, "Rex")
    assert tight.usage() == note_chars("Dog", "Rex", [])


def test_patch_refuses_a_symlinked_note(tmp_path):
    mem = Memory(tmp_path)
    outside = tmp_path / "outside.md"
    outside.write_text("keep")
    (tmp_path / "dog.md").symlink_to(outside)
    with pytest.raises(MemoryStoreError, match="through a link"):
        mem.patch("dog", "keep", "changed")
    assert outside.read_text() == "keep"


def test_budget_zero_or_none_means_unlimited(tmp_path):
    for budget in (0, None):
        mem = Memory(tmp_path / f"b{budget}", budget=budget)
        mem.add("Long", "x" * 20_000)
        assert mem.budget == 0
        assert mem.stats()["percent"] == 0
        assert mem.stats()["used"] == 20_004


def test_hidden_and_broken_notes_do_not_count_against_the_budget(tmp_path):
    (tmp_path / ".obsidian").mkdir()
    (tmp_path / ".obsidian" / "hidden.md").write_text("x" * 5000)
    (tmp_path / "broken.md").write_bytes(b"\xff\xfe\x00bad")
    mem = Memory(tmp_path)
    mem.add("Dog", "Rex")
    assert mem.usage() == note_chars("Dog", "Rex", [])
    assert [n.slug for n in mem.list()] == ["dog"]


def test_budget_uses_updated_size_when_save_overwrites(tmp_path):
    mem = Memory(tmp_path, budget=30)
    mem.add("Dog", "x" * 20)  # 23 chars
    # Overwriting with a longer body is checked against the replaced size.
    with pytest.raises(MemoryBudgetError):
        mem.save("Dog", "y" * 30)
    mem.save("Dog", "y" * 10)
    assert mem.usage() == note_chars("Dog", "y" * 10, [])
    assert mem.get("dog").body == "y" * 10
