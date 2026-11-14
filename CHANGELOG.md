# Changelog

All notable changes to this project are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

- Add web/desktop slash commands, a mobile command picker, conversation forking and compaction controls.

### Added

- Session picker gains fork: `/fork` copies the current chat (or `/fork 2` any earlier one), and **Ctrl+S** in the `/resume` picker forks the highlighted chat; forks keep messages and compacted context and continue independently.
- Remappable keyboard shortcuts: bind every chat action under `[tui.keys]` in `config.toml`.
- Light terminal theme: set `theme = "light"` under `[tui]`; colours adapt to a light background before terminal colour-depth downscaling.
- Progressive onboarding tips in the chat: one short tip per start instead of a one-shot key list.
- Onboarding names your assistant and picks a tone (warm, concise, professional, playful); the identity is used in the system prompt, the chat UI, the web UI and the desktop window and tray.
- Providers declare their capabilities once — tool calling, reasoning stream, sign-in style, address and header fields — and the CLI, terminal chat and web app offer only what is declared; the server refuses a forced unsupported action.
- `kova-web` command opens Kova's web UI in your browser.
- Terminal shortcut `Ctrl+T` toggles full model reasoning, streamed live.
- Terminal shortcut `Ctrl+O` expands and collapses tool calls and their output.
- Live streaming of commands and their output, and of code, as the model writes them.
- Queue a message for after the running turn, or steer the running turn itself, in the terminal and web UI. Queued messages are persisted and survive reloads.
- Per-turn change review with diffs and local, Git-independent restore points (`/changes`, `/diff`, `/restore`).
- Layered `KOVA.md` / `AGENTS.md` guidance from the Kova home, project root and working folder, with a 16 KiB budget and clear precedence.
- Linux Landlock sandbox for `run_command`: safe commands run without a prompt but cannot write outside the working folder.
- `kova doctor` now checks the command sandbox, port ownership, Node/npm for `kova-app`, token permissions, free disk and config schema, prints a verdict with a next step, and supports `--json`; the README documents every line.

### Changed

- `/health` answers from a 15-second cached model status refreshed in the background (`?probe=1` forces a live check), and the web poll pauses while hidden and backs off to 2–5 minutes, so CLI commands and open windows no longer wait on a down model.
- Approvals and `ask_user` questions are announced to screen readers and move keyboard focus to their first decision button (the transcript itself stays `aria-live="off"`); the web UI announces how a turn ended and returns focus to the composer after an answer.
- `kova-app` now launches the desktop app (native window; `--browser` for your browser).
- `docs/ARCHITECTURE.md` now documents all 72 endpoints with their auth, plus the SSE event vocabulary; `uv run pytest -k architecture` fails when the table and the code drift apart.
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
