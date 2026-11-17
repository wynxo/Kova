"""The no-ripgrep path must actually stop catastrophic regex execution."""
import time

from kova.tools import code_files


def test_catastrophic_regex_timeout_preserves_partial_matches(tmp_path, monkeypatch):
    (tmp_path / "input.txt").write_text("aaa\n" + "a" * 32 + "Z\n")
    monkeypatch.setattr(code_files.shutil, "which", lambda _: None)
    # The budget covers starting the worker interpreter as well as the search.
    # The first match is only reported once the worker is running, so a budget
    # near the cold-start time (about 0.2 s here, slower on loaded CI runners)
    # could kill it before any match and make the partial-result check flaky.
    # The regex never finishes, so the test always waits the full budget; keep
    # it short but far above worst-case startup.
    monkeypatch.setattr(code_files, "SEARCH_TIMEOUT", 5)
    started = time.monotonic()
    result = code_files.search_files(str(tmp_path), "(a+)+$", regex=True)
    assert 4 < time.monotonic() - started < 30
    assert result["ok"] is False
    assert "timed out" in result["error"].lower()
    assert result["matches"] == ["input.txt:1: aaa"]
    assert result["truncated"] is True


def test_regex_worker_completes_normally(tmp_path, monkeypatch):
    (tmp_path / "input.txt").write_text("hello 123\nnope\n")
    monkeypatch.setattr(code_files.shutil, "which", lambda _: None)
    result = code_files.search_files(str(tmp_path), r"\d+", regex=True)
    assert result == {"ok": True, "matches": ["input.txt:1: hello 123"],
                      "truncated": False, "engine": "python"}


def test_plain_search_does_not_spawn_worker(tmp_path, monkeypatch):
    (tmp_path / "input.txt").write_text("(a+)+$\n")
    monkeypatch.setattr(code_files.shutil, "which", lambda _: None)
    def unexpected(*args, **kwargs):
        raise AssertionError("literal search should stay in-process")
    monkeypatch.setattr(code_files.subprocess, "run", unexpected)
    result = code_files.search_files(str(tmp_path), "(a+)+$")
    assert result["ok"]
    assert result["matches"] == ["input.txt:1: (a+)+$"]
