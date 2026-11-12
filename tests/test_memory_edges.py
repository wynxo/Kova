"""Edge cases for Markdown memory: parsing, search and Obsidian-mode paths.

Complementary to ``test_memory.py``: this file covers empty vaults, unicode and
emoji, unusual frontmatter, long notes, and nested/hostile write directories.
"""

from __future__ import annotations

from datetime import datetime

import pytest

from kova.memory import Memory, slugify


def test_search_with_no_notes(tmp_path):
    missing = Memory(tmp_path / "does-not-exist")
    assert missing.list() == []
    assert missing.search("dog") == []
    assert missing.get("dog") is None

    empty = Memory(tmp_path / "empty")
    empty.root.mkdir()
    assert empty.search("dog") == []


def test_search_with_empty_or_too_short_queries_returns_nothing(tmp_path):
    mem = Memory(tmp_path)
    mem.save("Dog", "Rex is my dog")
    for query in ("", "   ", "\n\t", "a", "!!", "a b", "x y z"):
        assert mem.search(query) == [], query


def test_search_matches_unicode_words_and_uppercase(tmp_path):
    mem = Memory(tmp_path)
    mem.save("Café ☕", "naïve résumé about coffee")
    mem.save("猫の手帳", "cat notebook")
    assert [n.title for n in mem.search("CAFÉ")] == ["Café ☕"]
    assert [n.title for n in mem.search("naïve")] == ["Café ☕"]
    assert [n.title for n in mem.search("猫の")] == ["猫の手帳"]


def test_search_scores_add_up_across_query_words(tmp_path):
    mem = Memory(tmp_path)
    mem.save("Dog cat", "unrelated")
    mem.save("Dog feeding", "cat lives here")
    mem.save("Dog grooming", "nothing else")
    assert [n.title for n in mem.search("dog cat")] == [
        "Dog cat", "Dog feeding", "Dog grooming"]
    assert [n.title for n in mem.search("dog cat lizard")] == [
        "Dog cat", "Dog feeding", "Dog grooming"]
    bodies = [n.body for n in mem.search("dog cat")]
    assert bodies == ["unrelated", "cat lives here", "nothing else"]


def test_search_limit_zero_and_one(tmp_path):
    mem = Memory(tmp_path)
    mem.save("Dog", "one")
    mem.save("Dog walks", "two")
    assert mem.search("dog", limit=0) == []
    assert len(mem.search("dog", limit=1)) == 1
    assert [n.body for n in mem.search("dog", limit=1)] == ["two"]  # title hit x3 wins


@pytest.mark.xfail(
    reason="bug: Memory.search splits the query on whitespace only, so punctuation "
           "attached to a word (\"dog's\", \"Rex!\") never matches the note text",
    strict=True,
)
def test_search_matches_words_with_attached_punctuation(tmp_path):
    mem = Memory(tmp_path)
    mem.save("Dog", "Rex is my dog")
    assert [n.title for n in mem.search("dog's")] == ["Dog"]
    assert [n.title for n in mem.search("Rex!")] == ["Dog"]


def test_search_tags_given_as_a_bare_string(tmp_path):
    mem = Memory(tmp_path)
    path = tmp_path / "note.md"
    path.write_text("---\ntitle: Note\ntags: pets\n---\nbody\n")
    assert mem.get("note").tags == ["pets"]
    assert [n.title for n in mem.search("pets")] == ["Note"]

    path.write_text("---\ntitle: Empty tags\ntags: ''\n---\nbody\n")
    assert mem.get("note").tags == []


def test_save_roundtrips_emoji_and_unicode(tmp_path):
    mem = Memory(tmp_path)
    note = mem.save("🐶 Dog walk", "Rex 🐕 loves the park", ["🐾", "pets"])
    assert note.slug == "dog-walk"
    got = mem.get("dog-walk")
    assert (got.title, got.body, got.tags) == (
        "🐶 Dog walk", "Rex 🐕 loves the park", ["🐾", "pets"])
    assert [n.title for n in mem.search("park")] == ["🐶 Dog walk"]
    assert [n.title for n in mem.search("pets")] == ["🐶 Dog walk"]


@pytest.mark.xfail(
    reason="bug: emoji-only titles both slugify to 'note', so saving a second one "
           "silently overwrites the first (data loss)",
    strict=True,
)
def test_emoji_only_titles_are_saved_separately(tmp_path):
    mem = Memory(tmp_path)
    mem.save("🐶", "dog note")
    mem.save("😀", "happy note")
    assert slugify("🐶") != slugify("😀")
    assert {n.body for n in mem.list()} == {"dog note", "happy note"}


def test_save_roundtrips_a_very_long_title_and_body(tmp_path):
    mem = Memory(tmp_path)
    title = "A very long title " * 50
    body = "long body line\n" * 8000
    note = mem.save(title, body)
    assert len(note.slug) == 60
    assert note.path.name == f"{note.slug}.md"
    got = mem.get(note.slug)
    assert got.title == title
    assert got.body == body


def test_body_may_contain_frontmatter_delimiters(tmp_path):
    mem = Memory(tmp_path)
    body = "intro\n---\nkey: value\n---\nend\n"
    note = mem.save("Tricky", body)
    assert mem.get(note.slug).body == body


