"""All served stylesheets consume one themeable semantic colour layer."""
from pathlib import Path
import re

ASSETS = Path(__file__).resolve().parents[1] / 'src' / 'kova' / 'web_assets'
LEGACY = re.compile(r'--(?:bg|bg-side|surface(?:-[23])?|border|text(?:-[23])?|accent|accent-strong|accent-line|on-accent|ok(?:-soft)?|warn(?:-soft)?|danger(?:-soft)?|code-bg|scroll(?:-hover)?|overlay|hl-\w+|shadow-(?:sm|md|lg))(?![\w-])')
LITERAL = re.compile(r'#[0-9a-fA-F]{3,8}\b|\b(?:rgba?|hsla?|hwb|lab|lch|oklab|oklch|color)\s*\(|(?<![\w-])(?:white|black|red|blue|green|orange|purple|grey|gray)(?![\w-])', re.I)


def plain(css):
    return re.sub(r'/\*.*?\*/', '', css, flags=re.S)


def test_components_use_semantic_colors_only():
    for path in ASSETS.rglob('*.css'):
        if path.name == 'tokens.css':
            continue
        css = plain(path.read_text())
        assert not LEGACY.search(css), path.name
        # Inspect declaration values, not selectors (which may contain ids).
        values = re.findall(r'[\w-]+\s*:\s*([^;{}]+)', css)
        assert not any(LITERAL.search(v) for v in values), path.name
        assert not re.search(r'\[data-theme[^\]]*\]', css), path.name


def test_components_do_not_override_semantic_palette():
    semantic = set(re.findall(r'(--[\w-]+):', (ASSETS / 'tokens.css').read_text().split(':root,')[1]))
    for path in ASSETS.rglob('*.css'):
        if path.name != 'tokens.css':
            declared = set(re.findall(r'(--[\w-]+)\s*:', plain(path.read_text())))
            assert not semantic & declared, path.name
