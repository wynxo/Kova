from kova.memory import Memory, Note, slugify


def test_slugify():
    assert slugify("My Dog's Name!") == "my-dog-s-name"
    assert slugify("") == "note"
    assert len(slugify("x" * 100)) == 60


def test_save_get_roundtrip(tmp_path):
    mem = Memory(tmp_path)
    note = mem.save("My Dog's Name!", "Rex is a good boy.", ["pets"])
    assert isinstance(note, Note)
    assert (note.slug, note.title, note.body, note.tags, note.path) == (
        "my-dog-s-name",
        "My Dog's Name!",
        "Rex is a good boy.",
        ["pets"],
        tmp_path / "my-dog-s-name.md",
    )
    got = mem.get("my-dog-s-name")
    assert got is not None
    assert (got.title, got.body, got.tags) == ("My Dog's Name!", "Rex is a good boy.", ["pets"])
    # same slug overwrites
    mem.save("My Dog's Name!", "Rex turned three.", ["pets"])
    again = mem.get("my-dog-s-name")
    assert (again.body, again.tags) == ("Rex turned three.", ["pets"])
    # tags default to empty, unknown slugs to None
    assert mem.save("Plain", "No tags given").tags == []
    assert mem.get("missing") is None


def test_plain_markdown_without_frontmatter_is_listed(tmp_path):
    mem = Memory(tmp_path)
    (tmp_path / "recipes.md").write_text("# Recipes\n\nBoil water.\n")
    notes = mem.list()
    assert [n.slug for n in notes] == ["recipes"]
    note = notes[0]
    assert (note.title, note.body, note.tags) == ("recipes", "# Recipes\n\nBoil water.\n", [])
    assert mem.get("recipes").body == "# Recipes\n\nBoil water.\n"


def test_hidden_dirs_skipped(tmp_path):
    mem = Memory(tmp_path)
    mem.save("Visible note", "hello")
    obsidian = tmp_path / ".obsidian"
    obsidian.mkdir()
    (obsidian / "secret.md").write_text("---\ntitle: Secret\n---\nhidden\n")
    trash = tmp_path / ".trash"
    trash.mkdir()
    (trash / "old.md").write_text("old note\n")
    assert [n.slug for n in mem.list()] == ["visible-note"]
    assert mem.search("secret") == []
    assert mem.get("secret") is None


def test_obsidian_mode_writes_only_in_kova_dir(tmp_path):
    vault = tmp_path / "vault"
    vault.mkdir()
    original = vault / "Dog.md"
    original.write_text("---\ntitle: Dog\n---\nMy own dog note.\n")
    before = original.read_bytes()

    mem = Memory(vault, "Kova")
    note = mem.save("Dog", "Kova writes here", ["dog"])
    assert note.path == vault / "Kova" / "dog.md"
    assert original.read_bytes() == before
    # reads the whole vault, but the write dir wins on equal-ish slugs
    assert mem.get("dog").body == "Kova writes here"
    assert mem.get("Dog").body == "My own dog note.\n"

    # delete removes only the Kova copy, never the user's note
    assert mem.delete("dog") is True
    assert not (vault / "Kova" / "dog.md").exists()
    assert original.read_bytes() == before
    assert mem.delete("Dog") is False
    assert original.read_bytes() == before


def test_search_ranks_title_over_body(tmp_path):
    mem = Memory(tmp_path)
    mem.save("Cat training", "Feeding schedules for pets")  # no hit -> excluded
    mem.save("Dog training", "Feeding schedules for pets")  # title hit -> 3
    mem.save("Pet care", "How to walk a dog safely")  # body hit -> 1
    mem.save("Animals I love", "Various creatures", tags=["dog"])  # tag hit -> 2
    mem.save("Dog older", "text")
    mem.save("Dog newer", "text")
    results = mem.search("dog")
    assert [n.title for n in results] == [
        "Dog newer",  # score 3, newest updated
        "Dog older",  # score 3
        "Dog training",  # score 3, oldest of the three
        "Animals I love",  # score 2 (tag)
        "Pet care",  # score 1 (body)
    ]
    # words shorter than 2 chars are ignored, so this scores exactly like "dog"
    assert [n.title for n in mem.search("a dog")] == [n.title for n in results]
    assert [n.title for n in mem.search("dog", limit=2)] == ["Dog newer", "Dog older"]


def test_slug_path_traversal_refused(tmp_path):
    outside = tmp_path / "secret.md"
    outside.write_text("keep me")
    mem = Memory(tmp_path / "memory")
    mem.save("x", "y")
    for bad in ["../secret", "../../etc/passwd", "/etc/passwd", ".hidden", "a/b"]:
        assert mem.delete(bad) is False
        assert mem.get(bad) is None
    assert outside.read_text() == "keep me"


def test_unicode_titles_do_not_collide(tmp_path):
    mem = Memory(tmp_path)
    a, b = mem.save("猫", "cat"), mem.save("犬", "dog")
    assert a.slug != b.slug and mem.get(a.slug).body == "cat" and mem.get(b.slug).body == "dog"


def test_broken_note_is_skipped(tmp_path):
    (tmp_path / "bad.md").write_bytes(b"\xff\xfe\x00bad")
    mem = Memory(tmp_path)
    mem.save("Good", "dog")
    assert [n.title for n in mem.search("dog")] == ["Good"]


def test_symlinked_note_is_not_written_through(tmp_path):
    import pytest

    vault = tmp_path / "vault"
    (vault / "Kova").mkdir(parents=True)
    (vault / "Dog.md").write_text("mine")
    (vault / "Kova" / "dog.md").symlink_to(vault / "Dog.md")
    with pytest.raises(ValueError):
        Memory(vault, "Kova").save("Dog", "overwrite")
    assert (vault / "Dog.md").read_text() == "mine"
