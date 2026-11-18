#!/usr/bin/env python3
"""Draw the Kova app mark and write every icon file from one geometry.

The mark is a rounded tile with a solid, bold "K". It is simple enough to describe
with a handful of shapes, so this script owns the geometry and renders it itself:
no Pillow, no cairo, no Inkscape, only the standard library. The same shapes become
the SVG files and the PNG files, so they can never drift apart.

Outputs:

* ``src/kova/web_assets/icon.svg`` and ``desktop/assets/icon.svg``: the static
  icon, a near-black tile with a white K (favicon, notifications, anything that
  cannot follow the theme).
* ``src/kova/web_assets/mark.svg``: an alpha mask, the tile with the K cut out. The
  web UI paints it with ``background: var(--fg-default)`` through CSS ``mask``, so
  the tile is the text colour and the K shows the surface behind it, in every theme.
* PNGs for the favicon, Apple touch icon, PWA, Electron window/tray and installers,
  plus ``docs/screenshots/app-icon.png`` (the mark from 160px down to 16px on light
  and dark). PNGs are written with IHDR, IDAT and IEND chunks only: no text, time or
  colour-profile chunks, so there is no metadata to strip.

Usage:
    python scripts/build_icons.py            # write all icon files
    python scripts/build_icons.py --check    # exit 1 if any file is stale
"""

from __future__ import annotations

import argparse
import math
import struct
import sys
import zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WEB = ROOT / "src" / "kova" / "web_assets"
DESKTOP = ROOT / "desktop" / "assets"
SHOTS = ROOT / "docs" / "screenshots"

# Static icon colours. Not theme tokens: these files are shown by the OS, the
# browser tab strip and app stores, where Kova's theme does not reach.
TILE = (0x14, 0x12, 0x1C)
INK = (0xFF, 0xFF, 0xFF)
# Swatches for the docs preview strip only (dark and light page backgrounds).
PREVIEW_BACKGROUNDS = ((0x0B, 0x0B, 0x0E), (0xF7, 0xF7, 0xF8))

# Geometry, in a 512 x 512 box.
VIEW = 512
TILE_INSET = 24          # transparent margin around the tile
TILE_RADIUS = 112        # corner radius of the tile

# The K, as one outline: a heavy stem and two straight arms that meet in a notch
# right of the stem. Every vertical and horizontal edge sits on the 16px pixel grid
# (multiples of 32), so at favicon size the stem is a solid 3px column instead of a
# grey blur. Arms are ~2.4px thick at 16px and the counters stay at least 1.75px
# open. Square ends on purpose: round caps turned into grey smudges at 16px.
GLYPH = (
    (128, 128), (224, 128), (224, 192), (280, 128), (384, 128), (272, 256),
    (384, 384), (280, 384), (224, 320), (224, 384), (128, 384),
)
GLYPH_SHAPES = (GLYPH,)


# --------------------------------------------------------------------------- geometry

def rounded_rect(x: float, y: float, w: float, h: float, r: float, steps: int = 24):
    """Polygon for a rounded rectangle, counter-clockwise in screen space."""
    points = []
    corners = ((x + w - r, y + r, -90), (x + w - r, y + h - r, 0),
               (x + r, y + h - r, 90), (x + r, y + r, 180))
    for cx, cy, start in corners:
        for i in range(steps + 1):
            a = math.radians(start + 90 * i / steps)
            points.append((cx + r * math.cos(a), cy + r * math.sin(a)))
    return points


def _signed_area(poly) -> float:
    return sum(x1 * y2 - x2 * y1 for (x1, y1), (x2, y2) in zip(poly, poly[1:] + poly[:1])) / 2


def _oriented(poly):
    """Same orientation for every polygon, so non-zero winding gives their union."""
    poly = list(poly)
    return poly if _signed_area(poly) > 0 else poly[::-1]


# --------------------------------------------------------------------------- raster

