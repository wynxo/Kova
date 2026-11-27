//! Kova's native desktop app: a GPU-rendered client of the local Kova server, built on Rune.
//!
//! The server owns chats, memory, extensions and models; this app is one more thin view over its
//! HTTP API, like the terminal chat and the web app. It opens on the Marketplace.
//!
//!     cargo run --release                               # a window
//!     cargo run --release --features snap -- --scene detail --snap market.png --size 1440x900

#![forbid(unsafe_code)]

mod api;
mod art;
mod data;
mod gfx;
mod images;
mod market;
mod style;

use rune::{App, AppHandle, AppSpec, Cx, El, TextEdit, col, el, row, spacer};
use serde_json::Value;

use crate::api::Api;
use crate::data::Entry;
use crate::gfx::{MESH, fx};
use crate::style::*;

#[derive(Clone, Copy, PartialEq, Eq, Debug, Default)]
pub enum Tab {
    #[default]
    Market,
    Images,
    Bots,
}

#[derive(Clone, Copy, PartialEq, Eq, Debug, Default)]
pub enum Filter {
    #[default]
    All,
    Bot,
    Plugin,
    Skill,
}

impl Filter {
    pub fn label(self) -> &'static str {
        match self {
            Filter::All => "All",
            Filter::Bot => "Bots",
            Filter::Plugin => "Plugins",
            Filter::Skill => "Skills",
        }
    }

    pub fn admits(self, entry: &Entry) -> bool {
        match self {
            Filter::All => true,
            Filter::Bot => entry.kind == "bot",
            Filter::Plugin => entry.kind == "plugin",
            Filter::Skill => entry.kind == "skill",
        }
    }
}

#[derive(Debug, Default)]
pub enum Install {
    #[default]
    Idle,
    Checking,
    /// The server's preview: what the download adds, and its one-use ticket.
    Confirm(Value),
    Installing,
    Done(String),
    Failed(String),
}

pub struct State {
    pub tab: Tab,
    pub filter: Filter,
    pub search: TextEdit,
    pub entries: Vec<Entry>,
    /// The server answered; without it the bundled catalog is shown and installs are off.
    pub online: bool,
    pub detail: Option<String>,
    pub install: Install,
    pub hovered: Option<String>,
    pub images: images::Images,
    /// Why the Chat button could not open the classic app, if it could not.
    pub chat_note: Option<String>,
    api: Option<Api>,
    handle: Option<AppHandle<State>>,
}

impl State {
    fn new() -> State {
        State {
            tab: Tab::Market,
            filter: Filter::All,
            search: TextEdit::new(""),
            entries: data::bundled(),
            online: false,
            detail: None,
            install: Install::Idle,
            hovered: None,
            images: images::Images::default(),
            chat_note: None,
            api: Api::discover(),
            handle: None,
        }
    }
}

/// Run `work` on a worker thread with the server client, then apply its result on the UI thread.
fn call<T: Send + 'static>(s: &State, work: impl FnOnce(&Api) -> T + Send + 'static,
                           done: impl FnOnce(&mut State, T) + Send + 'static) {
    let (Some(api), Some(handle)) = (s.api.clone(), s.handle.clone()) else { return };
    std::thread::spawn(move || {
        let result = work(&api);
        handle.update(move |s, _| done(s, result));
    });
}

pub fn refresh(s: &mut State) {
    call(s, |api| api.catalog(), |s, result| match result {
        Ok(entries) => {
            s.entries = entries;
            s.online = true;
        }
        Err(_) => s.online = false,
    });
}

pub fn preview(s: &mut State, name: &str, version: &str) {
    s.install = Install::Checking;
    let (name, version) = (name.to_owned(), version.to_owned());
    call(s, move |api| api.preview(&name, &version), |s, result| {
        s.install = match result {
            Ok(preview) => Install::Confirm(preview),
            Err(why) => Install::Failed(why),
        };
    });
}

