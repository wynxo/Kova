"""All served stylesheets consume one themeable semantic colour layer."""
from pathlib import Path
import re

ASSETS = Path(__file__).resolve().parents[1] / 'src' / 'kova' / 'web_assets'
LEGACY = re.compile(r'--(?:bg|bg-side|surface(?:-[23])?|border|text(?:-[23])?|accent|accent-strong|accent-line|on-accent|ok(?:-soft)?|warn(?:-soft)?|danger(?:-soft)?|code-bg|scroll(?:-hover)?|overlay|hl-\w+|shadow-(?:sm|md|lg))(?![\w-])')
NAMED_COLORS = """
aliceblue antiquewhite aqua aquamarine azure beige bisque black blanchedalmond blue
blueviolet brown burlywood cadetblue chartreuse chocolate coral cornflowerblue cornsilk
crimson cyan darkblue darkcyan darkgoldenrod darkgray darkgreen darkgrey darkkhaki
darkmagenta darkolivegreen darkorange darkorchid darkred darksalmon darkseagreen
darkslateblue darkslategray darkslategrey darkturquoise darkviolet deeppink deepskyblue
dimgray dimgrey dodgerblue firebrick floralwhite forestgreen fuchsia gainsboro ghostwhite
gold goldenrod gray green greenyellow grey honeydew hotpink indianred indigo ivory khaki
lavender lavenderblush lawngreen lemonchiffon lightblue lightcoral lightcyan
lightgoldenrodyellow lightgray lightgreen lightgrey lightpink lightsalmon lightseagreen
lightskyblue lightslategray lightslategrey lightsteelblue lightyellow lime limegreen linen
magenta maroon mediumaquamarine mediumblue mediumorchid mediumpurple mediumseagreen
mediumslateblue mediumspringgreen mediumturquoise mediumvioletred midnightblue mintcream
mistyrose moccasin navajowhite navy oldlace olive olivedrab orange orangered orchid
palegoldenrod palegreen paleturquoise palevioletred papayawhip peachpuff peru pink plum
powderblue purple rebeccapurple red rosybrown royalblue saddlebrown salmon sandybrown
seagreen seashell sienna silver skyblue slateblue slategray slategrey snow springgreen
steelblue tan teal thistle tomato turquoise violet wheat white whitesmoke yellow yellowgreen
""".split()
LITERAL = re.compile(
    r"#[0-9a-fA-F]{3,8}\b|\b(?:rgba?|hsla?|hwb|lab|lch|oklab|oklch|color)\s*\("
    r"|(?<![\w-])(?:" + "|".join(NAMED_COLORS) + r")(?![\w-])", re.I)


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


def test_literal_guard_covers_named_and_functional_colors():
    for value in ("#abc", "#12345678", "rgb(0 0 0)", "oklch(50% .1 20)",
                  "rebeccapurple", "LightSteelBlue"):
        assert LITERAL.search(value), value
    for value in ("var(--status-danger)", "currentColor", "transparent",
                  "color-mix(in srgb, var(--accent-default) 10%, transparent)"):
        assert not LITERAL.search(value), value