def coverage(polys, size: int):
    """Anti-aliased coverage (0..1 per pixel) of the union of ``polys``.

    Scanline fill at ``ss`` sub-rows per pixel with exact horizontal coverage, which
    is plenty for a flat, straight-edged mark.
    """
    scale = size / VIEW
    edges = []
    for poly in map(_oriented, polys):
        pts = [(x * scale, y * scale) for x, y in poly]
        for (x1, y1), (x2, y2) in zip(pts, pts[1:] + pts[:1]):
            if y1 != y2:
                edges.append((x1, y1, x2, y2, 1 if y2 > y1 else -1))
    ss = max(4, 256 // size)
    weight = 1 / ss
    cover = [[0.0] * (size + 1) for _ in range(size)]
    for py in range(size):
        row = cover[py]
        diff = [0.0] * (size + 2)
        for s in range(ss):
            y = py + (s + 0.5) / ss
            hits = []
            for x1, y1, x2, y2, d in edges:
                lo, hi = (y1, y2) if y1 < y2 else (y2, y1)
                if lo <= y < hi:
                    hits.append((x1 + (y - y1) * (x2 - x1) / (y2 - y1), d))
            hits.sort()
            winding = 0
            start = 0.0
            for x, d in hits:
                was = winding
                winding += d
                if was == 0 and winding != 0:
                    start = x
                elif was != 0 and winding == 0:
                    _span(row, diff, max(0.0, start), min(float(size), x), weight)
        run = 0.0
        for i in range(size):
            run += diff[i]
            row[i] += run
    return [[min(1.0, max(0.0, v)) for v in row[:size]] for row in cover]


def _span(row, diff, a: float, b: float, weight: float) -> None:
    if b <= a:
        return
    ia, ib = int(a), int(b)
    if ia == ib:
        row[ia] += (b - a) * weight
        return
    row[ia] += (ia + 1 - a) * weight
    if ib < len(row) - 1:
        row[ib] += (b - ib) * weight
    diff[ia + 1] += weight
    diff[ib] -= weight


def render(size: int, background=None):
    """RGBA rows of the static icon, optionally flattened onto ``background``."""
    tile = coverage([rounded_rect(TILE_INSET, TILE_INSET, VIEW - 2 * TILE_INSET,
                                  VIEW - 2 * TILE_INSET, TILE_RADIUS)], size)
    glyph = coverage(GLYPH_SHAPES, size)
    rows = []
    for ty, gy in zip(tile, glyph):
        out = []
        for t, g in zip(ty, gy):
            g = min(g, t)
            rgb = [c0 + (c1 - c0) * (g / t if t else 0) for c0, c1 in zip(TILE, INK)]
            alpha = t
            if background is not None:
                rgb = [b + (c - b) * alpha for b, c in zip(background, rgb)]
                alpha = 1.0
            out.append(tuple(round(c) for c in rgb) + (round(alpha * 255),))
        rows.append(out)
    return rows


def png_bytes(rows) -> bytes:
    """A minimal RGBA PNG: IHDR, IDAT, IEND. No metadata chunks at all."""
    height, width = len(rows), len(rows[0])
    raw = b"".join(b"\x00" + bytes(v for px in row for v in px) for row in rows)

    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))

    header = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header)
            + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b""))


def strip(sizes=(160, 64, 32, 16), gap: int = 40):
    """The docs preview: the mark at each size, on a dark and a light band."""
    band = max(sizes) + gap
    width = sum(sizes) + gap * (len(sizes) + 1)
    rows = []
    for bg in PREVIEW_BACKGROUNDS:
        canvas = [[bg + (255,) for _ in range(width)] for _ in range(band)]
        x = gap
        for s in sizes:
            icon = render(s, bg)
            top = (band - s) // 2
            for dy, line in enumerate(icon):
                canvas[top + dy][x:x + s] = line
            x += s + gap
        rows.extend(canvas)
    return rows


# --------------------------------------------------------------------------- svg

def _path(poly) -> str:
    head, *rest = poly
    return f"M{head[0]} {head[1]}" + "".join(f"L{x} {y}" for x, y in rest) + "Z"


def _glyph_d() -> str:
    return "".join(_path(p) for p in GLYPH_SHAPES)


