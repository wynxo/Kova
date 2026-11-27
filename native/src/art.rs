//! Cover art: every catalog entry gets its own live GPU artwork, so no card is a box of text.
//!
//! Bots wear mesh gradients, plugins liquid metal, skills grain gradients. The choice is fixed
//! per name (first-party entries are hand-picked, others hashed), so a card always looks the same.

use crate::data::Entry;
use crate::gfx::{GRAIN_GRADIENT, MESH, METAL};

/// The effect mark and data for an entry's cover.
pub fn cover(entry: &Entry) -> (u32, [f32; 4]) {
    let pick = |n: u32| (hash(&entry.name) % n) as f32;
    match entry.kind.as_str() {
        "bot" => {
            let palette = match entry.name.as_str() {
                "translator" => 1.0,
                "tutor" => 6.0,
                "critic" => 5.0,
                _ => pick(8),
            };
            (MESH, [palette, 0.22, pick(97), 0.14])
        }
        "plugin" => {
            let preset = match entry.name.as_str() {
                "clipboard" => 0.0,
                "morning-briefing" => 1.0,
                "file-watcher" => 3.0,
                _ => pick(5),
            };
            (METAL, [preset, 0.8, 0.0, 0.0])
        }
        _ => {
            let palette = match entry.name.as_str() {
                "explain-error" => 0.0,
                "weekly-review" => 4.0,
                _ => pick(5),
            };
            (GRAIN_GRADIENT, [palette, 0.0, 0.0, 0.0])
        }
    }
}

/// One or two letters to set large over the art.
pub fn monogram(entry: &Entry) -> String {
    let words: Vec<&str> = entry.name.split(['-', '_', ' ']).filter(|w| !w.is_empty()).collect();
    let letters: String = match words.as_slice() {
        [one] => one.chars().take(1).collect(),
        [first, second, ..] => first.chars().take(1).chain(second.chars().take(1)).collect(),
        [] => "K".into(),
    };
    letters.to_uppercase()
}

/// FNV-1a: stable across runs and platforms.
fn hash(text: &str) -> u32 {
    text.bytes().fold(0x811c9dc5u32, |h, b| (h ^ b as u32).wrapping_mul(0x01000193))
}

#[cfg(test)]
mod tests {
    use super::*;

    fn entry(name: &str, kind: &str) -> Entry {
        Entry { name: name.into(), kind: kind.into(), ..Entry::default() }
    }

    #[test]
    fn each_kind_has_its_own_material() {
        assert_eq!(cover(&entry("anything", "bot")).0, MESH);
        assert_eq!(cover(&entry("anything", "plugin")).0, METAL);
        assert_eq!(cover(&entry("anything", "skill")).0, GRAIN_GRADIENT);
    }

    #[test]
    fn covers_are_stable() {
        assert_eq!(cover(&entry("some-bot", "bot")), cover(&entry("some-bot", "bot")));
    }

    #[test]
    fn monograms() {
        assert_eq!(monogram(&entry("morning-briefing", "plugin")), "MB");
        assert_eq!(monogram(&entry("tutor", "bot")), "T");
    }
}
