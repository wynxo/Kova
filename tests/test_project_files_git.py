"""The file picker lists git repositories exactly: real names, every folder."""

import shutil
import subprocess

import pytest

from kova import project_files

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="needs git")


def _repo(tmp_path):
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    (tmp_path / "a" / "b").mkdir(parents=True)
    (tmp_path / "a" / "b" / "c.py").write_text("")
    (tmp_path / "café.txt").write_text("")
    (tmp_path / "with space.txt").write_text("")
    return tmp_path


def test_non_ascii_names_are_not_git_quoted(tmp_path):
    found = project_files.paths(_repo(tmp_path))
    assert "café.txt" in found
    assert not any(path.startswith('"') for path in found)
    assert "with space.txt" in found


def test_every_ancestor_folder_is_listed(tmp_path):
    found = project_files.paths(_repo(tmp_path))
    assert "a/" in found
    assert "a/b/" in found
