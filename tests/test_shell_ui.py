"""The chat-app shell: app switch, Chat | Work at the top, fork, context ring, bot characters."""
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ASSETS = Path(__file__).resolve().parents[1] / "src" / "kova" / "web_assets"
HTML = (ASSETS / "index.html").read_text(encoding="utf-8")


def test_the_mode_switch_sits_in_the_top_bar_not_the_model_panel():
    topbar = HTML[HTML.index('<header class="topbar">'):HTML.index("</header>")]
    panel = HTML[HTML.index('id="chat-options-panel"'):HTML.index('id="send"')]
    assert 'id="mode-switch"' in topbar and 'id="mode-fork"' in topbar
    assert 'id="mode-switch"' not in panel
    # Chat comes first, as in the familiar chat apps.
    switch = topbar[topbar.index('id="mode-switch"'):]
    assert switch.index('data-value="chat"') < switch.index('data-value="work"')


def test_the_side_list_opens_with_the_app_switch_and_a_plus_for_new_chats():
    assert 'id="app-switch"' in HTML and 'id="app-menu"' in HTML
    for app in ("kova", "code", "bots"):
        assert f'data-app="{app}"' in HTML
    # Kova Code is not built yet, so the menu says so instead of opening a fake screen.
    code = re.search(r'<button[^>]*data-app="code"[^>]*>', HTML).group(0)
    assert 'aria-disabled="true"' in code
    new_chat = HTML[HTML.index('id="new-chat"'):HTML.index("</button>", HTML.index('id="new-chat"'))]
    assert "i-plus" in new_chat and "i-square-pen" not in new_chat


def test_the_context_ring_has_a_setting_and_a_label():
    assert 'id="context-ring"' in HTML and 'aria-label="Context used"' in HTML
    assert 'id="context-ring-toggle"' in HTML and 'role="switch"' in HTML
    assert "host: 'context-ring-toggle'" in (ASSETS / "settings.js").read_text(encoding="utf-8")


def test_new_scripts_and_styles_are_loaded_and_cached_offline():
    sw = (ASSETS / "sw.js").read_text(encoding="utf-8")
    for name in ("shell.js", "shell.css", "characters.js"):
        assert name in HTML and f"'/app/assets/{name}'" in sw
    assert HTML.index("characters.js") < HTML.index("bots.js") < HTML.index("shell.js") < HTML.index("app.js")


def test_character_motion_requires_full_motion():
    css = (ASSETS / "shell.css").read_text(encoding="utf-8")
    animated = re.findall(r"([^{}]+)\{[^}]*animation:character-", css)
    assert animated and all('html[data-motion="full"]' in rule for rule in animated)


@pytest.mark.skipif(not shutil.which("node"), reason="node is not installed")
def test_a_bot_keeps_the_same_face_and_faces_differ():
    script = (
        "globalThis.document={createElementNS:()=>({setAttribute(){},style:{setProperty(){}}})};"
        f"require({str(ASSETS / 'characters.js')!r});"
        "const t=globalThis.KovaCharacters.traits;"
        "const a=JSON.stringify(t('henry')),b=JSON.stringify(t('henry'));"
        "const shapes=new Set(['a','b','c','d','e','f','g','h','i','j'].map(x=>t(x).shape));"
        "console.log(a===b, shapes.size>2);"
    )
    out = subprocess.run(["node", "-e", script], capture_output=True, text=True, check=True).stdout
    assert out.strip() == "true true"