@pytest.mark.parametrize(
    "text",
    [
        "---\ntitle: Secret\ntags:\n  - x\n---",
        "\ufeff---\ntitle: Secret\ntags:\n  - x\n---\nbody\n",
    ],
    ids=["frontmatter-at-end-of-file", "frontmatter-after-utf8-bom"],
)
@pytest.mark.xfail(
    reason="bug: frontmatter is only detected when the file starts with exactly "
           "'---\\n' and the closing '---' is followed by a newline, so a UTF-8 BOM "
           "or a note ending with its frontmatter loses its title/tags and leaks "
           "the YAML into the body",
    strict=True,
)
def test_frontmatter_variants_are_parsed(tmp_path, text):
    (tmp_path / "note.md").write_text(text)
    note = Memory(tmp_path).get("note")
    assert note.title == "Secret"
    assert note.tags == ["x"]
    assert "title:" not in note.body


def test_invalid_yaml_frontmatter_is_kept_as_body(tmp_path):
    text = "---\ntitle: [unclosed\n---\nbody\n"
    (tmp_path / "broken.md").write_text(text)
    note = Memory(tmp_path).get("broken")
    assert (note.title, note.tags, note.body) == ("broken", [], text)


def test_non_mapping_frontmatter_is_kept_as_body(tmp_path):
    text = "---\n- a\n- b\n---\nbody\n"
    (tmp_path / "list.md").write_text(text)
    note = Memory(tmp_path).get("list")
    assert (note.title, note.tags, note.body) == ("list", [], text)


def test_frontmatter_title_falls_back_to_the_file_stem(tmp_path):
    (tmp_path / "plain.md").write_text("---\ntags: [x]\n---\nbody\n")
    (tmp_path / "empty.md").write_text("---\ntitle: ''\n---\nbody\n")
    (tmp_path / "null.md").write_text("---\ntitle:\n---\nbody\n")
    mem = Memory(tmp_path)
    assert mem.get("plain").title == "plain"
    assert mem.get("empty").title == "empty"
    assert mem.get("null").title == "null"
    assert mem.get("plain").tags == ["x"]


def test_frontmatter_keeps_non_string_titles_and_updates(tmp_path):
    (tmp_path / "year.md").write_text("---\ntitle: 123\nupdated: 2026-01-02\n---\nbody\n")
    note = Memory(tmp_path).get("year")
    assert note.title == "123"
    assert note.updated == "2026-01-02"


def test_plain_note_gets_a_parseable_mtime_update(tmp_path):
    (tmp_path / "plain.md").write_text("hello\n")
    mem = Memory(tmp_path)
    note = mem.get("plain")
    assert datetime.fromisoformat(note.updated)
    assert mem.get("plain").updated == note.updated


def test_missing_root_is_created_on_first_save(tmp_path):
    mem = Memory(tmp_path / "nested" / "memory")
    assert mem.list() == []
    note = mem.save("First", "hello")
    assert note.path == tmp_path / "nested" / "memory" / "first.md"
    assert [n.slug for n in mem.list()] == ["first"]


# -- Obsidian mode --------------------------------------------------------

def test_obsidian_nested_write_subdir_roundtrip(tmp_path):
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "Mine.md").write_text("---\ntitle: Mine\n---\nmy own note\n")
    mem = Memory(vault, "Kova/notes")
    note = mem.save("Dog", "Kova writes here", ["dog"])
    assert note.path == vault / "Kova" / "notes" / "dog.md"
    assert (vault / "Mine.md").read_text() == "---\ntitle: Mine\n---\nmy own note\n"
    assert mem.get("dog").body == "Kova writes here"
    assert {n.title for n in mem.list()} == {"Mine", "Dog"}
    assert mem.delete("dog") is True
    assert mem.get("dog") is None
    assert mem.delete("dog") is False


def test_obsidian_delete_reveals_the_original_vault_note(tmp_path):
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "Dog.md").write_text("---\ntitle: Dog\n---\nMy own dog note.\n")
    mem = Memory(vault, "Kova")
    mem.save("Dog", "Kova writes here")
    assert mem.get("dog").body == "Kova writes here"  # write dir wins for its own slug
    assert mem.get("Dog").body == "My own dog note.\n"  # the vault note is read too
    assert mem.delete("dog") is True
    assert mem.get("dog") is None
    assert mem.get("Dog").body == "My own dog note.\n"


def test_obsidian_write_subdir_cannot_escape_the_vault(tmp_path):
    vault = tmp_path / "vault"
    vault.mkdir()
    outside = tmp_path / "outside"
    for subdir in ("../outside", str(outside)):
        mem = Memory(vault, subdir)
        with pytest.raises(ValueError, match="Refusing to write memory note"):
            mem.save("Escape", "should not land")
        assert mem.delete("escape") is False
    assert list(outside.rglob("*.md")) == []


def test_obsidian_search_reads_nested_vault_notes_but_skips_hidden_dirs(tmp_path):
    vault = tmp_path / "vault"
    (vault / "People").mkdir(parents=True)
    (vault / "People" / "Ada.md").write_text("---\ntitle: Ada\n---\nMathematician.\n")
    (vault / ".obsidian").mkdir()
    (vault / ".obsidian" / "secret.md").write_text("---\ntitle: Secret\n---\nhidden\n")
    (vault / ".trash").mkdir()
    (vault / ".trash" / "old.md").write_text("old note\n")
    mem = Memory(vault, "Kova")
    mem.save("Me", "I like Ada Lovelace")
    assert [n.title for n in mem.search("ada")] == ["Ada", "Me"]
    assert mem.search("secret") == []
    assert {n.title for n in mem.list()} == {"Ada", "Me"}
