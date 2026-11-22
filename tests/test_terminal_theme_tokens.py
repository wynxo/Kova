"""The terminal's roles come from each theme's semantic tokens (#211)."""
from __future__ import annotations

import io
import json
import re
import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest
from rich.console import Console
from typer.testing import CliRunner

from kova import cli, theme, themes
from kova.tui.style import ACCENT, ERROR, MUTED, Palette, basic_pins, colour

ROOT = Path(__file__).resolve().parent.parent
EXAMPLES = sorted((ROOT / "design" / "themes").glob("*.json"))
HEX6 = re.compile(r"^#[0-9a-f]{6}$")


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("KOVA_HOME", str(tmp_path))
    monkeypatch.delenv("KOVA_THEME", raising=False)
    monkeypatch.delenv("NO_COLOR", raising=False)
    (tmp_path / "themes").mkdir()
    return tmp_path


def install(home: Path, theme_id: str, tokens: dict, *, appearance="dark", extends=None, **extra):
    payload = {"id": theme_id, "name": theme_id.title(), "version": "1.0.0",
               "appearance": appearance, "tokens": tokens, **extra}
    if extends:
        payload["extends"] = extends
    path = home / "themes" / f"{theme_id}.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_kova_light_takes_every_role_from_its_tokens():
    tokens = themes.base_tokens("kova-light")
    light = theme.terminal_theme("light")
    assert light.id == "kova-light" and light.light
    for role, token in theme.ROLE_TOKENS.items():
        assert light.roles[role] == tokens[token], role
    for role, index in theme.ROLE_ANSI.items():
        assert light.roles[role] == tokens["terminal"][index], role
    assert light.gradient[0] == tokens["accent.default"]
    assert light.gradient[-1] == tokens["fg.muted"]


def test_kova_dark_keeps_its_terminal_tuned_values():
    dark = theme.terminal_theme("dark")
    assert dark is theme.DARK and theme.terminal_theme("kova-dark") is dark
    assert dark.roles["ACCENT"] == theme.ACCENT and dark.gradient == theme.GRADIENT


@pytest.mark.parametrize("path", EXAMPLES, ids=lambda path: path.stem)
def test_every_example_theme_maps_every_role_to_a_colour(home, path):
    shutil.copy(path, home / "themes" / path.name)
    data = json.loads(path.read_text(encoding="utf-8"))
    mapped = theme.terminal_theme(data["id"])
    assert mapped.id == data["id"] and mapped.appearance == data["appearance"]
    assert set(mapped.roles) == set(theme.ROLES)
    assert all(HEX6.match(value) for value in [*mapped.roles.values(), *mapped.gradient])
    accent = data.get("tokens", {}).get("accent", {}).get("default")
    if accent:
        assert mapped.roles["ACCENT"] == accent.lower()


def test_a_theme_only_moves_the_roles_whose_tokens_it_changes(home):
    install(home, "mint", {"accent": {"default": "#5fd787", "on": "#04201c"}})
    mint = theme.terminal_theme("mint")
    assert mint.roles["ACCENT"] == "#5fd787" and mint.roles["ON_ACCENT"] == "#04201c"
    assert mint.roles["MUTED"] == theme.MUTED and mint.roles["USER"] == theme.USER
    assert mint.gradient[0] == "#5fd787" and mint.gradient[-1] == theme.MUTED

    install(home, "rose-paper", {"status": {"danger": "#9b1c31"}}, appearance="light")
    rose = theme.terminal_theme("rose-paper")
    light = theme.terminal_theme("light")
    assert rose.roles["ERROR"] == "#9b1c31"
    assert {k: v for k, v in rose.roles.items() if k != "ERROR"} == \
        {k: v for k, v in light.roles.items() if k != "ERROR"}


def test_translucent_tokens_are_composited_over_the_page(home):
    install(home, "veil", {"fg": {"muted": "rgba(255, 255, 255, 0.5)"}})
    veil = theme.terminal_theme("veil")
    base = themes.base_tokens("kova-dark")["bg.base"]  # #17191c
    expected = "#" + "".join(f"{round((255 * .5 + int(base[i:i + 2], 16) * .5)):02x}" for i in (1, 3, 5))
    assert veil.roles["MUTED"] == expected
    assert veil.roles["ACCENT_SOFT"] != theme.ACCENT_SOFT


@pytest.mark.parametrize("contents", [None, "{", '{"id": "gone"}'])
def test_missing_or_broken_theme_files_fall_back_to_dark(home, contents):
    if contents is not None:
        (home / "themes" / "gone.json").write_text(contents, encoding="utf-8")
    assert theme.terminal_theme("gone") is theme.DARK


def test_a_changed_theme_file_is_picked_up(home):
    path = install(home, "shift", {"accent": {"default": "#5fd787"}})
    assert theme.terminal_theme("shift").roles["ACCENT"] == "#5fd787"
    install(home, "shift", {"accent": {"default": "#7aa2ff", "hover": "#96b4ff"}})
    assert path.exists()
    assert theme.terminal_theme("shift").roles["ACCENT"] == "#7aa2ff"


