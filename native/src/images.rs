//! Images: draw pictures on this computer. A hero that says what this PC can run, a prompt
//! composer, the image models rated for this hardware with the recommended one marked, and a
//! gallery of what was drawn. All of it is the server's `/images` API (see docs/IMAGES.md).

use std::collections::HashMap;

use rune::{El, Pixels, ScrollSpec, SurfaceFit, TextEdit, TextStyle, col, el, row, spacer, text_input};
use serde_json::Value;

use crate::gfx::{GLOW, GRAIN, GRAIN_GRADIENT, MESH, SHIMMER, fx};
use crate::style::*;
use crate::State;

#[derive(Clone, Copy, PartialEq, Eq, Debug, Default)]
pub enum Shape {
    #[default]
    Square,
    Portrait,
    Landscape,
}

impl Shape {
    fn label(self) -> &'static str {
        match self {
            Shape::Square => "Square",
            Shape::Portrait => "Portrait",
            Shape::Landscape => "Landscape",
        }
    }

    /// The size to ask for, from what the active model offers.
    fn size(self, sizes: &[String]) -> Option<String> {
        let wanted = |w: u32, h: u32| sizes.iter().find(|s| {
            let mut parts = s.split('x').map(|v| v.parse::<u32>().unwrap_or(0));
            let (sw, sh) = (parts.next().unwrap_or(0), parts.next().unwrap_or(0));
            (w == 0 || (sw > sh) == (w > h)) && (sw == sh) == (w == h)
        }).cloned();
        match self {
            Shape::Square => wanted(1, 1),
            Shape::Portrait => wanted(2, 3),
            Shape::Landscape => wanted(3, 2),
        }
    }
}

#[derive(Default)]
pub struct Images {
    /// `GET /images`, once it answers.
    pub status: Option<Value>,
    pub gallery: Vec<Value>,
    pub thumbs: HashMap<String, Pixels>,
    pub prompt: TextEdit,
    pub shape: Shape,
    pub drawing: bool,
    pub error: Option<String>,
    /// A model downloading: id, bytes done, bytes in all.
    pub installing: Option<(String, u64, u64)>,
    /// The picture shown full size.
    pub open: Option<String>,
}

// -------------------------------------------------------------------------------------------------
// Server calls

pub fn refresh(s: &mut State) {
    crate::call(s, |api| (api.images(), api.gallery()), |s, (status, gallery)| {
        if let Ok(status) = status {
            s.images.status = Some(status);
        }
        if let Ok(gallery) = gallery {
            s.images.gallery = gallery;
            load_thumbs(s);
        }
    });
}

/// Fetch and decode pictures not shown yet, a few at a time, off the UI thread.
fn load_thumbs(s: &mut State) {
    let wanted: Vec<String> = s.images.gallery.iter()
        .filter_map(|row| row.get("name").and_then(Value::as_str).map(str::to_owned))
        .filter(|name| !s.images.thumbs.contains_key(name))
        .take(24)
        .collect();
    for name in wanted {
        crate::call(s, {
            let name = name.clone();
            move |api| api.picture(&name).ok().and_then(|bytes| decode(&bytes))
        }, move |s, decoded| {
            if let Some((w, h, rgba)) = decoded {
                let pixels = Pixels::new();
                pixels.set(w, h, rgba);
                s.images.thumbs.insert(name, pixels);
            }
        });
    }
}

