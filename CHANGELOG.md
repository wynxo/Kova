# Changelog

All notable changes to this project are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- Per-turn file diffs with local, git-independent checkpoints and restore controls in the web UI and TUI.
- `kova-web` command opens Kova's web UI in your browser.
- Terminal shortcut `Ctrl+T` toggles full model reasoning, streamed live.
- Terminal shortcut `Ctrl+O` expands and collapses tool calls and their output.
- Live streaming of commands and their output, and of code, as the model writes them.

### Changed

- `kova-app` now launches the desktop app (native window; `--browser` for your browser).
- Redesigned desktop and web UI, with a new app icon.

## History

Notable merged pull requests, most recent first:

- **#23** — Electron desktop app: sandboxed shell, startup flow, and package targets, plus launcher wiring and optional Linux menu registration.
- **#22** — Fix web app behavior (streaming, provider setup, navigation, API panels).
- **#21** — Terminal chat redesign; brand the CLI screens and setup pickers.
- **#20** — Fix web visuals; rebuild the app stylesheet and bundle Inter and JetBrains Mono.
- **#19** — Bulletproof first chat; harden provider setup, server startup, and first-chat failures.
- **#18** — Providers; redesign model connections around explicit providers.
- **#17** — Update/install/reinstall/uninstall launcher scripts.
- **#16** — Kaine TUI; port the full-screen terminal chat from kaine-cli.
- **#15** — Fix agent edge bugs.
- **#14** — Agent core.
- **#13** — Server finder.
- **#12** — Fix memory edge bugs.
- **#11** — Agent memory edge tests.
- **#10** — Architecture contributing docs.
