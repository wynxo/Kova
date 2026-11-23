"""Terminal surfaces must resolve the theme when they render, not at import."""
from __future__ import annotations

import io
from types import SimpleNamespace

import pytest
from rich.console import Console
from typer.testing import CliRunner

from kova import cli, ui
from kova.tui import brand
from kova.tui.prompts import _List
from kova.tui.style import ACCENT, Palette, colour


@pytest.fixture(autouse=True)
def terminal_home(tmp_path, monkeypatch):
    monkeypatch.setenv("KOVA_HOME", str(tmp_path))
    monkeypatch.delenv("KOVA_THEME", raising=False)
    monkeypatch.delenv("NO_COLOR", raising=False)
    return tmp_path


def capture(renderable, *, terminal=True, no_color=False):
    out = io.StringIO()
    console = Console(file=out, width=120, force_terminal=terminal,
                      color_system="truecolor" if terminal else None, no_color=no_color)
    console.print(renderable)
    return out.getvalue()


@pytest.mark.parametrize("source", ["config", "env"])
@pytest.mark.parametrize("surface", ["brand", "status", "chat"])
def test_rich_surfaces_use_the_light_palette(source, surface, terminal_home, monkeypatch):
    if source == "config":
        (terminal_home / "config.toml").write_text('[tui]\ntheme = "light"\n')
    else:
        monkeypatch.setenv("KOVA_THEME", "light")
    if surface == "brand":
        renderable = brand.heading("Details", "Secondary text")
    elif surface == "status":
        renderable = cli._status_rows(SimpleNamespace(active="demo", privacy_local_only=False),
                                      None, notes=None, reminders=None)
    else:
        turn = ui._Turn(no_think=False, show_thinking=False)
        turn.accept({"type": "text", "text": "Hello"})
        renderable = turn.render()
    raw = capture(renderable)
    if surface != "status":
        # The chat's speaker tile fills its one cell with the accent and writes the
        # initial in the on-accent ink, so the token may appear as a background there.
        assert "17;17;17" in raw  # kova-light's accent token, not the near-white dark-theme accent
    else:
        assert "38;2;85;85;79" in raw  # kova-light's fg.muted
    assert "38;2;246;246;248" not in raw
    assert "38;2;138;143;152" not in raw


def test_environment_overrides_config_and_changes_without_reimport(terminal_home, monkeypatch):
    (terminal_home / "config.toml").write_text('[tui]\ntheme = "light"\n')
    assert "38;2;17;17;17" in capture(brand.heading("Theme"))
    monkeypatch.setenv("KOVA_THEME", "dark")
    assert "38;2;246;246;248" in capture(brand.heading("Theme"))
    monkeypatch.delenv("KOVA_THEME")
    (terminal_home / "config.toml").write_text('[tui]\ntheme = "dark"\n')
    assert "38;2;246;246;248" in capture(brand.heading("Theme"))


def test_default_palette_and_setup_picker_respect_config(terminal_home):
    (terminal_home / "config.toml").write_text('[tui]\ntheme = "light"\n')
    p = Palette(3)
    assert p.paint(ACCENT, "x") == "\x1b[38;2;17;17;17mx\x1b[0m"
    rows = "\n".join(_List("Provider", ["Demo"], multi=False).render(60, p))
    assert "38;2;17;17;17" in rows
    assert Palette(0).paint(ACCENT, "x") == "x"
    assert Palette(1).paint(ACCENT, "x") == "\x1b[30mx\x1b[0m"


def test_setup_banner_and_gradient_follow_light_theme(monkeypatch):
    monkeypatch.setenv("KOVA_THEME", "light")
    out = io.StringIO()
    console = Console(file=out, force_terminal=True, color_system="truecolor", width=120)
    brand.setup_banner(console, "1.0")
    raw = out.getvalue()
    assert "38;2;17;17;17" in raw  # first gradient stop: the light accent token
    assert "38;2;85;85;79" in raw
    assert "38;2;255;255;255" not in raw