pub fn install(s: &mut State, ticket: &str, upgrade: bool) {
    s.install = Install::Installing;
    let ticket = ticket.to_owned();
    call(s, move |api| api.install(&ticket, upgrade), |s, result| {
        s.install = match result {
            Ok(what) => Install::Done(what),
            Err(why) => Install::Failed(why),
        };
        refresh(s);
    });
}

/// Drop an unconfirmed download on the server (best effort).
pub fn cancel(s: &mut State) {
    if let Install::Confirm(preview) = std::mem::take(&mut s.install) {
        if let Some(ticket) = preview.get("ticket").and_then(Value::as_str).map(str::to_owned) {
            call(s, move |api| { let _ = api.cancel(&ticket); }, |_, ()| {});
        }
    }
}

// -------------------------------------------------------------------------------------------------
// Shell

fn open_tab(s: &mut State, tab: Tab) {
    s.tab = tab;
    if tab == Tab::Images {
        images::refresh(s);
    }
}

fn nav_item(s: &State, tab: Tab, label: &str, glyph: &str, ready: bool) -> El {
    let on = s.tab == tab;
    let mut item = row().key(format!("nav-{label}")).w_full().h(38.0).px(12.0).gap(12.0).center_y().radius(10.0)
        .fill(if on { ca(0xffffff, 0.07) } else { ca(0xffffff, 0.0) })
        .role("tab").label(label).focusable(ready)
        .child(t(glyph, 15.0, 500.0, if on { TEXT } else { FAINT }).w(18.0))
        .child(t(label, 14.0, if on { 600.0 } else { 500.0 }, if on { TEXT } else if ready { MUTED } else { FAINT }))
        .child(spacer());
    if ready {
        item = item.hover_fill(ca(0xffffff, 0.05)).press_scale(0.98).on_press(move |s: &mut State| open_tab(s, tab));
    } else {
        item = item.child(row().h(20.0).px(8.0).center_y().radius(10.0).fill(ca(0xffffff, 0.05)).child(t("Soon", 10.5, 600.0, FAINT)));
    }
    item
}

/// Chat still lives in the classic app: open it with the same Kova this app was launched from.
fn open_chat(s: &mut State) {
    let cli = std::env::var_os("KOVA_CLI").map(std::path::PathBuf::from)
        .filter(|path| path.is_file())
        .unwrap_or_else(|| std::path::PathBuf::from(if cfg!(windows) { "kova.exe" } else { "kova" }));
    let started = std::process::Command::new(cli).args(["app", "--classic"])
        .stdin(std::process::Stdio::null()).stdout(std::process::Stdio::null()).stderr(std::process::Stdio::null())
        .spawn();
    s.chat_note = started.err().map(|_| "Couldn't open chat. Run: kova app --classic".to_string());
}

fn chat_item(s: &State) -> El {
    let mut item = col().key("nav-chat").w_full().gap(4.0)
        .child(row().w_full().h(38.0).px(12.0).gap(12.0).center_y().radius(10.0)
            .fill(ca(0xffffff, 0.0)).hover_fill(ca(0xffffff, 0.05)).press_scale(0.98)
            .role("link").label("Chat").focusable(true)
            .on_press(|s: &mut State| open_chat(s))
            .child(t("✦", 15.0, 500.0, FAINT).w(18.0))
            .child(t("Chat", 14.0, 500.0, MUTED))
            .child(spacer())
            .child(t("↗", 13.0, 500.0, FAINT)));
    if let Some(note) = &s.chat_note {
        item = item.child(t(note.clone(), 11.5, 480.0, DANGER).px(12.0).w_full().wrap());
    }
    item
}

