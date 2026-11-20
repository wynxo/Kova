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
    "C:\\Projects\\someone\\note.md",
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
