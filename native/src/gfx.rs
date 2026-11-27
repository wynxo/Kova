//! The compositor: every `El::effect` slot becomes one rune-fx layer drawn on the GPU in the same
//! pass as the UI. The slot's mark says which effect, `effect_data` carries its few numbers, and the
//! slot's presented rect and radius are the shape.
//!
//! Adapted from Rune's `examples/showreel/src/gfx.rs` (MIT, © Anomaly).

use rune::compose::{Compositor, DeviceKey, Frame};
use rune::render::{EffectPhase, EffectSite, KIND_EFFECT};
use rune_fx::*;

/// Liquid metal. data: [preset 0 chrome / 1 gold / 2 rose / 3 gunmetal / 4 ice, flow, -, -].
pub const METAL: u32 = 1;
/// Mesh gradient. data: [palette, speed, seed, drift].
pub const MESH: u32 = 2;
/// Grain gradient. data: [palette, -, -, -].
pub const GRAIN_GRADIENT: u32 = 3;
/// A soft glow around a card. data: [colour index, intensity, radius, -].
pub const GLOW: u32 = 4;
/// A light sweep across a surface. data: [intensity, period, band, -].
pub const SHIMMER: u32 = 5;
/// Northern lights. data: [intensity, speed, -, -].
pub const AURORA: u32 = 6;
/// Fine film grain over a surface. data: [amount, fps, -, -].
pub const GRAIN: u32 = 7;
/// Drifting glossy blobs. data: [palette, -, -, -].
pub const METABALLS: u32 = 8;

/// Colours by index, for effects that take one.
pub const COLORS: [u32; 6] = [0x6d9cfe, 0x22d3ee, 0x9b7bff, 0x34d399, 0xffb547, 0xff5ca8];

fn color(i: f32) -> Rgb {
    rgb(COLORS[(i.max(0.0) as usize).min(COLORS.len() - 1)])
}

/// The mesh gradients Kova uses for bots and the hero, by palette index.
pub fn mesh(palette: u32) -> MeshGradientParams {
    match palette {
        1 => MeshGradientParams::lagoon(),
        2 => MeshGradientParams::orchid(),
        3 => MeshGradientParams::graphite(),
        // Kova night: deep navy into Astryx blue and violet.
        4 => MeshGradientParams { colors: Stops::hex(&[0x070b1f, 0x2a4bd8, 0x6d9cfe, 0x7b4dff, 0x0b1030]), ..MeshGradientParams::dusk() },
        // Ember: wine into amber.
        5 => MeshGradientParams { colors: Stops::hex(&[0x1f0710, 0xd2364b, 0xffb547, 0x6b1236, 0xff7a1a]), ..MeshGradientParams::dusk() },
        // Jade.
        6 => MeshGradientParams { colors: Stops::hex(&[0x04201b, 0x1fb88a, 0xc9f7a5, 0x0b6b5e]), ..MeshGradientParams::dusk() },
        // Rose quartz.
        7 => MeshGradientParams { colors: Stops::hex(&[0x1a0d2b, 0xff6fb5, 0x8a6bff, 0xffd6ea, 0x2e1d5c]), ..MeshGradientParams::dusk() },
        _ => MeshGradientParams::dusk(),
    }
}

fn grain_gradient(palette: u32) -> GrainGradientParams {
    match palette {
        1 => GrainGradientParams::halo(),
        2 => GrainGradientParams::sweep(),
        3 => GrainGradientParams { colors: Stops::hex(&[0x07131f, 0x0f5b78, 0x4fd1c5, 0xe6fffb]), ..GrainGradientParams::ember() },
        4 => GrainGradientParams { colors: Stops::hex(&[0x120a24, 0x5b3cc4, 0xb69cff, 0xf3eaff]), ..GrainGradientParams::ember() },
        _ => GrainGradientParams::ember(),
    }
}

