"""The chat modes: how much Kova may do in a conversation."""

import kova.modes as modes


def test_known_modes_are_chat_and_work():
    assert modes.MODES == ("chat", "work")


def test_default_mode_is_work_and_known():
    assert modes.DEFAULT_MODE == "work"
    assert modes.DEFAULT_MODE in modes.MODES


def test_chat_mode_has_no_tools_except_web():
    # "chat" is the constrained mode; "work" is the full-featured default.
    assert "chat" in modes.MODES
    assert modes.MODES.index("chat") < modes.MODES.index("work")
