# Changelog

All notable changes to this project are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

- Code blocks gain a Lines switch beside Wrap and Copy (#189): a quiet gutter of line numbers, remembered per browser like Wrap, aria-hidden and never copied, hidden while a block wraps and left off for fences still streaming or over 2,000 lines.
- A plan with open steps is pinned above the message box while the turn runs (#193): the current step, a progress bar and a disclosure for the whole checklist, fed by the same plan events as the transcript. It hands the plan back to the transcript when the turn ends (never both at once), can be dismissed for that turn, stays one line on phone widths and opens as a sheet there.
- Composer polish (#197): Up in an empty message box browses the messages you sent in that chat (Down moves forward, Escape restores the draft), and it only starts while the caret is on the first line. Typing `@` lists files in the chat's working folder, chosen with the arrow keys and inserted as `@path`; the list is a labelled listbox and the list request returns names only, never contents. Attachment chips show the name and size next to a named remove button, and a pasted screenshot is attached even when the clipboard exposes it as an unnamed item.
- The primary accent is now solid black on light and solid white on dark instead of orange, finishing the monochrome palette; the example themes keep their own accents (#267).
- Reasoning rows are now Collapsed (default), Expanded or Hidden, set in Settings > General and shared across paired devices with the other preferences (#274).
- The web app now applies the chosen theme on load: it paints immediately with no light-to-dark flash and takes the window and toolbar colour from it (#277).
- The web and desktop stylesheets now resolve every colour through the semantic token layer instead of a fixed palette, with a test keeping raw colours out (#278).
- The dev-only component gallery (/app/gallery) now covers menus, dialogs, messages, tool rows, plans, approvals, spinner, diffs, toasts and inline errors, with theme, density and motion switchers (#276).
- The composer gains a per-chat model picker: search models by provider, pin favourites and see vision, tools and context, with the model and reasoning effort kept per chat (#279).
- The README gains a Themes section covering appearance, the monochrome palette, the example themes in `design/themes/` and custom theme files (#280).
- Several web dialogs gain accessible names and the chat actions menu now closes with Escape and returns focus, improving keyboard and screen-reader use (#281).
- The terminal accent and wordmark now match the monochrome palette, with completion selections and accent chips kept readable in both terminal themes (#282).
- Themes apply in the web and desktop app (#174): Settings > General > Theme and the command palette offer System (the new default, following the device live), Light, Dark and every installed theme file. The choice is the `theme` UI preference, so paired devices and `kova prefs export` share it; a browser's earlier local light/dark choice is kept and synced once. A theme file's resolved tokens are written into the semantic custom properties, and the `theme-color` meta tag and the web manifest take the theme's `bg.base`.
- Add a command palette to the web and desktop app (#183). `Ctrl/Cmd+K` (or **Search or run a command** above the conversation list) opens one fuzzy search over new and incognito chats, search, theme, the current conversation's compact/fork/rename/delete, permission modes, every settings dialog, models and recent conversations. The dialog traps focus, moves with arrows/Home/End/PageUp/PageDown, closes on Esc, announces its result count and honours reduced motion. New conversation moved to `Ctrl/Cmd+Shift+K` so the two keys no longer compete; see docs/SHORTCUTS.md.
- Per-chat colour and icon, first slice (#213): a chat may carry one palette colour (a theme token, never free-form hex) and one vendored icon name, stored on the chat with a migration for older databases, shown in the sidebar row and set through `PATCH /chats/{id}` with validation. Picking a colour or icon is cosmetic: unlike a phone-set title or mode, it never marks the chat as written in by a phone.
- Pin a conversation from its sidebar menu: pinned chats stay on top and the rest are grouped into Today, Yesterday, Previous 7 days and Older by local calendar day (#195). `pinned` is stored on the chat, existing databases gain the column without losing anything, and every client shows the same order. A paired phone can pin with its ordinary `chat` capability: a pin is cosmetic, so it neither needs a stopped turn nor counts as the phone writing in that chat.
- Paired phones get a per-device capability set (#229). A new phone may chat and see local models (`chat`, `models:read`); installing or deleting local models (`models:write`), changing providers and model servers (`providers:write`), MCP servers, plugins and skills (`extensions:write`), and approval rules, privacy, the memory folder persona, memory settings or start at login (`settings:write`), and commands, scheduled jobs, MCP tools and files outside Kova Files (`host:exec`) need a capability granted from the computer with `kova devices allow ID CAPABILITY`. Memory writes need `memory:write`. The limits also apply inside the agent: a phone's turns are offered only the tools it may use, a chat a phone wrote in keeps its limits for every later turn, a phone-queued message runs bound to its own row and is never read as local input by the terminal or web app, and a phone can approve, answer, steer or queue nothing beyond its own capabilities. Writing or restoring `KOVA.md`/`AGENTS.md` or git data needs `host:exec` (and the terminal's file index runs git with `core.fsmonitor` off), removing jobs needs `host:exec`, a phone-set chat title or setting limits that chat, the memory folder is never writable as a shared folder, recall never surfaces a phone-limited chat to a stronger turn, backups keep chat limits, and messages queued before the upgrade are treated as a phone's. Phones paired earlier get the defaults. The matrix is in docs/remote-access.md.
- Add the extensions catalog (#137): `kova catalog search|show|install` and an **Extensions** page (Installed · Plugins · Skills) browse a JSON index from `[catalog] url`, with a bundled fallback that works offline. Every install is pinned by version and SHA-256, shows its source and exactly what it adds, and runs nothing until confirmed. `kova skills list|show|rm` and the Extensions page show every skill with its origin (learned by Kova, catalog, yours, plugin) and let you read, edit and delete it (#107). First-party extensions: the `clipboard` (#91) and `morning-briefing` (#95) plugins and two skills; `file-watcher` (#108) is listed as coming soon.
- Add opt-in outbound secret protection: `privacy.secrets = "off" | "redact" | "block"` (default `off`) scans every OpenAI-compatible, Anthropic and Responses request at `ModelClient`, redacts known credential shapes on a deep copy or blocks before any request, reports safe counts in the CLI and web UI, and bypasses only parsed loopback hosts (#78).
- Keep extracted attachment text out of stored messages, search, exports, backups and generated titles; it stays turn-only model context, and `read_attachment` still rereads files by id on later turns. Messages keep a name-only attachment link, so attachment-only turns show the file name after a reload and forked chats keep that link.
- Reap abandoned chat uploads after 24 hours and cap unsent uploads, per-chat attachments and total attachment storage; attachments referenced by saved messages are always retained (#151).
- Fix chat forks copying attachment references without independent files; failed copies roll back the fork.

- Add web/desktop slash commands, a mobile command picker, conversation forking and compaction controls.
- Attach up to three local documents or images per chat turn, with bounded local extraction, file chips, rereading and explicit model vision support (#74).
- Vision is per model: select a vision model to attach images without a reload, turn it on or off in Model settings (persisted to config.toml), and `GET /capabilities?model=…` reports that model's contract with a 404 for an unknown name (#151).
- File shares from the installed web app become native composer attachments uploaded through the existing attachment API and gated by the selected model's vision setting; text and link shares are unchanged. Unredeemed share-draft chats are removed at startup because their in-memory tokens cannot survive a restart.

### Added

- Add an installable app shell, one-use phone share drafts, and opt-in device reminders while the app is alive; closed-app push is not supported.

- Session picker gains fork: `/fork` copies the current chat (or `/fork 2` any earlier one), and **Ctrl+S** in the `/resume` picker forks the highlighted chat; forks keep messages and compacted context and continue independently.
- Remappable keyboard shortcuts: bind every chat action under `[tui.keys]` in `config.toml`.
- Light terminal theme: set `theme = "light"` under `[tui]`; colours adapt to a light background before terminal colour-depth downscaling.
- Progressive onboarding tips in the chat: one short tip per start instead of a one-shot key list.
- Onboarding names your assistant and picks a tone (warm, concise, professional, playful); the identity is used in the system prompt, the chat UI, the web UI and the desktop window and tray.
- Providers declare their capabilities once — tool calling, reasoning stream, sign-in style, address and header fields — and the CLI, terminal chat and web app offer only what is declared; the server refuses a forced unsupported action.
- Start Kova at login with `kova autostart on|off|status`: a per-user systemd unit (or `.desktop` fallback), macOS LaunchAgent, or Windows HKCU Run entry, with no administrator rights and no stale duplicates.
- `kova-web` command opens Kova's web UI in your browser.
- Terminal shortcut `Ctrl+T` toggles full model reasoning, streamed live.
- Terminal shortcut `Ctrl+O` expands and collapses tool calls and their output.
- Live streaming of commands and their output, and of code, as the model writes them.
- Queue a message for after the running turn, or steer the running turn itself, in the terminal and web UI. Queued messages are persisted and survive reloads.
- Per-turn change review with diffs and local, Git-independent restore points (`/changes`, `/diff`, `/restore`).
- Layered `KOVA.md` / `AGENTS.md` guidance from the Kova home, project root and working folder, with a 16 KiB budget and clear precedence.
- Linux Landlock sandbox for `run_command`: safe commands run without a prompt but cannot write outside the working folder.
- `kova doctor` now checks the command sandbox, port ownership, Node/npm for `kova-app`, token permissions, free disk and config schema, prints a verdict with a next step, and supports `--json`; the README documents every line.
- `install.ps1`, `uninstall.ps1` and their `.bat` wrappers are exercised on Windows CI with a disposable home, and `scripts/smoke.sh` now runs as its own CI job on every push.

### Changed

- `/health` answers from a 15-second cached model status refreshed in the background (`?probe=1` forces a live check), and the web poll pauses while hidden and backs off to 2–5 minutes, so CLI commands and open windows no longer wait on a down model.
- Approvals and `ask_user` questions are announced to screen readers and move keyboard focus to their first decision button (the transcript itself stays `aria-live="off"`); the web UI announces how a turn ended and returns focus to the composer after an answer.
- `kova-app` now launches the desktop app (native window; `--browser` for your browser).
- `docs/ARCHITECTURE.md` now documents all 72 endpoints with their auth, plus the SSE event vocabulary; `uv run pytest -k architecture` fails when the table and the code drift apart.
- Redesigned desktop and web UI, with a new app icon.
- Reminder notifications now work on macOS and Windows and fall back from `notify-send` to `gdbus` on Linux; `kova doctor` reports the notifier in use, and the desktop app allows the Notification permission.

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