/// A PNG as RGBA8.
pub fn decode(bytes: &[u8]) -> Option<(u32, u32, Vec<u8>)> {
    let mut decoder = png::Decoder::new(std::io::Cursor::new(bytes));
    decoder.set_transformations(png::Transformations::EXPAND | png::Transformations::STRIP_16);
    let mut reader = decoder.read_info().ok()?;
    let mut buffer = vec![0; reader.output_buffer_size()?];
    let info = reader.next_frame(&mut buffer).ok()?;
    let data = &buffer[..info.buffer_size()];
    let rgba = match info.color_type {
        png::ColorType::Rgba => data.to_vec(),
        png::ColorType::Rgb => data.chunks_exact(3).flat_map(|p| [p[0], p[1], p[2], 255]).collect(),
        png::ColorType::GrayscaleAlpha => data.chunks_exact(2).flat_map(|p| [p[0], p[0], p[0], p[1]]).collect(),
        png::ColorType::Grayscale => data.iter().flat_map(|&g| [g, g, g, 255]).collect(),
        _ => return None,
    };
    Some((info.width, info.height, rgba))
}

fn generate(s: &mut State) {
    let prompt = s.images.prompt.text.trim().to_owned();
    if prompt.is_empty() || s.images.drawing {
        return;
    }
    let size = s.images.shape.size(&active_sizes(s));
    s.images.drawing = true;
    s.images.error = None;
    crate::call(s, move |api| api.generate(&prompt, size.as_deref()), |s, result| {
        s.images.drawing = false;
        match result {
            Ok(row) => {
                s.images.gallery.insert(0, row);
                load_thumbs(s);
            }
            Err(why) => s.images.error = Some(why),
        }
    });
}

fn install(s: &mut State, model: &str) {
    if s.images.installing.is_some() {
        return;
    }
    s.images.installing = Some((model.to_owned(), 0, 0));
    s.images.error = None;
    let Some(handle) = s.handle.clone() else { return };
    let model = model.to_owned();
    crate::call(s, move |api| {
        let id = model.clone();
        api.install_model(&model, |done, total| {
            let id = id.clone();
            handle.update(move |s, _| s.images.installing = Some((id, done, total)));
        })
    }, |s, result| {
        s.images.installing = None;
        if let Err(why) = result {
            s.images.error = Some(why);
        }
        refresh(s);
    });
}

fn choose(s: &mut State, model: &str) {
    let model = model.to_owned();
    crate::call(s, move |api| api.choose(&model), |s, result| {
        if let Err(why) = result {
            s.images.error = Some(why);
        }
        refresh(s);
    });
}

// -------------------------------------------------------------------------------------------------
// View

fn text_of<'a>(v: &'a Value, key: &str) -> &'a str {
    v.get(key).and_then(Value::as_str).unwrap_or("")
}

fn gib(bytes: u64) -> String {
    format!("{:.1} GB", bytes as f64 / 1e9)
}

fn active_sizes(s: &State) -> Vec<String> {
    let Some(status) = &s.images.status else { return Vec::new() };
    let active = text_of(status, "active");
    status.get("models").and_then(Value::as_array).into_iter().flatten()
        .find(|m| text_of(m, "id") == active)
        .and_then(|m| m.get("sizes")).and_then(Value::as_array)
        .map(|a| a.iter().filter_map(Value::as_str).map(str::to_owned).collect())
        .unwrap_or_default()
}

pub fn view(s: &State) -> El {
    let page = col().key("images-page").w_full().max_w(1240.0).gap(36.0).px(40.0).pt(28.0).pb(64.0)
        .child(hero(s))
        .child(composer(s))
        .child(models(s))
        .child(gallery(s));
    let scroller = col().key("images-scroll").w_full().h_full().items_center().fill(c(CANVAS))
        .scroll(ScrollSpec { stick_bottom: false })
        .child(page);
    let mut stack = el().w_full().h_full().child(scroller);
    if let Some(name) = &s.images.open {
        stack = stack.child(viewer(s, name));
    }
    stack
}

fn chip(label: String) -> El {
    row().h(30.0).px(12.0).center_y().radius(15.0).fill(ca(0x000000, 0.3)).backdrop_blur(12.0)
        .border(1.0, ca(0xffffff, 0.14))
        .child(ta(label, 12.5, 560.0, 0xffffff, 0.9))
}