def _tile_attrs() -> str:
    side = VIEW - 2 * TILE_INSET
    return f'x="{TILE_INSET}" y="{TILE_INSET}" width="{side}" height="{side}" rx="{TILE_RADIUS}"'


def icon_svg() -> str:
    hexes = ["#" + "".join(f"{c:02x}" for c in col) for col in (TILE, INK)]
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {VIEW} {VIEW}" role="img" aria-label="Kova">\n'
        "  <!-- Generated by scripts/build_icons.py; edit the geometry there. -->\n"
        f'  <rect {_tile_attrs()} fill="{hexes[0]}"/>\n'
        f'  <path d="{_glyph_d()}" fill="{hexes[1]}"/>\n'
        "</svg>\n"
    )


def _tile_d() -> str:
    lo, hi, r = TILE_INSET, VIEW - TILE_INSET, TILE_RADIUS
    arc = f"A{r} {r} 0 0 1"
    return (f"M{lo + r} {lo}H{hi - r}{arc} {hi} {lo + r}V{hi - r}{arc} {hi - r} {hi}"
            f"H{lo + r}{arc} {lo} {hi - r}V{lo + r}{arc} {lo + r} {lo}Z")


def mark_svg() -> str:
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {VIEW} {VIEW}">\n'
        "  <!-- Generated by scripts/build_icons.py. An alpha mask: the tile with the K cut\n"
        "       out. The UI paints it with a theme colour through CSS mask (.kova-mark). -->\n"
        f'  <path d="{_tile_d()}{_glyph_d()}" fill-rule="evenodd"/>\n'
        "</svg>\n"
    )


# --------------------------------------------------------------------------- outputs

PNG_SIZES = {
    WEB / "icon-32.png": 32,
    WEB / "icon-180.png": 180,
    WEB / "icon-192.png": 192,
    WEB / "icon-512.png": 512,
    DESKTOP / "icon-32.png": 32,
    DESKTOP / "icon-64.png": 64,
    DESKTOP / "icon-256.png": 256,
    DESKTOP / "icon.png": 512,
}


def outputs() -> dict[Path, bytes]:
    files: dict[Path, bytes] = {
        WEB / "icon.svg": icon_svg().encode(),
        DESKTOP / "icon.svg": icon_svg().encode(),
        WEB / "mark.svg": mark_svg().encode(),
    }
    cache: dict[int, bytes] = {}
    for path, size in PNG_SIZES.items():
        if size not in cache:
            cache[size] = png_bytes(render(size))
        files[path] = cache[size]
    files[SHOTS / "app-icon.png"] = png_bytes(strip())
    return files


def _pixels(data: bytes) -> bytes:
    """IHDR plus the decompressed image data: what a PNG shows, minus how zlib packed it.

    Different zlib builds (system Python vs. uv's bundled one) compress the same rows
    to different bytes, so freshness compares pixels, not files.
    """
    out, offset = [], 8
    while offset < len(data):
        length = int.from_bytes(data[offset:offset + 4], "big")
        kind, body = data[offset + 4:offset + 8], data[offset + 8:offset + 8 + length]
        out.append(zlib.decompress(body) if kind == b"IDAT" else kind + body)
        offset += length + 12
    return b"".join(out)


def is_fresh(path: Path, data: bytes) -> bool:
    if not path.exists():
        return False
    current = path.read_bytes()
    # A Windows checkout may turn the SVGs' line endings into CRLF.
    if current == data or current.replace(b"\r\n", b"\n") == data:
        return True
    return path.suffix == ".png" and current[:8] == data[:8] and _pixels(current) == _pixels(data)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--check", action="store_true", help="exit 1 if any icon file is stale")
    args = parser.parse_args(argv)
    stale = []
    for path, data in outputs().items():
        if is_fresh(path, data):
            continue
        if args.check:
            stale.append(path.relative_to(ROOT))
        else:
            path.write_bytes(data)
            print(f"wrote {path.relative_to(ROOT)}")
    if stale:
        print("Stale icon files (run python scripts/build_icons.py):", file=sys.stderr)
        for path in stale:
            print(f"  {path}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