fn sidebar(s: &State) -> El {
    let logo = row().gap(10.0).center_y().px(8.0)
        .child(fx(MESH, [4.0, 0.3, 11.0, 0.2]).size(30.0, 30.0).radius(9.0))
        .child(t("Kova", 17.0, 680.0, TEXT));
    let status = row().gap(8.0).center_y().px(10.0)
        .child(el().size(7.0, 7.0).radius(3.5).fill(c(if s.online { SUCCESS } else { FAINT })))
        .child(t(if s.online { "Connected" } else { "Kova isn't running" }, 12.5, 500.0, if s.online { MUTED } else { FAINT }));
    col().key("sidebar").w(236.0).h_full().padding(14.0).pt(22.0).gap(4.0).fill(c(SIDEBAR))
        .border_sides([0.0, 1.0, 0.0, 0.0], hair())
        .child(logo.mb(22.0))
        .child(chat_item(s))
        .child(t("BROWSE", 10.5, 650.0, FAINT).px(12.0).mt(14.0).mb(4.0))
        .child(nav_item(s, Tab::Market, "Marketplace", "◆", true))
        .child(nav_item(s, Tab::Images, "Images", "◐", true))
        .child(nav_item(s, Tab::Bots, "Bots", "☺", false))
        .child(spacer())
        .child(status)
}

fn view(s: &State, _cx: &mut Cx<State>) -> El {
    let page = match s.tab {
        Tab::Market => market::view(s),
        Tab::Images => images::view(s),
        Tab::Bots => col().w_full().h_full().center().child(t("Coming soon", 16.0, 600.0, MUTED)),
    };
    row().w_full().h_full().fill(c(CANVAS))
        .child(sidebar(s))
        .child(el().grow(1.0).h_full().child(page))
}

// -------------------------------------------------------------------------------------------------
// Scenes for headless snapshots (`--scene NAME`); each starts from the bundled catalog.

fn demo(s: &mut State) {
    s.online = true;
    s.api = None;
}

fn scene_detail(s: &mut State) {
    demo(s);
    s.detail = Some("tutor".into());
}

fn scene_confirm(s: &mut State) {
    demo(s);
    s.detail = Some("tutor".into());
    s.install = Install::Confirm(serde_json::json!({
        "ticket": "demo", "sha256": "b71f0c443b0939bb86acb7d4213e50b182aa34593f330b27ec678da641a361d5",
        "bot": {"tools": ["web_*", "memory_*", "reminder_*"], "ceiling": "manual", "model": "",
                "persona": "You are a patient tutor who helps people actually learn, not just get answers.\n\nHow you work:\n- Find out what the person already knows with one short question.\n- Teach one idea at a time, with a concrete example for each."},
        "notes": []
    }));
}

/// The confirmation for a preview saved from a real server (`KOVA_APP_PREVIEW=file.json`).
fn scene_preview(s: &mut State) {
    demo(s);
    let raw = std::env::var("KOVA_APP_PREVIEW").ok().and_then(|path| std::fs::read_to_string(path).ok()).unwrap_or_default();
    let preview: Value = serde_json::from_str(&raw).unwrap_or(Value::Null);
    s.detail = preview.get("name").and_then(Value::as_str).map(str::to_owned);
    s.install = Install::Confirm(preview);
}

fn scene_hover(s: &mut State) {
    demo(s);
    s.hovered = Some("tutor".into());
}

fn scene_bots(s: &mut State) {
    demo(s);
    s.filter = Filter::Bot;
}