fn hero(s: &State) -> El {
    let h = 260.0;
    let mut chips = row().gap(8.0).flex_wrap();
    if let Some(hw) = s.images.status.as_ref().and_then(|v| v.get("hardware")) {
        let gpu = text_of(hw, "gpu");
        chips = chips.child(chip(if gpu.is_empty() { "No graphics card found".into() } else { gpu.to_owned() }));
        if let Some(vram) = hw.get("vram_bytes").and_then(Value::as_u64) {
            chips = chips.child(chip(format!("{} graphics memory", gib(vram))));
        }
        if let Some(ram) = hw.get("ram_bytes").and_then(Value::as_u64) {
            chips = chips.child(chip(format!("{} memory", gib(ram))));
        }
    } else {
        chips = chips.child(chip("Checking this computer…".into()));
    }
    let copy = col().gap(12.0).items_start().max_w(640.0)
        .child(row().h(26.0).px(11.0).center_y().radius(13.0).fill(ca(0xffffff, 0.12)).border(1.0, ca(0xffffff, 0.14))
            .child(ta("KOVA IMAGES", 11.0, 650.0, 0xffffff, 0.92)))
        .child(t("Draw anything, right here", 40.0, 680.0, 0xffffff))
        .child(ta("Pictures are made on this computer by a model picked for its hardware. Prompts and \
                   pictures never leave it.", 15.5, 430.0, 0xffffff, 0.8).max_w(560.0).wrap())
        .child(chips.mt(6.0));
    let frame = el().key("images-hero").w_full().h(h).radius(28.0).clip_rounded(28.0)
        .child(fx(MESH, [7.0, 0.14, 5.0, 0.16]).absolute(0.0, 0.0).w_full().h(h).radius(28.0))
        .child(fx(GRAIN, [0.06, 0.0, 0.0, 0.0]).absolute(0.0, 0.0).w_full().h(h).radius(28.0))
        .child(el().absolute(0.0, 0.0).w_full().h(h)
            .linear_gradient(90.0, [(0.0, ca(0x06040d, 0.75)), (0.6, ca(0x06040d, 0.2)), (1.0, ca(0x06040d, 0.0))]))
        .child(copy.absolute(48.0, 40.0))
        .border(1.0, ca(0xffffff, 0.08));
    lift(frame, 1.4)
}

fn composer(s: &State) -> El {
    let status = s.images.status.as_ref();
    let active = status.map(|v| text_of(v, "active")).unwrap_or("");
    let engine_ready = status.and_then(|v| v.get("engine")).and_then(|e| e.get("ready")).and_then(Value::as_bool).unwrap_or(false);
    let can_draw = !active.is_empty() && engine_ready && !s.images.drawing && !s.images.prompt.text.trim().is_empty();
    let input = text_input(&s.images.prompt, TextStyle::new(17.0, 450.0)).key("images-prompt")
        .label("Describe the picture")
        .placeholder("A lighthouse on a cliff at dusk, in soft watercolour…", c(FAINT))
        .color(c(TEXT)).grow(1.0)
        .on_edit(|s: &mut State| &mut s.images.prompt)
        .on_submit(|s: &mut State, _| generate(s));
    let mut shapes = row().gap(6.0);
    let sizes = active_sizes(s);
    for shape in [Shape::Square, Shape::Portrait, Shape::Landscape] {
        if !sizes.is_empty() && shape.size(&sizes).is_none() {
            continue;
        }
        let on = s.images.shape == shape;
        shapes = shapes.child(row().key(format!("shape-{}", shape.label())).h(32.0).px(12.0).center_y().radius(10.0)
            .fill(if on { ca(0xffffff, 0.12) } else { ca(0xffffff, 0.0) })
            .border(1.0, if on { ca(0xffffff, 0.18) } else { hair() })
            .hover_fill(ca(0xffffff, 0.08)).press_scale(0.96).role("radio").checked(on).label(shape.label()).focusable(true)
            .on_press(move |s: &mut State| s.images.shape = shape)
            .child(t(shape.label(), 12.5, 560.0, if on { TEXT } else { MUTED })));
    }
    let button = row().key("images-draw").h(44.0).px(22.0).gap(8.0).center().radius(22.0)
        .fill(if can_draw { c(TEXT) } else { ca(0xffffff, 0.08) })
        .hover_fill(if can_draw { c(0xffffff) } else { ca(0xffffff, 0.08) })
        .press_scale(0.96).role("button").label("Draw").focusable(can_draw)
        .on_press(|s: &mut State| generate(s))
        .child(t(if s.images.drawing { "Drawing…" } else { "Draw" }, 14.0, 650.0, if can_draw { CANVAS } else { FAINT }));
    let note = if active.is_empty() {
        "Pick a model below to start drawing.".to_string()
    } else if !engine_ready {
        status.and_then(|v| v.get("engine")).map(|e| text_of(e, "help").to_owned()).unwrap_or_default()
    } else {
        format!("Drawing with {}. Press Enter to draw.", model_name(s, active))
    };
    let mut card = col().key("composer").w_full().gap(14.0).padding(18.0).radius(22.0).fill(c(SURFACE)).border(1.0, hair())
        .child(row().w_full().min_h(52.0).px(6.0).center_y().child(input))
        .child(row().w_full().center_y().gap(12.0).child(shapes).child(spacer())
            .child(t(note, 12.5, 480.0, FAINT).max_w(520.0).wrap())
            .child(button));
    if let Some(why) = &s.images.error {
        card = card.child(t(why.clone(), 13.0, 520.0, DANGER).w_full().wrap());
    }
    sheen(lift(card, 1.0))
}