def test_palette_paints_roles_in_the_theme_at_every_depth(home):
    install(home, "mint", {"accent": {"default": "#5fd787", "on": "#04201c"}})
    assert Palette(3, theme_name="mint").paint(ACCENT, "x") == "\x1b[38;2;95;215;135mx\x1b[0m"
    assert Palette(2, theme_name="mint").paint(ACCENT, "x") == "\x1b[38;5;78mx\x1b[0m"
    # 16 colours: a coloured accent takes the nearest basic colour, status roles keep theirs.
    assert Palette(1, theme_name="mint").paint(ACCENT, "x") == "\x1b[92mx\x1b[0m"
    assert Palette(1, theme_name="mint").paint(ERROR, "x") == "\x1b[91mx\x1b[0m"
    assert Palette(3, theme_name="mint").paint(MUTED, "x") == Palette(3, theme_name="dark").paint(MUTED, "x")
    chip = Palette(3, theme_name="mint").pill("x", solid=True)
    assert chip == "\x1b[1;38;2;4;32;28;48;2;95;215;135m x \x1b[0m"


def test_basic_pins_swap_the_accent_pair_on_light_themes():
    light = basic_pins(theme.terminal_theme("light"))
    dark = basic_pins(theme.DARK)
    assert (light["ACCENT"], light["ON_ACCENT"]) == (dark["ON_ACCENT"], dark["ACCENT"]) == (30, 97)
    assert light["ERROR"] == dark["ERROR"] == 91


def test_tints_and_the_gradient_are_mixed_in_the_theme(home):
    install(home, "mint", {"accent": {"default": "#5fd787"}})
    p = Palette(3, theme_name="mint")
    assert "38;2;95;215;135" in p.gradient("Kova")  # starts on the theme's accent
    assert "38;2;255;255;255" not in p.gradient("Kova")
    light = Palette(3, theme_name="light")
    tint = light.pill("x", theme.SUCCESS)
    assert tint.startswith("\x1b[38;2;26;110;60;48;2;")  # kova-light's status.success


def test_rich_cli_output_follows_a_theme_file(home, monkeypatch):
    install(home, "sepia", {"fg": {"muted": "#a08060"}})
    monkeypatch.setenv("KOVA_THEME", "sepia")
    out = io.StringIO()
    monkeypatch.setattr(cli, "console", Console(file=out, force_terminal=True,
                                                color_system="truecolor", width=100))
    cli._muted("quiet text")
    assert "38;2;160;128;96" in out.getvalue()
    assert colour("MUTED") == "#a08060"
    assert colour(theme.MUTED) == "#a08060"  # role hex written in kova.theme maps too


# --------------------------------------------------------------- kova themes use
def test_themes_use_saves_the_terminal_theme(home):
    install(home, "mint", {"accent": {"default": "#5fd787"}})
    result = CliRunner().invoke(cli.app, ["themes", "use", "mint"])
    assert result.exit_code == 0, result.output
    assert "Mint is now the terminal's theme" in result.output
    assert 'theme = "mint"' in (home / "config.toml").read_text(encoding="utf-8")
    from kova.tui.style import active_theme
    assert active_theme() == "mint"
    assert Palette(3).terminal.roles["ACCENT"] == "#5fd787"


@pytest.mark.parametrize("name", ["light", "kova-light", "dark"])
def test_themes_use_accepts_the_built_ins(home, name):
    result = CliRunner().invoke(cli.app, ["themes", "use", name])
    assert result.exit_code == 0, result.output
    assert f'theme = "{name}"' in (home / "config.toml").read_text(encoding="utf-8")


def test_themes_use_refuses_a_theme_that_is_not_installed(home):
    (home / "config.toml").write_text('[tui]\ntheme = "light"\n', encoding="utf-8")
    result = CliRunner().invoke(cli.app, ["themes", "use", "nope"])
    assert result.exit_code == 1
    assert "No theme 'nope'" in result.output
    assert 'theme = "light"' in (home / "config.toml").read_text(encoding="utf-8")


@pytest.mark.parametrize("name, preference", [("mint", "mint"), ("kova-light", "light"), ("dark", "dark")])
def test_themes_use_app_also_sets_the_shared_preference(home, monkeypatch, name, preference):
    install(home, "mint", {"accent": {"default": "#5fd787"}})
    saved = {}
    monkeypatch.setattr(cli, "get_client",
                        lambda: SimpleNamespace(set_preferences=lambda body: saved.update(body) or body))
    result = CliRunner().invoke(cli.app, ["themes", "use", name, "--app"])
    assert result.exit_code == 0, result.output
    assert saved == {"theme": preference}
    assert "and the app's" in result.output


def test_themes_use_says_when_the_environment_still_wins(home, monkeypatch):
    monkeypatch.setenv("KOVA_THEME", "dark")
    result = CliRunner().invoke(cli.app, ["themes", "use", "light"])
    assert result.exit_code == 0 and "KOVA_THEME is set" in result.output


def test_themes_list_marks_the_terminal_theme(home, monkeypatch):
    install(home, "mint", {"accent": {"default": "#5fd787"}})
    (home / "config.toml").write_text('[tui]\ntheme = "mint"\n', encoding="utf-8")
    monkeypatch.setattr(cli, "get_client", lambda: SimpleNamespace(themes=lambda: themes.discover(home)))
    result = CliRunner().invoke(cli.app, ["themes", "list"], terminal_width=120)
    assert result.exit_code == 0, result.output
    row = next(line for line in result.output.splitlines() if " mint " in line)
    assert "●" in row
    assert not any("●" in line for line in result.output.splitlines() if "kova-dark" in line)


def test_config_set_validates_theme_ids(home):
    install(home, "mint", {"accent": {"default": "#5fd787"}})
    ok = CliRunner().invoke(cli.app, ["config", "set", "tui.theme", "mint"])
    assert ok.exit_code == 0, ok.output
    bad = CliRunner().invoke(cli.app, ["config", "set", "tui.theme", "nope"])
    assert bad.exit_code == 1 and "Invalid value for tui.theme" in bad.output
