//! The Marketplace: a live GPU hero, a featured shelf, a shelf per kind, and a detail sheet that
//! runs the server's verify → show → confirm install. Nothing installs from a card directly.

use rune::{El, ScrollSpec, TextStyle, col, el, row, spacer, text_input};
use serde_json::Value;

use crate::art;
use crate::data::Entry;
use crate::gfx::{GLOW, GRAIN, MESH, SHIMMER, fx};
use crate::style::*;
use crate::{Filter, Install, State};

const CARD_RADIUS: f32 = 18.0;
const SHEET_W: f32 = 760.0;

pub fn view(s: &State) -> El {
    let shown: Vec<&Entry> = s.entries.iter().filter(|e| s.filter.admits(e) && e.matches(&s.search.text)).collect();
    let browsing = s.search.text.trim().is_empty() && s.filter == Filter::All;
    let mut page = col().key("market-page").w_full().max_w(1240.0).gap(36.0).px(40.0).pt(28.0).pb(64.0)
        .child(hero(s))
        .child(filters(s));
    if browsing {
        page = page.child(featured(s));
        for (kind, title, blurb) in [
            ("bot", "Bots", "Ready-made assistants with their own voice, tools and notes. Chat with one, or add it to a group."),
            ("plugin", "Plugins", "New commands, skills and tools that plug into Kova. Each one shows exactly what it adds."),
            ("skill", "Skills", "Instructions Kova loads when they help: a playbook for one kind of task."),
        ] {
            let items: Vec<&Entry> = shown.iter().copied().filter(|e| e.kind == kind).collect();
            if !items.is_empty() {
                page = page.child(shelf(s, title, blurb, &items));
            }
        }
    } else if shown.is_empty() {
        page = page.child(empty());
    } else {
        let title = match s.filter { Filter::All => "Results", Filter::Bot => "Bots", Filter::Plugin => "Plugins", Filter::Skill => "Skills" };
        page = page.child(shelf(s, title, "", &shown));
    }
    let scroller = col().key("market-scroll").w_full().h_full().items_center().fill(c(CANVAS))
        .scroll(ScrollSpec { stick_bottom: false })
        .child(page);
    let mut stack = el().w_full().h_full().child(scroller);
    if let Some(entry) = s.detail.as_ref().and_then(|name| s.entries.iter().find(|e| &e.name == name)) {
        stack = stack.child(sheet(s, entry));
    }
    stack
}

// ---------------------------------------------------------------------------------------------
// Hero

fn hero(s: &State) -> El {
    let total = s.entries.iter().filter(|e| e.available()).count();
    let search = text_input(&s.search, TextStyle::new(15.0, 450.0)).key("market-search")
        .label("Search the marketplace")
        .placeholder("Search bots, plugins and skills", ca(0xffffff, 0.55))
        .color(c(TEXT)).grow(1.0)
        .on_edit(|s: &mut State| &mut s.search);
    let field = row().key("search-field").w(520.0).h(52.0).px(18.0).gap(12.0).center_y().radius(16.0)
        .fill(ca(0x0b0d1a, 0.38)).backdrop_blur(24.0).border(1.0, ca(0xffffff, 0.16))
        .child(ta("⌕", 18.0, 500.0, 0xffffff, 0.7))
        .child(search);
    let copy = col().gap(14.0).max_w(620.0).items_start()
        .child(row().h(26.0).px(11.0).center_y().radius(13.0).fill(ca(0xffffff, 0.12)).border(1.0, ca(0xffffff, 0.14))
            .child(ta("KOVA MARKETPLACE", 11.0, 650.0, 0xffffff, 0.92)))
        .child(t("Make Kova yours", 46.0, 680.0, 0xffffff))
        .child(ta("Bots, plugins and skills for your private AI. Every download is checksum-verified, \
                   and nothing runs until you've seen what it adds.", 16.0, 430.0, 0xffffff, 0.78).max_w(560.0).wrap())
        .child(field.mt(10.0))
        .child(row().gap(18.0).mt(2.0)
            .child(ta(format!("{total} ready to install"), 12.5, 520.0, 0xffffff, 0.66))
            .child(ta("·", 12.5, 520.0, 0xffffff, 0.4))
            .child(ta(if s.online { "Connected to Kova" } else { "Browsing offline" }, 12.5, 520.0, 0xffffff, 0.66)));
    let frame = el().key("hero").w_full().h(340.0).radius(28.0).clip_rounded(28.0)
        .child(fx(MESH, [4.0, 0.16, 3.0, 0.16]).absolute(0.0, 0.0).w_full().h(340.0).radius(28.0))
        .child(fx(GRAIN, [0.06, 0.0, 0.0, 0.0]).absolute(0.0, 0.0).w_full().h(340.0).radius(28.0))
        // Darken the left so the copy reads on any frame of the animation.
        .child(el().absolute(0.0, 0.0).w_full().h(340.0)
            .linear_gradient(90.0, [(0.0, ca(0x05060d, 0.72)), (0.55, ca(0x05060d, 0.25)), (1.0, ca(0x05060d, 0.0))]))
        .child(copy.absolute(48.0, 46.0))
        .border(1.0, ca(0xffffff, 0.08));
    lift(frame, 1.4)
}

