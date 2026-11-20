"""Pinned project files (#196): relative references, never automatic content."""
from __future__ import annotations

import pytest

from kova import project_pins


def test_relative_paths_are_normalised_and_deduplicated():
    assert project_pins.pins(
        ["notes/a.md", "./src/./app.py", "src\\win\\b.py", "notes/a.md"],
        "/projects/demo",
    ) == ["notes/a.md", "src/app.py", "src/win/b.py"]
    assert project_pins.pins([], None) == []
    assert project_pins.pins(["  ", ""], None) == []


@pytest.mark.parametrize("value", [
    "/etc/passwd",
    "~/private.md",
    "C:\\Users\\someone\\note.md",
    "\\\\server\\share\\file.md",
    "../outside.md",
    "notes/../../outside.md",
    "notes/",
    "notes/\nsecond line",
    "notes/\ttabbed.md",
])
def test_a_pin_must_stay_a_relative_file_inside_the_folder(value):
    with pytest.raises(ValueError):
        project_pins.pins([value], "/projects/demo")


def test_pins_need_a_working_folder_and_stay_bounded():
    with pytest.raises(ValueError, match="working folder"):
        project_pins.pins(["notes/a.md"], None)
    with pytest.raises(ValueError, match="working folder"):
        project_pins.pins(["notes/a.md"], "   ")
    with pytest.raises(ValueError, match="at most"):
        project_pins.pins([f"notes/{number}.md" for number in range(project_pins.MAX_PINS + 1)],
                          "/projects/demo")
    with pytest.raises(ValueError, match="longer than"):
        project_pins.pins(["a" * (project_pins.MAX_PIN_CHARS + 1)], "/projects/demo")


def test_salvage_keeps_valid_references_from_a_hand_edited_list():
    assert project_pins.salvage(
        ["notes/a.md", "/etc/passwd", 7, "../out.md", "notes/a.md"], "/projects/demo",
    ) == ["notes/a.md"]
    assert project_pins.salvage(["notes/a.md"], None) == []
    assert project_pins.salvage("notes/a.md", "/projects/demo") == []


def test_render_lists_references_and_never_claims_their_contents():
    block = project_pins.render(["notes/a.md", "src/b.py"], "/projects/demo")
    assert "notes/a.md" in block and "src/b.py" in block
    assert "Project folder: /projects/demo" in block
    assert "contents are not included" in block
    assert "read_file" in block
    assert project_pins.render([], "/projects/demo") == ""
    assert project_pins.render(["notes/a.md"], None) == ""


def test_render_stays_within_the_prompt_budget():
    files = [f"notes/{number:02d}-" + "x" * 80 + ".md" for number in range(project_pins.MAX_PINS)]
    block = project_pins.render(files, "/projects/demo", budget=700)
    assert len(block.encode("utf-8")) <= 700
    assert "more pinned files not shown" in block
    assert block.count("\n- ") < project_pins.MAX_PINS
