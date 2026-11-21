# Kova documentation

Start with the guide that matches what you are doing. Every page here is current
unless it sits in [`archive/`](archive/), which keeps superseded plans for history.

## Getting started

- [DEMO.md](DEMO.md) — the two-minute judge demo: terminal and web on one server, approvals and file changes.
- [remote-access.md](remote-access.md) — use Kova from a phone on the same Wi-Fi by pairing it once.
- [screenshots/](screenshots/README.md) — the web, desktop and terminal screenshot gallery, with capture notes.

## Using Kova

- [SHORTCUTS.md](SHORTCUTS.md) — every key the terminal chat understands.
- [permission-rules.md](permission-rules.md) — how deny/ask/allow rules, modes and the Linux sandbox interact.
- [EXTENSIONS.md](EXTENSIONS.md) — find, install and write plugins and skills from the catalog.

## Configuration

- [CONFIG.md](CONFIG.md) — every `config.toml` key, its default, and the `kova config` commands.
- [THEMES.md](THEMES.md) — theme file format, the two shipped themes and the example gallery.

## Development

- [ARCHITECTURE.md](ARCHITECTURE.md) — how the server, clients, agent loop, store, memory and plugins fit together.
- [ICONS.md](ICONS.md) — the vendored Lucide sprite and why it is not a package.

## Design notes / archive

- [DESIGN.md](DESIGN.md) — the design system: principles, the token pipeline and the rules UI changes follow.
- [research/](research/README.md) — peer-agent research notes, labelled VERIFIED/UNSURE, with sources.
  - [research/design-ui.md](research/design-ui.md) — index to the design research for the redesign epic.
  - [research/design-ui-1-tokens-and-themes.md](research/design-ui-1-tokens-and-themes.md) — tokens, themes, TUI parity, motion and typography.
  - [research/design-ui-2-testing-and-conversation.md](research/design-ui-2-testing-and-conversation.md) — visual/a11y testing, icons and the conversation UI.
  - [research/design-ui-3-layout-settings-and-plan.md](research/design-ui-3-layout-settings-and-plan.md) — streaming, settings, layout and build order.
  - [research/claude-code-codex.md](research/claude-code-codex.md) — notes on Claude Code and Codex.
  - [research/cursor-t3-herdr.md](research/cursor-t3-herdr.md) — notes on Cursor, T3 Code and herdr.
  - [research/hermes-openclaw.md](research/hermes-openclaw.md) — notes on Hermes Agent and OpenClaw.
  - [research/local-tools-ui.md](research/local-tools-ui.md) — UI/UX notes on locally installed coding tools.
  - [research/memory-systems.md](research/memory-systems.md) — notes on Kova's memory and other agents' context systems.
- [archive/](archive/) — superseded planning artifacts, kept for reference.
  - [archive/redesign-backlog.md](archive/redesign-backlog.md) — shell layout decisions from the redesign epic, partly implemented.
  - [archive/superpowers/plans/2026-11-12-kova-core-cli.md](archive/superpowers/plans/2026-11-12-kova-core-cli.md) — implementation plan for the first server + CLI.
  - [archive/superpowers/specs/2026-11-12-kova-core-cli-design.md](archive/superpowers/specs/2026-11-12-kova-core-cli-design.md) — design spec for that first sub-project.