// ---------------------------------------------------------------------------------------------
// Filters

fn filters(s: &State) -> El {
    let count = |f: Filter| s.entries.iter().filter(|e| f.admits(e)).count();
    let mut bar = row().key("filters").gap(8.0).center_y();
    for f in [Filter::All, Filter::Bot, Filter::Plugin, Filter::Skill] {
        let on = s.filter == f;
        bar = bar.child(row().key(format!("filter-{}", f.label())).h(36.0).px(16.0).gap(8.0).center_y().radius(18.0)
            .fill(if on { c(TEXT) } else { ca(0xffffff, 0.05) })
            .border(1.0, if on { c(TEXT) } else { hair() })
            .hover_fill(if on { c(TEXT) } else { ca(0xffffff, 0.09) })
            .press_scale(0.96).motion(rune_motion::Spring::SNAPPY)
            .role("tab").label(f.label()).focusable(true)
            .on_press(move |s: &mut State| s.filter = f)
            .child(t(f.label(), 13.5, 560.0, if on { CANVAS } else { TEXT }))
            .child(t(count(f).to_string(), 12.0, 560.0, if on { 0x55555f } else { FAINT })));
    }
    bar
}

// ---------------------------------------------------------------------------------------------
// Featured

fn featured(s: &State) -> El {
    // One standout of each kind: first-party, available, not yet installed if possible.
    let pick = |kind: &str| s.entries.iter()
        .filter(|e| e.kind == kind && e.available())
        .min_by_key(|e| (e.installed_version.is_some(), !e.tags.iter().any(|t| t == "first-party"), e.name.clone()));
    let picks: Vec<&Entry> = ["bot", "plugin", "skill"].iter().filter_map(|k| pick(k)).collect();
    let mut grid = row().w_full().gap(20.0);
    for (i, entry) in picks.iter().enumerate() {
        grid = grid.child(feature_card(s, entry, i == 0));
    }
    col().w_full().gap(16.0)
        .child(section_head("Featured", "Hand-picked to start with."))
        .child(grid)
}

fn feature_card(s: &State, entry: &Entry, wide: bool) -> El {
    let (mark, data) = art::cover(entry);
    let hovered = s.hovered.as_deref() == Some(&entry.name);
    let h = 300.0;
    let name = entry.name.clone();
    let hover_name = entry.name.clone();
    let body = col().absolute(0.0, 0.0).w_full().h(h).padding(26.0).gap(8.0)
        .child(row().w_full().center_y().child(pill(entry.kind_label(), true)).child(spacer())
            .child(match entry.state_label() { Some(label) => pill(label, true), None => el() }))
        .child(spacer())
        .child(row().gap(14.0).center_y().child(icon_tile(entry, 48.0)).child(t(entry.title(), 26.0, 680.0, 0xffffff)))
        .child(ta(entry.description.clone(), 14.0, 440.0, 0xffffff, 0.84).w_full().max_lines(2))
        .child(row().mt(10.0).gap(10.0).center_y()
            .child(primary_button(entry, true))
            .child(ta(format!("by {} · v{}", entry.author, entry.version), 12.5, 500.0, 0xffffff, 0.6)));
    let card = el().key(format!("feature-{}", entry.name)).h(h).grow(if wide { 1.35 } else { 1.0 }).radius(24.0)
        .clip_rounded(24.0).border(1.0, ca(0xffffff, if hovered { 0.22 } else { 0.1 }))
        .child(fx(mark, data).absolute(0.0, 0.0).w_full().h(h).radius(24.0))
        .child(el().absolute(0.0, 0.0).w_full().h(h)
            .linear_gradient(180.0, [(0.0, ca(0x000000, 0.08)), (0.4, ca(0x000000, 0.28)), (0.7, ca(0x000000, 0.7)), (1.0, ca(0x000000, 0.9))]))
        .child(body)
        .scale(if hovered { 1.012 } else { 1.0 }).motion(rune_motion::Spring::SMOOTH)
        .press_scale(0.985).role("button").label(entry.title()).focusable(true)
        .on_hover_change(move |s: &mut State, on, _| hover(s, &hover_name, on))
        .on_press(move |s: &mut State| open(s, &name));
    let card = lift(card, if hovered { 1.8 } else { 1.2 });
    glow_behind(card, hovered, 24.0, h)
}