fn model_name(s: &State, id: &str) -> String {
    s.images.status.as_ref().and_then(|v| v.get("models")).and_then(Value::as_array).into_iter().flatten()
        .find(|m| text_of(m, "id") == id).map(|m| text_of(m, "name").to_owned()).unwrap_or_else(|| id.to_owned())
}

fn models(s: &State) -> El {
    let Some(status) = &s.images.status else {
        return col().w_full().h(120.0).center().child(t("Checking what this computer can run…", 14.0, 500.0, MUTED));
    };
    let recommended = text_of(status, "recommended");
    let active = text_of(status, "active");
    let mut grid = el().w_full().auto_fill_columns(260.0).gap(18.0);
    for (i, model) in status.get("models").and_then(Value::as_array).into_iter().flatten().enumerate() {
        grid = grid.child(model_card(s, model, i, text_of(model, "id") == recommended, text_of(model, "id") == active));
    }
    col().w_full().gap(16.0)
        .child(col().gap(4.0)
            .child(t("Models for this computer", 22.0, 660.0, TEXT))
            .child(t("Rated against your graphics card, memory and disk. The recommended one is the best that fits.", 14.0, 430.0, MUTED)))
        .child(grid)
}

fn model_card(s: &State, model: &Value, index: usize, recommended: bool, active: bool) -> El {
    let id = text_of(model, "id").to_owned();
    let rating = text_of(model, "rating");
    let installed = model.get("installed").and_then(Value::as_bool).unwrap_or(false);
    let fits = rating != "too-big";
    let downloading = s.images.installing.as_ref().filter(|(m, _, _)| *m == id);
    let (badge, badge_color) = if recommended {
        ("Recommended", ACCENT)
    } else if rating == "fits-comfortably" {
        ("Fits well", SUCCESS)
    } else if rating == "fits-tightly" {
        ("Fits, just", 0xe0b341)
    } else {
        ("Too big for this PC", FAINT)
    };
    let cover_h = 96.0;
    let cover = el().w_full().h(cover_h).clip_rounded(18.0)
        .child(fx(GRAIN_GRADIENT, [[3.0, 4.0, 1.0, 2.0][index % 4], 0.0, 0.0, 0.0]).absolute(0.0, 0.0).w_full().h(cover_h))
        .child(el().absolute(0.0, 0.0).w_full().h(cover_h).fill(ca(0x000000, if fits { 0.1 } else { 0.55 })))
        .child(row().absolute(14.0, 14.0).h(24.0).px(10.0).gap(6.0).center_y().radius(12.0)
            .fill(ca(0x000000, 0.4)).backdrop_blur(12.0).border(1.0, ca(0xffffff, 0.14))
            .child(el().size(6.0, 6.0).radius(3.0).fill(c(badge_color)))
            .child(ta(badge, 11.5, 600.0, 0xffffff, 0.95)));
    let action: El = if let Some((_, done, total)) = downloading {
        let frac = if *total > 0 { *done as f32 / *total as f32 } else { 0.0 };
        col().w_full().gap(6.0)
            .child(el().w_full().h(6.0).radius(3.0).fill(ca(0xffffff, 0.08))
                .child(el().w_frac(frac.max(0.02)).h(6.0).radius(3.0).fill(c(ACCENT))))
            .child(t(format!("Downloading and verifying… {}%", (frac * 100.0) as u32), 12.0, 520.0, MUTED))
    } else if active {
        row().gap(6.0).center_y().child(el().size(7.0, 7.0).radius(3.5).fill(c(SUCCESS))).child(t("Drawing with this", 12.5, 600.0, SUCCESS))
    } else if installed {
        let id = id.clone();
        pill_button("Use", false).key(format!("use-{id}")).on_press(move |s: &mut State| choose(s, &id))
    } else if fits {
        let id = id.clone();
        pill_button(&format!("Download {}", gib(model.get("download_bytes").and_then(Value::as_u64).unwrap_or(0))), recommended)
            .key(format!("get-{id}")).on_press(move |s: &mut State| install(s, &id))
    } else {
        t(text_of(model, "reason").to_owned(), 12.0, 480.0, FAINT).w_full().wrap()
    };
    let body = col().w_full().padding(16.0).gap(6.0)
        .child(t(text_of(model, "name").to_owned(), 15.5, 640.0, if fits { TEXT } else { MUTED }))
        .child(t(text_of(model, "about").to_owned(), 13.0, 430.0, MUTED).w_full().wrap().max_lines(2).h(36.0))
        .child(t(format!("{} · {}", gib(model.get("download_bytes").and_then(Value::as_u64).unwrap_or(0)), text_of(model, "license")),
                 12.0, 500.0, FAINT))
        .child(el().h(8.0))
        .child(action);
    let card = sheen(col().key(format!("model-{id}")).w_full().radius(18.0).fill(c(SURFACE))
        .border(1.0, if recommended { ca(0x6d9cfe, 0.45) } else { hair() })
        .child(cover).child(body));
    let card = lift(card, if recommended { 1.4 } else { 0.8 });
    if recommended {
        el().w_full().child(fx(GLOW, [0.0, 0.4, 22.0, 0.0]).absolute(0.0, 0.0).w_full().h_full().radius(18.0).effect_bleed(50.0)).child(card)
    } else {
        card
    }
}

