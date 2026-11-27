# Kova native app

Kova's desktop app rendered on the GPU with [Rune](https://github.com/anomalyco/rune), a
Rust UI framework. Like the terminal chat and the web app, it is a thin client of the local
Kova server: it reads `KOVA_HOME/token` and the port in `KOVA_HOME/config.toml`, and talks to
the same HTTP API. Nothing here stores chats, settings or extensions of its own.

It has two pages so far. **Images** draws pictures on this computer: it shows what the
hardware can run, rates every image model for it with the recommended one marked, downloads
the one you pick with progress, and keeps a gallery with a full-size viewer (see
[docs/IMAGES.md](../docs/IMAGES.md)). It opens on the **Marketplace**: live GPU artwork for every bot, plugin and skill, a featured
shelf, a shelf per kind, search, and a detail sheet that runs the server's install flow
(download → checksum → show what it adds → confirm). With no server running it shows the
catalog bundled with Kova and turns installs off.

## Build and run

Needs the Rust toolchain in `rust-toolchain.toml` (rustup installs it on first build). On Linux
also `libxkbcommon-dev` and `libfontconfig1-dev`.

```sh
cd native
cargo run --release
```

## See it without a window

Rune renders headless, so every screen can be checked as a PNG, including on a machine with no
GPU (Mesa's software Vulkan driver is enough, just slower):

```sh
cargo build --release --features snap
./target/release/kova-app --scene market  --snap market.png  --size 1440x900 --at 1
./target/release/kova-app --scene detail  --snap detail.png  --size 1440x900 --at 1
./target/release/kova-app --scene confirm --snap confirm.png --size 1440x900 --at 1
```

Scenes: `market`, `hover`, `detail`, `confirm`, `bots`, `offline`, `images`, `downloading`,
`gallery` (pictures from a folder: `KOVA_APP_PICTURES=dir`), and `preview` (renders a
preview saved from a real server: `KOVA_APP_PREVIEW=preview.json`). Scripts drive the real flow
against a running server, for example tapping `feature-tutor`, then `install-get`, then
`install-confirm` (`--realtime --script flow.json`).

## Where things are

| File | What it holds |
| --- | --- |
| `src/main.rs` | state, the sidebar shell, server calls on worker threads, scenes |
| `src/market.rs` | the Marketplace: hero, filters, featured, shelves, detail sheet, install |
| `src/images.rs` | the Images page: hardware, composer, rated models, downloads, gallery, viewer |
| `src/art.rs` | which live artwork each catalog entry wears, and its icon letters |
| `src/gfx.rs` | the compositor that draws `rune-fx` effects (mesh gradients, liquid metal, glow) |
| `src/api.rs` | the blocking client for the local server |
| `src/data.rs` | catalog entries, with the bundled index as the offline copy |
| `src/style.rs` | colours and type, after Astryx's Neutral theme |

Every Rune line in `Cargo.toml` pins the same revision; bump them together, with the toolchain.
`src/gfx.rs` is adapted from Rune's showreel example (MIT, © Anomaly).
