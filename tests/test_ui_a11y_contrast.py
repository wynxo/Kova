"""Accessibility pass across themes (#201): the whole set, not just pairs.

The pair-level contrast gate (``test_tokens_contrast.py``) checks each built-in
theme's token combinations. These guards check what it cannot express alone:

- every built-in **and** gallery theme passes the contrast gate as a set, so a
  theme added to the gallery cannot ship below WCAG AA unnoticed;
- the shell honours ``prefers-contrast`` and ``forced-colors``;
- interactive controls meet the 24px (desktop) / 44px (touch) target floor;
- the viewport never disables zoom, so 200% reflow stays possible.
"""

from __future__ import annotations

import json
from pathlib import Path

from kova import themes
from kova.theme_contrast import contrast_failures

ROOT = Path(__file__).resolve().parent.parent
ASSETS = ROOT / "src" / "kova" / "web_assets"
GALLERY = ROOT / "src" / "kova" / "theme_gallery.json"


def _gallery_documents() -> dict[str, dict]:
    return json.loads(GALLERY.read_text(encoding="utf-8"))


def _theme_tokens(theme_id: str) -> dict[str, str]:
    if theme_id in themes.BUILTIN:
        return themes.base_tokens(theme_id)
    return themes.resolve(_gallery_documents()[theme_id])


def test_every_built_in_and_gallery_theme_passes_the_contrast_gate() -> None:
    failures: list[str] = []
    ids = (*themes.BUILTIN, *_gallery_documents())
    for theme_id in ids:
        tokens = _theme_tokens(theme_id)
        terminal = tokens.pop("terminal", None)
        for message in contrast_failures(tokens, terminal):
            failures.append(f"{theme_id}: {message}")
    assert not failures, "\n".join(failures)


def test_the_shell_respects_prefers_contrast_and_forced_colors() -> None:
    css = (ASSETS / "app.css").read_text(encoding="utf-8")
    assert "@media (prefers-contrast: more)" in css, "app.css has no prefers-contrast block"
    assert "@media (forced-colors: active)" in css, "app.css has no forced-colors block"


def test_interactive_controls_meet_minimum_target_sizes() -> None:
    css = (ASSETS / "app.css").read_text(encoding="utf-8")
    mobile = (ASSETS / "mobile.css").read_text(encoding="utf-8")
    # Desktop: the smallest interactive controls are 28px (compact button) and
    # 32px (icon button), above the 24px floor; the ordinary button is 34px.
    assert "min-height:34px" in css
    assert "min-height:28px" in css
    assert "width:32px" in css and "height:32px" in css
    # Touch: 44px is the platform floor, applied to every control that needs it.
    assert "--touch-target:max(44px" in mobile
    assert "44px is the platform floor" in mobile


def test_viewport_never_disables_zoom() -> None:
    for page in ("index.html", "token-sheet.html"):
        html = (ASSETS / page).read_text(encoding="utf-8")
        assert "user-scalable=no" not in html, f"{page} disables zoom"
        assert "maximum-scale" not in html, f"{page} caps zoom"