const DEMO_IMAGES: &str = r#"{
  "hardware": {"gpu": "Graphics card, 8 GB", "vram_bytes": 8589934592, "ram_bytes": 34359738368, "free_disk": 214748364800, "os": "Linux"},
  "recommended": "z-image-turbo-q4", "cpu": false, "active": null,
  "engine": {"ready": true, "source": "configured", "help": null, "managed": false},
  "models": [
    {"id": "sdxs-512", "name": "SDXS 512", "about": "Tiny one-step model: a rough picture in seconds, even without a GPU.", "download_bytes": 682847200, "license": "OpenRAIL++", "installed": false, "rating": "fits-comfortably", "reason": "Expect seconds on a GPU.", "sizes": ["512x512", "512x768", "768x512"]},
    {"id": "sd15", "name": "Stable Diffusion 1.5", "about": "The classic all-rounder at 512 px; huge community know-how for prompts.", "download_bytes": 1763578176, "license": "CreativeML OpenRAIL-M", "installed": true, "rating": "fits-comfortably", "reason": "Expect a few seconds on a GPU.", "sizes": ["512x512", "512x768", "768x512"]},
    {"id": "z-image-turbo-q4", "name": "Z-Image Turbo (compact)", "about": "Sharp, modern 1024 px pictures that follow long prompts and draw text.", "download_bytes": 6696836812, "license": "Apache-2.0", "installed": false, "rating": "fits-tightly", "reason": "Little room to spare.", "sizes": ["1024x1024", "768x1024", "1024x768", "768x768", "512x512"]},
    {"id": "z-image-turbo-q8", "name": "Z-Image Turbo (full quality)", "about": "The same model at near-full precision, for GPUs with room to spare.", "download_bytes": 11193150692, "license": "Apache-2.0", "installed": false, "rating": "too-big", "reason": "Needs more graphics memory than this PC has.", "sizes": ["1024x1024"]}
  ]
}"#;

fn scene_images(s: &mut State) {
    demo(s);
    s.tab = Tab::Images;
    s.images.status = serde_json::from_str(DEMO_IMAGES).ok();
}

fn scene_downloading(s: &mut State) {
    scene_images(s);
    s.images.installing = Some(("z-image-turbo-q4".into(), 2_600_000_000, 6_696_836_812));
}

/// Pictures from `KOVA_APP_PICTURES` (a folder of PNGs), to check the gallery's layout.
fn scene_gallery(s: &mut State) {
    scene_images(s);
    if let Some(status) = s.images.status.as_mut() {
        status["active"] = Value::from("sd15");
    }
    let folder = std::env::var("KOVA_APP_PICTURES").unwrap_or_default();
    let mut names: Vec<_> = std::fs::read_dir(&folder).into_iter().flatten().flatten()
        .map(|e| e.path()).filter(|p| p.extension().is_some_and(|x| x == "png")).collect();
    names.sort();
    for path in names {
        let name = path.file_name().unwrap_or_default().to_string_lossy().to_string();
        if let Some((w, h, rgba)) = std::fs::read(&path).ok().and_then(|b| images::decode(&b)) {
            let pixels = rune::Pixels::new();
            pixels.set(w, h, rgba);
            s.images.thumbs.insert(name.clone(), pixels);
            s.images.gallery.push(serde_json::json!({"name": name, "prompt": name.trim_end_matches(".png").replace('-', " "),
                                                     "width": w, "height": h, "seed": 7, "model": "sd15"}));
        }
    }
}

fn scene_offline(s: &mut State) {
    s.online = false;
    s.api = None;
}

fn main() {
    let spec = AppSpec::<State> {
        scenes: &[
            ("market", demo),
            ("hover", scene_hover),
            ("detail", scene_detail),
            ("confirm", scene_confirm),
            ("preview", scene_preview),
            ("bots", scene_bots),
            ("offline", scene_offline),
            ("images", scene_images),
            ("downloading", scene_downloading),
            ("gallery", scene_gallery),
        ],
        fonts: rune_theme_opencode::FONTS,
        families: rune_theme_opencode::FAMILIES,
        min_size: Some((960.0, 640.0)),
        ..AppSpec::new("Kova", (1440.0, 900.0), State::new, view)
    };
    let app = App::new(spec).compositor(gfx::Gfx::default());
    let handle = app.handle();
    app.init(move || {
        let mut state = State { handle: Some(handle.clone()), ..State::new() };
        // The first catalog load; its result lands through the handle once the app runs.
        refresh(&mut state);
        state
    })
    .run();
}
