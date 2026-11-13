"""Layered, budgeted project guidance: KOVA.md / AGENTS.md at several levels."""

from kova.project_instructions import (
    MAX_FILE_BYTES,
    TOTAL_BUDGET,
    project_instructions,
    sections,
)


def test_empty_when_no_guidance_files(tmp_path):
    assert project_instructions(tmp_path) == ""


def test_reads_kova_md_wrapped_with_header(tmp_path):
    (tmp_path / "KOVA.md").write_text("# Title\nbe nice\n", encoding="utf-8")
    out = project_instructions(tmp_path)
    assert out.startswith("Project instructions")
    assert "### KOVA.md" in out
    assert "be nice" in out


def test_reads_both_files_in_order(tmp_path):
    (tmp_path / "KOVA.md").write_text("alpha", encoding="utf-8")
    (tmp_path / "AGENTS.md").write_text("beta", encoding="utf-8")
    out = project_instructions(tmp_path)
    assert out.index("### KOVA.md") < out.index("### AGENTS.md")
    assert "alpha" in out and "beta" in out


def test_skips_files_larger_than_the_read_cap(tmp_path):
    # 12001 bytes exceeds the per-file read ceiling, so the file is skipped.
    (tmp_path / "KOVA.md").write_bytes(b"a" * (MAX_FILE_BYTES + 1))
    assert project_instructions(tmp_path) == ""


def test_keeps_a_file_just_under_the_cap(tmp_path):
    (tmp_path / "KOVA.md").write_bytes(b"b" * MAX_FILE_BYTES)
    out = project_instructions(tmp_path)
    assert "### KOVA.md" in out


def test_skips_binary_files(tmp_path):
    (tmp_path / "KOVA.md").write_bytes(b"line1\n\x00line2")
    assert project_instructions(tmp_path) == ""


def test_output_is_capped_at_the_total_budget(tmp_path):
    (tmp_path / "KOVA.md").write_text("x" * 4000, encoding="utf-8")
    out = project_instructions(tmp_path, budget=4096)
    assert len(out.encode("utf-8")) <= 4096
    assert out.startswith("Project instructions")
    assert out.endswith("…[truncated]")


def test_default_budget_is_larger_than_the_old_four_kilobyte_cap():
    assert TOTAL_BUDGET > 4096


def test_discovers_user_project_root_and_working_folder(tmp_path):
    home, root, sub = tmp_path / "home", tmp_path / "repo", tmp_path / "repo" / "src"
    sub.mkdir(parents=True)
    (root / ".git").mkdir()
    home.mkdir()
    (home / "KOVA.md").write_text("user preference", encoding="utf-8")
    (root / "KOVA.md").write_text("repository rule", encoding="utf-8")
    (sub / "AGENTS.md").write_text("src rule", encoding="utf-8")

    out = project_instructions(sub, user_home=home)
    assert out.index("user preference") < out.index("repository rule") < out.index("src rule")
    assert "### KOVA.md (user)" in out
    assert "### KOVA.md (project)" in out
    assert "### AGENTS.md (project: src)" in out
    assert "take precedence" in out


def test_subdirectory_guidance_is_loaded_only_where_it_applies(tmp_path):
    root = tmp_path / "repo"
    (root / ".git").mkdir(parents=True)
    for name, text in (("a", "rule for a"), ("b", "rule for b")):
        folder = root / name
        folder.mkdir()
        (folder / "KOVA.md").write_text(text, encoding="utf-8")

    inside_a = project_instructions(root / "a")
    assert "rule for a" in inside_a and "rule for b" not in inside_a
    at_root = project_instructions(root)
    assert "rule for a" not in at_root and "rule for b" not in at_root


def test_budget_keeps_the_more_specific_level(tmp_path):
    home, root, sub = tmp_path / "home", tmp_path / "repo", tmp_path / "repo" / "src"
    sub.mkdir(parents=True)
    (root / ".git").mkdir()
    home.mkdir()
    (home / "KOVA.md").write_text("U" * 900, encoding="utf-8")
    (root / "KOVA.md").write_text("R" * 900, encoding="utf-8")
    (sub / "KOVA.md").write_text("S" * 900, encoding="utf-8")

    out = project_instructions(sub, user_home=home, budget=1400)
    assert len(out.encode("utf-8")) <= 1400
    assert "S" * 100 in out             # the deepest rule survives
    assert "U" * 100 not in out         # the least specific one is dropped


def test_ignores_a_git_file_worktree_marker(tmp_path):
    # A worktree uses a ``.git`` file rather than a directory.
    root, sub = tmp_path / "repo", tmp_path / "repo" / "nested"
    sub.mkdir(parents=True)
    (root / ".git").write_text("gitdir: elsewhere\n", encoding="utf-8")
    (root / "KOVA.md").write_text("root rule", encoding="utf-8")
    assert "root rule" in project_instructions(sub)


def test_sections_are_ordered_least_specific_first(tmp_path):
    (tmp_path / "KOVA.md").write_text("one", encoding="utf-8")
    found = sections(tmp_path)
    assert [(section.level, section.name) for section in found] == [("project", "KOVA.md")]
