"""The icon sprite is inlined markup, so it gets the same scrutiny as any other input.

These tests are offline on purpose: they read the committed sprite and the pinned icon
list, and never fetch. Fetching is `scripts/vendor_icons.py`'s job, and CI should not
depend on GitHub being reachable to know whether the UI is safe to serve.
"""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
VENDOR = ROOT / "scripts" / "vendor_icons.py"
SPRITE = ROOT / "web_assets" / "vendor" / "icons.svg"


def _load_vendor():
    spec = importlib.util.spec_from_file_location("kova_vendor_icons", VENDOR)
    assert spec and spec.loader, f"cannot import {VENDOR}"
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


vendor = _load_vendor()
sprite = SPRITE.read_text(encoding="utf-8")


def test_sprite_contains_exactly_the_pinned_icons() -> None:
    ids = re.findall(r'<symbol id="icon-([a-z0-9-]+)"', sprite)
    assert ids == sorted(vendor.ICONS), f"sprite ids {ids} do not match ICONS"


def test_icon_list_is_sorted_and_unique() -> None:
    assert list(vendor.ICONS) == sorted(set(vendor.ICONS))


def test_sprite_carries_the_licence_and_pinned_version() -> None:
    assert "ISC License" in sprite
    assert vendor.LUCIDE_VERSION in sprite


def test_sprite_has_nothing_executable() -> None:
    for pattern in vendor._FORBIDDEN:
        assert not pattern.search(sprite), f"sprite matches forbidden {pattern.pattern}"


def test_every_symbol_uses_the_shared_stroke_attributes() -> None:
    symbols = re.findall(r"<symbol\b[^>]*>", sprite)
    assert len(symbols) == len(vendor.ICONS)
    for tag in symbols:
        assert 'viewBox="0 0 24 24"' in tag, tag
        assert 'stroke="currentColor"' in tag, tag
        assert 'fill="none"' in tag, tag


def test_audit_rejects_a_script() -> None:
    poisoned = '<svg viewBox="0 0 24 24"><script>fetch("/x")</script></svg>'
    assert vendor.audit("evil", poisoned)


def test_audit_rejects_an_event_handler() -> None:
    poisoned = '<svg viewBox="0 0 24 24"><path onload="alert(1)" d="M0 0"/></svg>'
    assert vendor.audit("evil", poisoned)


def test_audit_rejects_an_external_reference() -> None:
    poisoned = '<svg viewBox="0 0 24 24"><use href="https://example.com/evil.svg"/></svg>'
    assert vendor.audit("evil", poisoned)


def test_audit_accepts_a_clean_icon() -> None:
    clean = '<svg viewBox="0 0 24 24"><path d="M20 6 9 17l-5-5" /></svg>'
    assert vendor.audit("check", clean) == []


def test_to_symbol_keeps_the_viewbox_and_drops_wrapper_noise() -> None:
    source = (
        '<!-- comment --><svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24">'
        '<title>check</title><path d="M20 6 9 17l-5-5" /></svg>'
    )
    symbol = vendor.to_symbol("check", source)
    assert symbol.strip().startswith('<symbol id="icon-check" viewBox="0 0 24 24"')
    assert "<title>" not in symbol
    assert "<!--" not in symbol
    assert 'd="M20 6 9 17l-5-5"' in symbol