fn pill_button(label: &str, strong: bool) -> El {
    row().h(36.0).px(18.0).center().radius(18.0)
        .fill(if strong { c(TEXT) } else { ca(0xffffff, 0.08) })
        .hover_fill(if strong { c(0xffffff) } else { ca(0xffffff, 0.12) })
        .press_scale(0.96).role("button").label(label).focusable(true)
        .child(t(label, 13.0, 650.0, if strong { CANVAS } else { TEXT }))
}

fn gallery(s: &State) -> El {
    let mut grid = el().w_full().auto_fill_columns(220.0).gap(16.0);
    if s.images.drawing {
        grid = grid.child(col().key("tile-drawing").w_full().gap(8.0)
            .child(el().w_full().aspect_ratio(1.0).radius(18.0).clip_rounded(18.0)
                .child(fx(MESH, [4.0, 0.6, 9.0, 0.3]).absolute(0.0, 0.0).w_full().h_full())
                .child(fx(SHIMMER, [0.5, 1.4, 0.35, 0.0]).absolute(0.0, 0.0).w_full().h_full())
                .child(col().w_full().h_full().center().child(ta("Drawing…", 15.0, 620.0, 0xffffff, 0.92))))
            .child(t(s.images.prompt.text.trim().to_owned(), 12.5, 450.0, MUTED).w_full().max_lines(2)));
    }
    for row_ in &s.images.gallery {
        let name = text_of(row_, "name").to_owned();
        let open = name.clone();
        let picture = match s.images.thumbs.get(&name) {
            Some(pixels) => el().w_full().h_full().pixels_fit(pixels, SurfaceFit::Fill),
            None => el().w_full().h_full().fill(c(RAISED)),
        };
        grid = grid.child(col().key(format!("tile-{name}")).w_full().gap(8.0)
            .child(lift(el().w_full().aspect_ratio(1.0).radius(18.0).clip_rounded(18.0).border(1.0, hair())
                .child(picture)
                .hover_border(ca(0xffffff, 0.3)).press_scale(0.98)
                .role("button").label(text_of(row_, "prompt").to_owned()).focusable(true)
                .on_press(move |s: &mut State| s.images.open = Some(open.clone())), 0.8))
            .child(t(text_of(row_, "prompt").to_owned(), 12.5, 450.0, MUTED).w_full().wrap().max_lines(2)));
    }
    let mut section = col().w_full().gap(16.0).child(t("Your pictures", 22.0, 660.0, TEXT));
    if s.images.gallery.is_empty() && !s.images.drawing {
        section = section.child(col().w_full().h(200.0).center().gap(8.0).radius(22.0).fill(c(SURFACE)).border(1.0, hair())
            .child(t("Nothing drawn yet", 17.0, 620.0, TEXT))
            .child(t("Describe a picture above, or ask for one in any chat with /imagine.", 13.5, 430.0, MUTED)));
    } else {
        section = section.child(grid);
    }
    section
}