@pytest.mark.parametrize("args", [["--help"], ["model", "--help"]])
@pytest.mark.parametrize("source", ["config", "env"])
def test_help_resolves_theme_at_invocation(args, source, terminal_home, monkeypatch):
    from typer import rich_utils

    # Typer constructs its own console for help. Pin its colour depth too:
    # Windows auto-detection may downsample despite COLORTERM/TERM overrides.
    monkeypatch.setattr(rich_utils, "COLOR_SYSTEM", "truecolor")
    monkeypatch.setattr(cli, "console", Console(force_terminal=True, color_system="truecolor", width=120))
    for name, expected in [("light", "38;2;224;224;221"), ("dark", "38;2;58;61;69")]:
        if source == "env":
            monkeypatch.setenv("KOVA_THEME", name)
        else:
            (terminal_home / "config.toml").write_text(f'[tui]\ntheme = "{name}"\n')
        result = CliRunner().invoke(cli.app, args, color=True, terminal_width=120)
        assert result.exit_code == 0, result.output
        assert expected in result.output
        if name == "light":
            assert "38;2;138;143;152" not in result.output


@pytest.mark.parametrize("up", [True, False])
def test_status_command_themes_the_whole_card(up, terminal_home, monkeypatch):
    from kova.client import KovaError

    (terminal_home / "config.toml").write_text('[tui]\ntheme = "light"\n')

    def health(*, probe):
        if not up:
            raise KovaError(None, "offline")
        return {"version": "1.0", "model": "demo", "model_ok": True}

    client = SimpleNamespace(health=health, models_list=lambda: [], memory_list=lambda: [],
                             reminders_list=lambda: [], usage=lambda days: {"today": {}})
    monkeypatch.setattr(cli, "KovaClient", lambda *args: client)
    monkeypatch.setattr(cli, "console", Console(force_terminal=True, color_system="truecolor", width=120))
    result = CliRunner().invoke(cli.app, ["status"], color=True)
    assert result.exit_code == 0, result.output
    assert "38;2;85;85;79" in result.output
    assert "38;2;138;143;152" not in result.output
    assert "38;2;17;17;17" in result.output if up else "38;2;179;38;47" in result.output


def test_light_output_is_still_plain_when_piped(monkeypatch):
    monkeypatch.setenv("KOVA_THEME", "light")
    assert "\x1b[" not in capture(brand.wordmark(), terminal=False)
    raw = capture(brand.wordmark(), no_color=True)
    assert "38;2;" not in raw


def test_prompt_styles_resolve_foregrounds_and_backgrounds_at_session_start(monkeypatch):
    from prompt_toolkit.styles import DEFAULT_ATTRS

    monkeypatch.setenv("KOVA_THEME", "light")
    style = ui._chat_style()
    prompt = style.get_attrs_for_style_str("class:prompt", DEFAULT_ATTRS)
    menu = style.get_attrs_for_style_str("class:completion-menu.completion.current", DEFAULT_ATTRS)
    assert prompt.color == "1d6b62"
    assert menu.bgcolor == "111111"
    assert menu.color == "ffffff"
    monkeypatch.setenv("KOVA_THEME", "dark")
    menu = ui._chat_style().get_attrs_for_style_str("class:completion-menu.completion.current", DEFAULT_ATTRS)
    assert menu.bgcolor == "f6f6f8" and menu.color == "14121c"


@pytest.mark.parametrize("contents", [None, "invalid = [", '[tui]\ntheme = "unknown"\n', 'tui = 42'])
def test_theme_falls_back_without_creating_or_rewriting_config(contents, terminal_home):
    path = terminal_home / "config.toml"
    if contents is not None:
        path.write_text(contents)
    assert colour("ACCENT") == "#f6f6f8"
    if contents is None:
        assert not path.exists()
    else:
        assert path.read_text() == contents


def test_full_screen_chat_uses_the_same_environment_precedence(terminal_home, monkeypatch):
    from kova.config import load_config, save_config
    from kova.tui.app import ChatApp

    cfg = load_config()
    cfg.tui_theme = "light"
    save_config(cfg)
    client = SimpleNamespace()
    assert ChatApp(client, None, out=io.StringIO(), color=3,
                   history_path=terminal_home / "history").p.theme_name == "light"
    monkeypatch.setenv("KOVA_THEME", "dark")
    assert ChatApp(client, None, out=io.StringIO(), color=3,
                   history_path=terminal_home / "history").p.theme_name == "dark"