// ---------------------------------------------------------------------------------------------
// Shelves and cards

fn section_head(title: &str, blurb: &str) -> El {
    let mut head = col().gap(4.0).child(t(title, 22.0, 660.0, TEXT));
    if !blurb.is_empty() {
        head = head.child(t(blurb, 14.0, 430.0, MUTED));
    }
    head
}

fn shelf(s: &State, title: &str, blurb: &str, items: &[&Entry]) -> El {
    let mut grid = el().w_full().auto_fill_columns(280.0).gap(20.0);
    for entry in items {
        grid = grid.child(card(s, entry));
    }
    col().w_full().gap(16.0).child(section_head(title, blurb)).child(grid)
}

fn card(s: &State, entry: &Entry) -> El {
    let (mark, data) = art::cover(entry);
    let hovered = s.hovered.as_deref() == Some(&entry.name);
    let cover_h = 150.0;
    let name = entry.name.clone();
    let hover_name = entry.name.clone();
    let cover = el().w_full().h(cover_h).clip_rounded(CARD_RADIUS)
        .child(fx(mark, data).absolute(0.0, 0.0).w_full().h(cover_h))
        .child(el().absolute(0.0, 0.0).w_full().h(cover_h)
            .linear_gradient(180.0, [(0.5, ca(0x000000, 0.0)), (1.0, ca(0x000000, 0.42))]))
        .child(icon_tile(entry, 52.0).absolute(16.0, cover_h - 68.0))
        .child(pill(entry.kind_label(), true).absolute(14.0, 14.0));
    let footer = row().w_full().gap(8.0).center_y()
        .child(t(if entry.author.is_empty() { "Community".to_string() } else { entry.author.clone() }, 12.5, 520.0, FAINT))
        .child(t("·", 12.5, 520.0, FAINT))
        .child(t(format!("v{}", entry.version), 12.5, 520.0, FAINT))
        .child(spacer())
        .child(match entry.state_label() {
            Some(label) => state_badge(label),
            None => t("Get", 13.0, 640.0, ACCENT),
        });
    let body = col().w_full().padding(18.0).gap(6.0)
        .child(t(entry.title(), 16.0, 620.0, TEXT))
        .child(t(entry.description.clone(), 13.0, 430.0, MUTED).w_full().max_lines(2).h(36.0))
        .child(footer.mt(8.0));
    let card = sheen(col().key(format!("card-{}", entry.name)).w_full().radius(CARD_RADIUS)
        .fill(c(SURFACE)).border(1.0, if hovered { ca(0xffffff, 0.16) } else { hair() })
        .child(cover).child(body))
        .offset(0.0, if hovered { -3.0 } else { 0.0 }).motion(rune_motion::Spring::SMOOTH)
        .press_scale(0.985).role("button").label(entry.title()).focusable(true)
        .on_hover_change(move |s: &mut State, on, _| hover(s, &hover_name, on))
        .on_press(move |s: &mut State| open(s, &name));
    let card = lift(card, if hovered { 1.5 } else { 0.8 });
    glow_behind(card, hovered, CARD_RADIUS, 0.0)
}

