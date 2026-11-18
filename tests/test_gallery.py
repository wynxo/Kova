"""The dev-only component gallery: off by default, and token-only when on."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from kova.config import load_config
from kova.server import create_app

GALLERY = Path(__file__).resolve().parents[1] / "src" / "kova" / "web_pages" / "gallery.html"
# Colours must come from app.css tokens, never a literal in the page.
LITERAL_COLOUR = re.compile(r"#[0-9a-fA-F]{3,8}\b|\brgba?\(|\bhsla?\(")


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("KOVA_HOME", str(tmp_path))
    with TestClient(create_app(load_config())) as client:
        yield client


def test_gallery_is_a_plain_404_without_the_flag(client, monkeypatch):
    monkeypatch.setenv("KOVA_GALLERY", "0")
    assert client.get("/app/gallery").status_code == 404


def test_gallery_serves_the_components_with_the_flag(client, monkeypatch):
    monkeypatch.setenv("KOVA_GALLERY", "1")
    response = client.get("/app/gallery")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    page = GALLERY.read_text(encoding="utf-8")
    for state in ("Default", "Hover", "Focus", "Disabled"):
        assert f">{state}<" in page
    for stylesheet in ("tokens.css", "app.css", "toast.css", "tool-cards.css", "diff.css"):
        assert stylesheet in page
    for class_name in ("btn primary", "btn quiet", "btn danger", "icon-button",
                       "switch", "send", "menu-button", "search", "chat-option-input",
                       "popover", "option", "compose-menu-item", "option-empty",
                       "message user", "message assistant", "markdown", "markdown live",
                       "tool-row failed", "tool-card", "plan-view", "plan-done",
                       "plan-in_progress", "approval-card", "approval-status",
                       "spinner", "activity", "diff-line add", "diff-line del",
                       "file-diff", "toast", "turn-error", "notice-retry"):
        assert class_name in page
    for switcher in ("data-theme-value", "data-density-value", "data-motion-value"):
        assert switcher in page


def test_gallery_uses_tokens_instead_of_literal_colours():
    assert LITERAL_COLOUR.findall(GALLERY.read_text(encoding="utf-8")) == []


def test_gallery_is_not_reachable_through_the_public_asset_mount(client, monkeypatch):
    monkeypatch.setenv("KOVA_GALLERY", "0")
    assert client.get("/app/assets/gallery.html").status_code == 404
