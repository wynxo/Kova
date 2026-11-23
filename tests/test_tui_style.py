"""The terminal chat's look: colour depths, the gradient, the status bar,
setup pickers and the branded ``--help``."""
from __future__ import annotations

import re
from unittest.mock import patch

import pytest

from rich.console import Console
from typer.testing import CliRunner

from kova import cli, theme
from kova.tui import brand
from kova.tui.markdown import MarkdownStream
from kova.tui.prompts import _List, summary
from kova.tui.render import Renderer
from kova.tui.style import ACCENT, Palette, colour_level, downgrade, fg, gradient
from kova.tui.widgets import box, input_box, status_bar, visible, welcome

SGR = re.compile(r"\x1b\[[0-9;]*m")


def plain(text: str) -> str:
    return SGR.sub("", text)


def test_colour_level_reads_the_environment():
    assert colour_level({"NO_COLOR": "1", "COLORTERM": "truecolor"}) == 0
    assert colour_level({"TERM": "dumb"}) == 0
    assert colour_level({"TERM": "xterm-256color", "COLORTERM": "truecolor"}) == 3
    assert colour_level({"TERM": "xterm-kitty"}) == 3
    assert colour_level({"TERM": "xterm-256color"}) == 2
    assert colour_level({"TERM": "linux"}) == 1
    assert colour_level({"TERM": "xterm-256color", "KOVA_COLORS": "16"}) == 1
    assert colour_level({"NO_COLOR": "1", "KOVA_COLORS": "truecolor"}) == 3

    class Pipe:
        def isatty(self):
            return False
    assert colour_level({"TERM": "xterm-256color"}, stream=Pipe()) == 0


def test_theme_colours_degrade_to_the_terminal_depth():
    accent = fg(theme.ACCENT)
    assert downgrade(accent, 3) == accent
    assert downgrade(accent, 2) == "38;5;231"
    assert downgrade(accent, 1) == "97"
    assert downgrade("1;" + fg(theme.USER), 1) == "1;36"
    assert downgrade("48;2;30;29;40", 1) in {str(n) for n in [*range(40, 48), *range(100, 108)]}
    assert Palette(1, theme_name="dark").paint(ACCENT, "x") == "\x1b[97mx\x1b[0m"
    assert Palette(0).paint(ACCENT, "x") == "x" and Palette(False).pill("Bash") == "Bash"


def test_gradient_blends_per_letter_and_vanishes_without_colour():
    shaded = gradient("Kova", Palette(3))
    assert plain(shaded) == "Kova" and shaded.count("\x1b[38;2;") == 4
    assert gradient("Kova", Palette(1, theme_name="dark")) == "\x1b[97mKova\x1b[0m"
    assert gradient("Kova", Palette(1, theme_name="light")) == "\x1b[30mKova\x1b[0m"
    assert gradient("Kova", Palette(0)) == "Kova"


def test_status_bar_fits_any_width_and_drops_the_least_useful_parts():
    p = Palette(3)
    wide = status_bar(p, 120, mode="work", model="local", title="Trip plans", permission="auto",
                      web=False, plan="1/3", stats="141 tokens · 5.0s", cwd="~/code")
    for part in ("work", "local", "Trip plans", "asks before risky steps", "web off", "plan 1/3",
                 "141 tokens", "~/code"):
        assert part in plain(wide), part
    assert "WORK" not in plain(wide) and "\u00b7 local" not in plain(wide)   # words, not chips
    for width in (60, 40, 24):
        line = status_bar(p, width, mode="chat", model="a-very-long-model-name", title="Trip plans",
                          permission="auto", web=False, plan="1/3", stats="141 tokens · 5.0s",
                          cwd="~/code/projects/kova")
        assert visible(line) <= width - 1 and plain(line).startswith(" chat")
    bare = status_bar(Palette(False), 80, mode="work", model="local")
    assert bare.startswith(" work   local") and "\x1b" not in bare


def test_frames_carry_titles_and_footers_and_never_overflow():
    p = Palette(False)
    lines = box(["a" * 50], 20, p, title="Title", footer="hint")
    assert lines[0].startswith("╭─ Title ") and lines[-1].endswith(" hint ─╯")
    assert all(len(line) == 20 for line in lines)
    framed, _caret = input_box("", 0, 40, p, footer="? for shortcuts")
    assert framed[-1].endswith("? for shortcuts ─") and len(framed[-1]) == 40
    assert framed[0] == "─" * 40 and "│" not in "\n".join(framed)        # rules, not a box
    for width in (30, 59, 60, 80, 140):
        for line in welcome(p, width, version="1", model="m" * 40, mode="work",
                            cwd="/very/long/path/" * 6, memory="/mem/" * 10, notes=3):
            assert len(line) <= width, (width, line)