/// A soft coloured glow under a hovered card, drawn on the GPU behind it.
fn glow_behind(card: El, on: bool, radius: f32, h: f32) -> El {
    if !on {
        return card;
    }
    let glow = fx(GLOW, [0.0, 0.55, 26.0, 0.0]).absolute(0.0, 0.0).w_full().radius(radius).effect_bleed(60.0);
    let glow = if h > 0.0 { glow.h(h) } else { glow.h_full() };
    el().grow(1.0).w_full().child(glow).child(card)
}

/// A frosted-glass app icon: the entry's initials on a tile that reads over any cover art.
fn icon_tile(entry: &Entry, size: f32) -> El {
    sheen(row().size(size, size).center().radius(size * 0.28)
        .fill(ca(0x0a0a10, 0.42)).backdrop_blur(16.0).border(1.0, ca(0xffffff, 0.2))
        .child(ta(art::monogram(entry), size * 0.4, 700.0, 0xffffff, 0.95)))
}

fn pill(label: &str, on_art: bool) -> El {
    row().h(24.0).px(10.0).center_y().radius(12.0)
        .fill(if on_art { ca(0x000000, 0.32) } else { ca(0xffffff, 0.06) })
        .backdrop_blur(if on_art { 12.0 } else { 0.0 })
        .border(1.0, ca(0xffffff, 0.14))
        .child(ta(label, 11.5, 600.0, 0xffffff, 0.92))
}

fn state_badge(label: &str) -> El {
    let color = match label {
        "Installed" => SUCCESS,
        "Update available" => ACCENT,
        _ => FAINT,
    };
    row().gap(6.0).center_y()
        .child(el().size(6.0, 6.0).radius(3.0).fill(c(color)))
        .child(t(label, 12.0, 560.0, color))
}

fn primary_button(entry: &Entry, on_art: bool) -> El {
    let label = if !entry.available() {
        "Coming soon"
    } else if entry.update_available {
        "Update"
    } else if entry.installed_version.is_some() {
        "Installed"
    } else {
        "Get"
    };
    let strong = entry.available() && (entry.update_available || entry.installed_version.is_none());
    row().h(38.0).px(20.0).center().radius(19.0)
        .fill(if strong { c(if on_art { 0xffffff } else { TEXT }) } else { ca(0xffffff, 0.12) })
        .child(t(label, 13.5, 650.0, if strong { CANVAS } else { TEXT }))
}

fn empty() -> El {
    col().w_full().h(260.0).center().gap(8.0).radius(24.0).fill(c(SURFACE)).border(1.0, hair())
        .child(t("Nothing matches that", 18.0, 620.0, TEXT))
        .child(t("Try fewer words, or another kind.", 14.0, 430.0, MUTED))
}

// ---------------------------------------------------------------------------------------------
// Detail sheet