fn viewer(s: &State, name: &str) -> El {
    let row_ = s.images.gallery.iter().find(|r| text_of(r, "name") == name);
    let picture = match s.images.thumbs.get(name) {
        Some(pixels) => el().size(640.0, 640.0).radius(20.0).clip_rounded(20.0).pixels_fit(pixels, SurfaceFit::Fit),
        None => el().size(640.0, 640.0).radius(20.0).fill(c(RAISED)),
    };
    let mut info = col().w(300.0).gap(12.0)
        .child(t(row_.map(|r| text_of(r, "prompt")).unwrap_or("").to_owned(), 16.0, 520.0, TEXT).w_full().wrap());
    if let Some(r) = row_ {
        let num = |k: &str| r.get(k).and_then(Value::as_u64).map(|v| v.to_string()).unwrap_or_default();
        info = info.child(col().gap(6.0)
            .child(t(format!("{} × {}", num("width"), num("height")), 13.0, 500.0, MUTED))
            .child(t(format!("Model: {}", model_name(s, text_of(r, "model"))), 13.0, 500.0, MUTED))
            .child(t(format!("Seed: {}", num("seed")), 13.0, 500.0, MUTED))
            .child(t(format!("Saved in Kova Files/Images/{name}"), 12.0, 480.0, FAINT).w_full().wrap()));
    }
    let panel = lift(row().key("viewer").gap(28.0).padding(24.0).radius(28.0).fill(c(SURFACE)).border(1.0, ca(0xffffff, 0.1))
        .child(picture).child(info), 2.4)
        .modal(true, |s: &mut State| s.images.open = None);
    el().key("viewer-backdrop").absolute(0.0, 0.0).w_full().h_full().center()
        .fill(ca(0x000000, 0.6)).backdrop_blur(18.0)
        .child(panel)
}
