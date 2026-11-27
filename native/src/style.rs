//! Kova's native look: a quiet near-black canvas (after Astryx's Neutral theme), one cool accent,
//! and type and spacing on a small fixed scale. Colour helpers follow Rune's showreel examples.

use rune::color::Color;
use rune::el::Shadow;
use rune::{El, TextStyle, text};

/// An sRGB hex colour.
pub fn c(hex: u32) -> Color {
    let ch = |shift: u32| rune::color::srgb_decode(((hex >> shift) & 0xff) as f32 / 255.0);
    Color { r: ch(16), g: ch(8), b: ch(0), a: 1.0 }
}

/// An sRGB hex colour at `a` opacity.
pub fn ca(hex: u32, a: f32) -> Color {
    c(hex).alpha(a)
}

// Surfaces, darkest first.
pub const CANVAS: u32 = 0x09090b;
pub const SIDEBAR: u32 = 0x0d0d10;
pub const SURFACE: u32 = 0x131317;
pub const RAISED: u32 = 0x1a1a1f;
// Ink.
pub const TEXT: u32 = 0xf2f2f4;
pub const MUTED: u32 = 0xa3a3ad;
pub const FAINT: u32 = 0x6e6e78;
// Signals (Astryx Neutral, dark).
pub const ACCENT: u32 = 0x6d9cfe;
pub const SUCCESS: u32 = 0x64af4c;
pub const DANGER: u32 = 0xff705d;

/// A one-point hairline on dark surfaces.
pub fn hair() -> Color {
    ca(0xffffff, 0.07)
}

pub fn t(s: impl Into<String>, size: f32, weight: f32, color: u32) -> El {
    text(s).font(TextStyle::new(size, weight)).color(c(color))
}

pub fn ta(s: impl Into<String>, size: f32, weight: f32, color: u32, alpha: f32) -> El {
    text(s).font(TextStyle::new(size, weight)).color(ca(color, alpha))
}

pub fn mono(s: impl Into<String>, size: f32, color: u32) -> El {
    text(s).font(TextStyle::new(size, 500.0).mono()).color(c(color))
}

/// A soft, layered drop shadow: a tight contact shadow plus a wide ambient one.
pub fn lift(el: El, depth: f32) -> El {
    el.box_shadow(Shadow { x: 0.0, y: 1.0 * depth, blur: 2.0 * depth, spread: 0.0, color: ca(0x000000, 0.35), inset: false })
        .box_shadow(Shadow { x: 0.0, y: 10.0 * depth, blur: 36.0 * depth, spread: -4.0 * depth, color: ca(0x000000, 0.55), inset: false })
}

/// A faint top-edge highlight, as light catching a bevel.
pub fn sheen(el: El) -> El {
    el.box_shadow(Shadow { x: 0.0, y: 1.0, blur: 0.0, spread: 0.0, color: ca(0xffffff, 0.06), inset: true })
}