fn sheet(s: &State, entry: &Entry) -> El {
    let (mark, data) = art::cover(entry);
    let header_h = 230.0;
    let header = el().w_full().h(header_h).clip_rounded(26.0)
        .child(fx(mark, data).absolute(0.0, 0.0).w_full().h(header_h))
        .child(el().absolute(0.0, 0.0).w_full().h(header_h)
            .linear_gradient(180.0, [(0.3, ca(0x000000, 0.0)), (1.0, ca(0x000000, 0.65))]))
        .child(col().absolute(32.0, header_h - 128.0).gap(10.0)
            .child(row().gap(8.0).child(pill(entry.kind_label(), true)).child(pill(&format!("v{}", entry.version), true)))
            .child(row().gap(16.0).center_y().child(icon_tile(entry, 56.0)).child(t(entry.title(), 34.0, 700.0, 0xffffff))))
        .child(row().absolute(SHEET_W - 56.0, 16.0).size(36.0, 36.0).center().radius(18.0)
            .fill(ca(0x000000, 0.35)).backdrop_blur(12.0).border(1.0, ca(0xffffff, 0.16))
            .hover_fill(ca(0x000000, 0.5)).press_scale(0.92).role("button").label("Close").focusable(true)
            .on_press(|s: &mut State| close(s))
            .child(ta("✕", 14.0, 600.0, 0xffffff, 0.9)));
    let mut info = col().w_full().gap(18.0).padding(32.0)
        .child(t(entry.description.clone(), 16.0, 440.0, TEXT).w(SHEET_W - 64.0).wrap())
        .child(row().gap(10.0).center_y()
            .child(t(format!("by {}", if entry.author.is_empty() { "Community" } else { &entry.author }), 13.0, 520.0, MUTED))
            .child(match entry.state_label() { Some(label) => state_badge(label), None => el() }))
        // The action sits right under the title, as in an app store; no scrolling to install.
        .child(install_area(s, entry));
    if !entry.permissions.is_empty() {
        let mut list = col().gap(10.0);
        for line in &entry.permissions {
            list = list.child(row().gap(12.0).items_start()
                .child(el().mt(6.0).size(8.0, 8.0).radius(4.0).fill(c(ACCENT)))
                .child(t(line.clone(), 14.0, 440.0, MUTED).grow(1.0).wrap()));
        }
        info = info.child(panel("What it may do", list));
    }
    if !entry.tags.is_empty() {
        let mut tags = row().gap(8.0).flex_wrap();
        for tag in entry.tags.iter().filter(|t| *t != "first-party") {
            tags = tags.child(pill(tag, false));
        }
        info = info.child(tags);
    }
    if entry.available() && !entry.source.sha256.is_empty() {
        info = info.child(col().gap(4.0)
            .child(mono(format!("Source  {}", entry.source.url), 11.5, FAINT))
            .child(mono(format!("SHA-256 {}…", &entry.source.sha256[..entry.source.sha256.len().min(32)]), 11.5, FAINT)));
    }
    let panel = lift(col().key("sheet").w(SHEET_W).max_h(760.0).radius(26.0).fill(c(SURFACE)).border(1.0, ca(0xffffff, 0.1))
        .scroll(ScrollSpec { stick_bottom: false })
        .child(header).child(info), 2.4)
        .modal(true, |s: &mut State| close(s));
    el().key("sheet-backdrop").absolute(0.0, 0.0).w_full().h_full().center()
        .fill(ca(0x000000, 0.55)).backdrop_blur(18.0)
        .child(panel)
}

fn panel(title: &str, body: El) -> El {
    col().w_full().gap(12.0).padding(18.0).radius(16.0).fill(c(RAISED)).border(1.0, hair())
        .child(t(title, 13.0, 640.0, TEXT))
        .child(body)
}

fn install_area(s: &State, entry: &Entry) -> El {
    let button = |label: &str, strong: bool| row().h(44.0).px(24.0).center().radius(22.0)
        .fill(if strong { c(TEXT) } else { ca(0xffffff, 0.08) })
        .hover_fill(if strong { c(0xffffff) } else { ca(0xffffff, 0.12) })
        .press_scale(0.96).role("button").label(label).focusable(true)
        .child(t(label, 14.0, 650.0, if strong { CANVAS } else { TEXT }));
    if !entry.available() {
        return t("Coming soon. It isn't ready to install yet.", 14.0, 500.0, MUTED);
    }
    if !s.online {
        return col().gap(6.0)
            .child(t("Start Kova to install", 15.0, 620.0, TEXT))
            .child(t("This window is showing the catalog that ships with Kova. Run kova serve, then reopen it.", 13.5, 440.0, MUTED).wrap());
    }
    let upgrade = entry.installed_version.is_some();
    match &s.install {
        Install::Idle if entry.installed_version.is_some() && !entry.update_available => {
            state_badge("Installed")
        }
        Install::Idle => {
            let name = entry.name.clone();
            let version = entry.version.clone();
            row().gap(12.0).center_y()
                .child(button(if upgrade { "Check update" } else { "Get" }, true).key("install-get")
                    .on_press(move |s: &mut State| crate::preview(s, &name, &version)))
                .child(t("Kova downloads it, checks its checksum, and shows you everything it adds first.", 13.0, 440.0, FAINT).max_w(380.0).wrap())
        }
        Install::Checking => row().gap(10.0).center_y()
            .child(fx(SHIMMER, [0.6, 1.2, 0.4, 0.0]).size(120.0, 6.0).radius(3.0))
            .child(t("Downloading and verifying…", 13.5, 520.0, MUTED)),
        Install::Confirm(preview) => {
            let ticket = preview.get("ticket").and_then(Value::as_str).unwrap_or("").to_owned();
            col().w_full().gap(14.0).padding(18.0).radius(16.0).fill(ca(0x6d9cfe, 0.06)).border(1.0, ca(0x6d9cfe, 0.35))
                .child(row().gap(8.0).center_y()
                    .child(el().size(8.0, 8.0).radius(4.0).fill(c(SUCCESS)))
                    .child(t("Checksum verified", 13.5, 640.0, TEXT)))
                .child(adds(preview))
                .child(row().gap(10.0)
                    .child(button("Cancel", false).on_press(|s: &mut State| crate::cancel(s)))
                    .child(button(if upgrade { "Update" } else { "Install" }, true).key("install-confirm")
                        .on_press(move |s: &mut State| crate::install(s, &ticket, upgrade))))
        }
        Install::Installing => t("Installing…", 14.0, 560.0, MUTED),
        Install::Done(what) => row().gap(10.0).center_y()
            .child(el().size(10.0, 10.0).radius(5.0).fill(c(SUCCESS)))
            .child(t(format!("Installed {what}."), 15.0, 620.0, TEXT)),
        Install::Failed(why) => col().gap(10.0)
            .child(t(why.clone(), 13.5, 500.0, DANGER).w_full().wrap())
            .child(button("Try again", false).on_press(|s: &mut State| s.install = Install::Idle)),
    }
}

