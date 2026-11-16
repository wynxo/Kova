"""The committed stylesheet must be exactly what the generator produces.

``build_tokens.py --check`` used to validate contrast and shape only, so a token
edit without a rerun passed every gate while the app served the old palette
(#232). The check now regenerates in memory and compares bytes, and these tests
keep both halves honest: the committed file matches its source, and the
documented command really fails when it does not.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _build():
    path = ROOT / "scripts" / "build_tokens.py"
    spec = importlib.util.spec_from_file_location("kova_build_tokens", path)
    assert spec and spec.loader, f"cannot import {path}"
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


build = _build()


def test_generated_stylesheet_exists_where_the_app_serves_it() -> None:
    assert build.CSS_PATH.exists(), f"{build.CSS_PATH} is missing"


def test_committed_stylesheet_matches_the_generator() -> None:
    expected = build.render_css(build.load_tokens())
    actual = build.CSS_PATH.read_text(encoding="utf-8")
    assert actual == expected, (
        f"{build.CSS_PATH.name} is stale: run `python scripts/build_tokens.py` and commit the result"
    )


def test_check_passes_on_the_committed_tree(capsys) -> None:
    assert build.main(["--check"]) == 0
    assert "stylesheet matches the generator" in capsys.readouterr().out


def test_check_fails_on_a_stale_stylesheet(tmp_path, monkeypatch, capsys) -> None:
    stale = tmp_path / "tokens.css"
    stale.write_text(
        build.CSS_PATH.read_text(encoding="utf-8") + "--stale: 1;\n", encoding="utf-8"
    )
    monkeypatch.setattr(build, "CSS_PATH", stale)
    assert build.main(["--check"]) == 1
    err = capsys.readouterr().err
    assert "is stale" in err
    assert "first at line" in err
    assert "python scripts/build_tokens.py" in err


def test_check_fails_when_the_stylesheet_is_missing(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setattr(build, "CSS_PATH", tmp_path / "tokens.css")
    assert build.main(["--check"]) == 1
    assert "is missing" in capsys.readouterr().err