def test_user_turns_sit_on_a_tint_and_code_on_a_panel():
    class Out:
        def __init__(self):
            self.text = ""

        def write(self, text):
            self.text += text

        def flush(self):
            pass

        def isatty(self):
            return False
    out = Out()
    r = Renderer(out, color=3)
    r.user("hello there")
    assert "\x1b[1;" in out.text and "48;2;" in out.text and plain(out.text).startswith("▌ hello")
    chunks = []
    stream = MarkdownStream(chunks.append, 40, Palette(3))
    stream.feed("```python\nprint('hi')\n```\n")
    stream.finish()
    rows = plain("".join(chunks)).splitlines()
    assert rows[0].strip() == "python" and "print('hi')" in rows[1]
    assert all(len(row) == 40 for row in rows)            # the panel spans the reading width
    assert "48;2;30;29;40" in "".join(chunks)


def test_setup_picker_filters_marks_and_summarises():
    state = _List("Provider", ["OpenAI", "Anthropic", "Local server"], multi=False)
    state.type("loc")
    assert state.shown == [2] and state.selected == 2
    text = plain("\n".join(state.render(50, Palette(3))))
    assert "Provider" in text and "❯ Local server" in text and "Filter: loc" in text
    many = _List("Choose models", ["a", "b", "c"], multi=True, marked={0})
    many.move(1)
    many.marked ^= {many.selected}
    assert sorted(many.marked) == [0, 1]
    assert "[x] a" in "\n".join(many.render(40, Palette(False)))
    assert plain(summary(Palette(3), "Provider", "Local server")).startswith("✓ Provider")


def test_help_groups_commands_under_the_wordmark():
    result = CliRunner().invoke(cli.app, ["--help"], terminal_width=100)
    assert result.exit_code == 0, result.output
    text = result.output
    assert brand.LOGO[0] in text and "your private AI" in text
    for panel in ("Chat", "Models", "Memory", "Your data", "Server & updates"):
        assert f"─ {panel} " in text, panel
    assert text.index("─ Chat ") < text.index("─ Server & updates ")
    assert "\x1b[" not in text


def test_light_palette_changes_foreground_and_background_before_downscaling():
    dark = Palette(3, theme_name='dark')
    light = Palette(3, theme_name='light')
    codes = fg(theme.ACCENT) + ';48;2;30;29;40'
    assert dark.paint(codes, 'x') != light.paint(codes, 'x')
    assert '38;2;' in light.paint(codes, 'x') and '48;2;' in light.paint(codes, 'x')
    assert '38;5;' in Palette(2, theme_name='light').paint(codes, 'x')
    assert light.gradient('Kova') != dark.gradient('Kova')


@pytest.mark.parametrize("level, foreground, chip", [
    (1, "30", "1;97;40"),
    (2, "38;5;233", "1;38;5;231;48;5;233"),
    (3, "38;2;17;17;17", "1;38;2;255;255;255;48;2;17;17;17"),
])
def test_light_palette_uses_dark_accent_and_reverses_text_on_solid_chips(level, foreground, chip):
    p = Palette(level, theme_name="light")
    assert p.accent("x") == f"\x1b[{foreground}mx\x1b[0m"
    assert p.pill("x", solid=True) == f"\x1b[{chip}m x \x1b[0m"


@pytest.mark.parametrize("level, chip", [
    (1, "1;30;107"),
    (2, "1;38;5;233;48;5;231"),
    (3, "1;38;2;20;18;28;48;2;246;246;248"),
])
def test_dark_palette_uses_dark_text_on_solid_accent_chips(level, chip):
    p = Palette(level, theme_name="dark")
    assert p.pill("x", solid=True) == f"\x1b[{chip}m x \x1b[0m"


def test_brand_uses_the_chosen_assistant_name(tmp_path):
    from kova.config import load_config, save_config
    console = Console(record=True, width=100)
    with patch("kova.config.kova_home", return_value=tmp_path):
        brand.setup_banner(console, "1.0.0")
        assert "Kova talks to a model server" in console.export_text()
        assert brand.assistant_name() == "Kova"
        cfg = load_config(tmp_path)
        cfg.persona_name = "Nova"
        save_config(cfg)
        console = Console(record=True, width=100)
        brand.setup_banner(console, "1.0.0")
        assert "Nova talks to a model server" in console.export_text()
        assert brand.assistant_name() == "Nova"