/// What a verified download adds, from the preview the server returned.
fn adds(preview: &Value) -> El {
    let mut out = col().gap(10.0);
    let lines = |v: Option<&Value>, f: &dyn Fn(&Value) -> String| -> Vec<String> {
        v.and_then(Value::as_array).map(|a| a.iter().map(f).collect()).unwrap_or_default()
    };
    let group = |title: &str, items: Vec<String>| -> El {
        let mut g = col().gap(4.0).child(t(title, 12.0, 640.0, MUTED));
        for item in items {
            g = g.child(t(item, 13.0, 440.0, TEXT).w_full().wrap());
        }
        g
    };
    let text = |v: &Value, k: &str| v.get(k).and_then(Value::as_str).unwrap_or("").to_owned();
    if let Some(bot) = preview.get("bot") {
        let mut chips = row().gap(6.0).flex_wrap();
        for tool in lines(bot.get("tools"), &|v| v.as_str().unwrap_or("").to_owned()) {
            chips = chips.child(row().h(26.0).px(10.0).center_y().radius(8.0).fill(ca(0xffffff, 0.06)).border(1.0, hair())
                .child(mono(tool, 12.0, TEXT)));
        }
        out = out.child(col().gap(6.0).child(t("Tools it may use", 12.0, 640.0, MUTED)).child(chips));
        out = out.child(group("Settings", vec![
            if text(bot, "ceiling") == "manual" { "Asks before each action".into() } else { format!("Approval: {}", text(bot, "ceiling")) },
            format!("Model: {}", if text(bot, "model").is_empty() { "your active model".into() } else { text(bot, "model") }),
        ]));
        let notes = lines(preview.get("notes"), &|v| v.as_str().unwrap_or("").to_owned());
        if !notes.is_empty() {
            out = out.child(group("Changed for this computer", notes));
        }
        out = out.child(group("Persona", vec![text(bot, "persona").lines().take(8).collect::<Vec<_>>().join("\n")]));
    }
    if let Some(plugin) = preview.get("plugin") {
        out = out.child(group("Slash commands", lines(plugin.get("commands"), &|v| format!("/{}", text(v, "name")))));
        out = out.child(group("Skills", lines(plugin.get("skills"), &|v| text(v, "name"))));
        let mcp = lines(plugin.get("mcp"), &|v| text(v, "name"));
        if !mcp.is_empty() {
            out = out.child(group("Programs it runs (MCP)", mcp));
        }
    }
    if let Some(skill) = preview.get("skill") {
        out = out.child(group("Instructions it adds", vec![text(skill, "body").lines().take(10).collect::<Vec<_>>().join("\n")]));
    }
    out
}

// ---------------------------------------------------------------------------------------------
// Handlers

fn hover(s: &mut State, name: &str, on: bool) {
    if on {
        s.hovered = Some(name.to_owned());
    } else if s.hovered.as_deref() == Some(name) {
        s.hovered = None;
    }
}

fn open(s: &mut State, name: &str) {
    s.detail = Some(name.to_owned());
    s.install = Install::Idle;
}

fn close(s: &mut State) {
    if matches!(s.install, Install::Confirm(_)) {
        crate::cancel(s);
    }
    s.detail = None;
}
