"""Superscript digits are not numbers: ``str.isdigit`` is true for them, ``int`` raises.

A French keyboard's ``²`` key must not crash a menu, and a schedule typed with
one must be refused with the normal message.
"""

from datetime import datetime

import pytest

from kova.scheduler import parse_schedule
from kova.tools import ToolError
from kova.tui.widgets import Choice


def test_choice_ignores_superscript_key():
    choice = Choice("t", [], [("Yes", True, "y"), ("No", False, "n")])
    assert choice.pick("²") is None
    assert choice.pick("1") is True


def test_schedule_with_superscript_hour_is_refused():
    with pytest.raises(ToolError):
        parse_schedule("daily ²", datetime(2026, 11, 24, 10, 0))
