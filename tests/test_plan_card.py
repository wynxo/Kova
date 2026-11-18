"""The pinned plan card (#193).

The behaviour is exercised in ``tests/web_plan_card.mjs`` with Node and no
browser. These checks keep the wiring and the design contract: the card loads
before app.js, the plan event path feeds it, and every visual value is a token.
"""

from __future__ import annotations

from pathlib import Path

ASSETS = Path(__file__).resolve().parents[1] / "src" / "kova" / "web_assets"
INDEX = (ASSETS / "index.html").read_text(encoding="utf-8")
APP_JS = (ASSETS / "app.js").read_text(encoding="utf-8")
CARD_JS = (ASSETS / "plan-card.js").read_text(encoding="utf-8")
CARD_CSS = (ASSETS / "plan-card.css").read_text(encoding="utf-8")


def test_plan_card_loads_before_app_js_and_is_cached_offline() -> None:
    assert "/app/assets/plan-card.css" in INDEX
    assert INDEX.index("/app/assets/plan-card.js") < INDEX.index("/app/assets/app.js")
    shell = (ASSETS / "sw.js").read_text(encoding="utf-8")
    assert "'/app/assets/plan-card.js'" in shell
    assert "'/app/assets/plan-card.css'" in shell


def test_the_same_plan_events_feed_the_card_and_the_transcript() -> None:
    """One source of truth: the `plan` branch passes its own transcript node."""
    assert "window.KovaPlanCard?.update(event.items, next)" in APP_JS
    assert "window.KovaPlanCard?.finish()" in APP_JS
    assert "window.KovaPlanCard?.reset()" in APP_JS
    assert "function update(nextItems, transcriptNode)" in CARD_JS
    assert "transcript.hidden = false" in CARD_JS, "the transcript takes the plan back"


def test_step_changes_are_announced_once_not_per_event() -> None:
    assert "setAttribute('role', 'status')" in CARD_JS
    assert "announced" in CARD_JS and "signature" in CARD_JS, \
        "the card compares step transitions before speaking"


def test_dismiss_is_for_this_turn_only() -> None:
    assert "'Hide the plan for this turn'" in CARD_JS
    assert "dismissed = false" in CARD_JS, "finish makes the card available again"


def test_phone_layout_is_a_line_that_opens_as_a_sheet() -> None:
    assert "@media (max-width:720px)" in CARD_CSS
    phone = CARD_CSS[CARD_CSS.index("@media (max-width:720px)"):]
    assert "position:absolute" in phone and "bottom:calc(100%" in phone, \
        "the list rises from the pinned row instead of the composer column's containment"
    assert ".plan-card-step" in CARD_CSS and "text-overflow:ellipsis" in CARD_CSS, \
        "the pinned row stays one line"


def test_motion_uses_the_shared_tokens() -> None:
    assert "var(--motion-base)" in CARD_CSS and "var(--motion-fast)" in CARD_CSS
    assert "var(--motion-slow)" in CARD_CSS
    # The global data-motion block owns reduced/off; a component must not loop.
    assert "infinite" not in CARD_CSS