fn layer(kind: u32, d: [f32; 4], shape: Shape, t: f32) -> Option<FxLayer> {
    let fx = match kind {
        METAL => {
            let p = match d[0] as u32 {
                1 => LiquidMetalParams::gold(),
                2 => LiquidMetalParams::rose(),
                3 => LiquidMetalParams::gunmetal(),
                4 => LiquidMetalParams { light: rgb(0xd6e8ff), iridescence: 0.3, scale: 240.0, bevel: 3.5, dome: 0.2, contrast: 1.15, ..LiquidMetalParams::chrome() },
                _ => LiquidMetalParams::chrome(),
            };
            let p = if d[1] > 0.0 { LiquidMetalParams { flow: p.flow * d[1], ..p } } else { p };
            Fx::fill(p.anchor(FillAnchor::Element))
        }
        MESH => {
            let mut p = mesh(d[0] as u32);
            if d[1] > 0.0 {
                p.speed = d[1];
            }
            if d[3] > 0.0 {
                p.drift = d[3];
            }
            return Some(Fx::fill(p).on(shape).at(t).seed(d[2] as u32 + 1));
        }
        GRAIN_GRADIENT => Fx::fill(grain_gradient(d[0] as u32)),
        METABALLS => Fx::fill(if d[0] as u32 == 1 { MetaballParams::mint() } else { MetaballParams::lava() }),
        GLOW => Fx::glow(GlowParams { style: GlowStyle::Soft, color: color(d[0]), intensity: d[1], radius: if d[2] > 0.0 { d[2] } else { 18.0 }, ..Default::default() }),
        SHIMMER => Fx::shimmer(ShimmerParams { intensity: d[0], period: d[1], band: d[2], ..Default::default() }),
        AURORA => Fx::aurora(AuroraParams { intensity: d[0], speed: d[1], ..Default::default() }),
        GRAIN => Fx::grain(GrainParams { amount: d[0], fps: d[1], ..Default::default() }),
        _ => return None,
    };
    Some(fx.on(shape).at(t))
}

struct FxCache {
    device_key: DeviceKey,
    format: wgpu::TextureFormat,
    fx: FxRenderer,
}

#[derive(Default)]
pub struct Gfx {
    fx: Option<FxCache>,
    /// While a sheet covers the page, the art holds this moment: nothing under the blur needs to
    /// move, and the window stops drawing frames until something changes.
    held: Option<f32>,
}

impl Compositor<crate::State> for Gfx {
    fn animating(&self) -> bool {
        self.held.is_none()
    }

    fn release_gpu(&mut self) {
        self.fx = None;
    }

    fn render(&mut self, f: &mut Frame<'_, crate::State>) {
        let (device, queue, size) = (f.device, f.queue, f.size);
        let now = f.rt.t as f32;
        self.held = match (f.rt.state.detail.is_some(), self.held) {
            (true, Some(t)) => Some(t),
            (true, None) => Some(now),
            (false, _) => None,
        };
        let t = self.held.unwrap_or(now);
        let marks = &f.rt.marks;
        let mut at: Vec<u32> = Vec::new();
        let mut layers: Vec<FxLayer> = Vec::new();
        let mut placements: Vec<FxSlot> = Vec::new();
        for (n, i) in f.rt.instances.iter().enumerate() {
            if i.params[1] != KIND_EFFECT {
                continue;
            }
            let kind = marks.get(n).copied().unwrap_or(0) & 0xff;
            let k = i.params[3];
            let [x, y, w, h] = i.rect.map(|v| v / k);
            let r = i.radii.iter().fold(0.0f32, |a, v| a.max(v.abs())) / k;
            let Some(l) = layer(kind, i.uv, Shape::rounded(x, y, w, h, r), t) else { continue };
            at.push(n as u32);
            layers.push(l);
            placements.push(FxSlot::default().scale(k).clip(i.clip, i.fade));
        }
        let device_key = f.device_key();
        if self.fx.as_ref().is_none_or(|c| c.device_key != device_key || c.format != f.format) {
            self.fx = None;
            let mut fx = FxRenderer::new(device, f.format, FxOptions { dither: true });
            fx.add_format(device, wgpu::TextureFormat::Rgba16Float);
            self.fx = Some(FxCache { device_key, format: f.format, fx });
        }
        let fx = &mut self.fx.as_mut().expect("made above").fx;
        fx.set_srgb_blend(f.srgb_blend());
        fx.prepare_with_slots(device, queue, &FxFrame { viewport: [size.0, size.1], scale: 1.0, layers: &layers, backdrop: None, sources: &[] }, &placements);
        let clear = f.rt.clear_color();
        f.ui.prepare(device, queue, &mut f.rt.text.atlas, &f.rt.instances, size);
        f.ui.draw_with_backdrop(f.encoder, f.target, clear, &mut |phase, s: &EffectSite<'_>| {
            let Some(k) = at.iter().position(|n| *n == s.index) else { return };
            match phase {
                EffectPhase::Backdrop(enc) => {
                    if let Some(bd) = s.backdrop {
                        fx.blur_backdrop(device, queue, enc, bd, k);
                    }
                }
                EffectPhase::Draw(pass) => fx.draw_layer_in(pass, s.format, s.origin, k),
            }
        });
    }
}

/// An effect slot: `kind` is one of the constants above, `data` its numbers.
pub fn fx(kind: u32, data: [f32; 4]) -> rune::El {
    rune::effect(kind).effect_data(data)
}
