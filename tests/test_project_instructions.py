"""Layered guidance: discovery levels, precedence, and the byte budget."""

from pathlib import Path

import pytest

from kova.project_instructions import (INSTRUCTION_BUDGET, directory_instructions,
                                       project_instructions, project_root)


@pytest.fixture(autouse=True)
def _empty_home(monkeypatch, tmp_path):
    """A Kova home with no guidance, so the user level never leaks into a test."""
    monkeypatch.setenv("KOVA_HOME", str(tmp_path / "home"))


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
    # 12001 bytes exceeds the 12000 read ceiling, so the file is skipped entirely.
    (tmp_path / "KOVA.md").write_bytes(b"a" * 12_001)
    assert project_instructions(tmp_path) == ""


def test_keeps_a_file_just_under_the_cap(tmp_path):
    (tmp_path / "KOVA.md").write_bytes(b"b" * 12_000)
    out = project_instructions(tmp_path)
    assert "### KOVA.md" in out


def test_skips_binary_files(tmp_path):
    (tmp_path / "KOVA.md").write_bytes(b"line1\n\x00line2")
    assert project_instructions(tmp_path) == ""


def test_output_is_capped_at_the_budget(tmp_path):
    (tmp_path / "KOVA.md").write_text("x" * 12_000, encoding="utf-8")
    out = project_instructions(tmp_path)
    assert len(out.encode("utf-8")) == INSTRUCTION_BUDGET
    assert out.startswith("Project instructions")


def test_block_content_is_stripped(tmp_path):
    (tmp_path / "KOVA.md").write_text("   spaced   \n\n", encoding="utf-8")
    out = project_instructions(tmp_path)
    assert "spaced" in out
    assert "   spaced" not in out


def _project(tmp_path):
    project = tmp_path / "repo"
    (project / ".git").mkdir(parents=True)
    return project


def test_reads_user_project_and_working_folder_levels(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    (home / "KOVA.md").write_text("user level", encoding="utf-8")
    project = _project(tmp_path)
    (project / "KOVA.md").write_text("project level", encoding="utf-8")
    working = project / "src" / "deep"
    working.mkdir(parents=True)
    (working / "AGENTS.md").write_text("working level", encoding="utf-8")
    out = project_instructions(working, home=home)
    # Broadest first, so the working folder has the last word.
    assert out.index("user level") < out.index("project level") < out.index("working level")
    assert "### KOVA.md" in out                 # the project root's file
    assert "### src/KOVA.md" not in out
    assert "### src/deep/AGENTS.md" in out
    assert str(home / "KOVA.md") in out         # the user level is labelled by its path


def test_walks_from_the_project_root_and_stops_there(tmp_path):
    project = _project(tmp_path)
    (project / "KOVA.md").write_text("root rule", encoding="utf-8")
    (tmp_path / "KOVA.md").write_text("above the project", encoding="utf-8")
    working = project / "pkg"
    working.mkdir()
    out = project_instructions(working)
    assert "root rule" in out
    assert "above the project" not in out


def test_project_root_prefers_the_nearest_marker(tmp_path):
    project = _project(tmp_path)
    deep = project / "a" / "b"
    deep.mkdir(parents=True)
    assert project_root(deep) == project
    assert project_root(project) == project


def test_project_root_falls_back_to_the_nearest_guidance_folder(tmp_path):
    notes = tmp_path / "notes"
    (notes / "drafts").mkdir(parents=True)
    (notes / "KOVA.md").write_text("notes rule", encoding="utf-8")
    deep = notes / "drafts"
    assert project_root(deep) == notes
    assert "notes rule" in project_instructions(deep)


def test_project_root_is_the_working_folder_without_markers_or_guidance(tmp_path):
    working = tmp_path / "loose" / "src"
    working.mkdir(parents=True)
    assert project_root(working) == working


def test_the_budget_drops_the_broadest_level_first(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    (home / "KOVA.md").write_text("u" * 5_000, encoding="utf-8")
    project = _project(tmp_path)
    (project / "KOVA.md").write_text("p" * 9_000, encoding="utf-8")
    working = project / "src"
    working.mkdir()
    (working / "KOVA.md").write_text("working level", encoding="utf-8")
    out = project_instructions(working, home=home)
    assert len(out.encode("utf-8")) <= INSTRUCTION_BUDGET
    assert "working level" in out           # the folder in use survives
    assert "p" * 100 in out                 # the project level is cut to fit
    assert "u" * 100 not in out             # the user level is dropped


def test_the_same_file_is_never_read_twice(tmp_path):
    (tmp_path / "KOVA.md").write_text("only once", encoding="utf-8")
    out = project_instructions(tmp_path, home=tmp_path)
    assert out.count("only once") == 1


def test_symlinked_guidance_outside_the_folder_is_ignored(tmp_path):
    project = _project(tmp_path)
    secret = tmp_path / "secret.md"
    secret.write_text("external secret", encoding="utf-8")
    (project / "KOVA.md").symlink_to(secret)
    (project / "AGENTS.md").write_text("kept", encoding="utf-8")
    working = project / "src"
    working.mkdir()
    out = project_instructions(working)
    assert "kept" in out and "external secret" not in out


def test_directory_instructions_name_their_folder(tmp_path):
    project = _project(tmp_path)
    docs = project / "docs"
    docs.mkdir()
    (docs / "AGENTS.md").write_text("docs rule", encoding="utf-8")
    text = directory_instructions(docs, root=project)
    assert text.startswith("Instructions for docs/")
    assert "### docs/AGENTS.md" in text and "docs rule" in text
    # Nothing to load in a folder without guidance of its own.
    assert directory_instructions(project, root=project) == ""


def test_directory_instructions_are_budgeted(tmp_path):
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "KOVA.md").write_text("y" * 20, encoding="utf-8")
    assert len(directory_instructions(docs, budget=64).encode("utf-8")) <= 64
    assert directory_instructions(docs, budget=0) == ""
