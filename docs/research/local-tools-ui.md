# Local AI Coding Tools — UI/UX Research

Research-only. Sources are what is **installed on this machine**. Every claim is
labelled **VERIFIED** (a command run, a file read, or strings found in a binary)
or **UNSURE**. No secrets/tokens/API-keys were read (auth.json, auth.lock,
service.json, remote-settings were inspected only for key *names*, values
redacted). Interactive paid-model sessions were never started.

Kova reference: `src/kova/tui/*` (app.py, widgets.py, style.py, theme.py,
markdown.py, render.py, terminal.py, prompts.py, brand.py).

---

## 0. What is installed (VERIFIED via `which`)

| Tool | Path | Type | Version |
|---|---|---|---|
| claude | `~/.local/bin/claude` → `versions/2.1.323` | Rust ELF binary | Claude Code 2.1.323 |
| codex | `~/.local/bin/codex` → `packages/standalone` | Rust ELF binary | standalone (Rust) |
| omp | `~/.local/bin/omp` | Rust ELF binary | omp 18.20.0 |
| pi | `~/.local/bin/pi` → node bundle | Node JS | pi 1.4.0 |
| opencode | `~/.opencode/bin/opencode` | Rust ELF binary | (opencode.ai) |
| hermes | `~/.local/bin/hermes` | POSIX shell → `hermes-agent` | (Rust+Electron) |
| openclaw | `~/.openclaw/tools/node/bin/openclaw` | Node JS | OpenClaw 2026.10.9 |
| herdr | `~/.local/bin/herdr` | Rust ELF binary | terminal workspace manager |

All 8 exist. Additional related binaries in `~/.local/bin`: `goose`, `hc`,
`herdr-app`, `herdr-core`, `hermes-acp`, `hermes-agent`, `kaine*`, `locus*`,
`orca`, `orca-ide`, `pi-vscode`.

---

## 1. Claude Code (`claude`) — VERIFIED

**Slash commands** (extracted from the `2.1.323` binary strings, by hit count):
`/t` (enhance/prompt; 1921 hits — the most common), `/login`, `/mcp`,
`/config`, `/model`, `/clear`, `/status`, `/init`, `/resume`, `/add`,
`/compact`, `/install`, `/permissions`, `/agents`, `/message`, `/rename`,
`/cost`, `/bug`, `/upgrade`, `/export`, `/doctor`, `/help`, `/cwd`, `/voice`,
`/review`, `/pr`, `/apply`.

**Descriptions** (VERIFIED from binary strings):
- `/compact` → "to shorten the conversation" (context compaction)
- `/clear` → clears the view
- `/model` → switch model (also `/config` Model row, fast-mode promotion)
- `/status` → lists setting sources
- `/permissions` → permission modes (also `/config`)
- `/cost` and `/usage` → cost tracking (resolve to each other)
- `/review` → code review
- `/upgrade` → "to increase your usage limit"
- `/bug` → bug report ("Any other key closes this panel")
- `/voice` → push-to-talk dictation
- `/rename` → rename sessions ("to tell them apart at a glance")
- `/doctor` → prompt audit, hands off to bundled `claude-api` skill
- `/init` → initialize `AGENTS.md`/`CLAUDE.md`

**Keybindings** (VERIFIED from binary strings): present; dominant keys are
`end` (320), `escape` (234), `home` (74), `f1`–`f12`, `page_up`/`page_down`.
Exact default action map is UNSURE (minified), but the keys exist.

**Themes** (VERIFIED): `appearance`, `dark-mode`, `light-mode`, `theme-adapted`,
`theme-vscode`, `theme-only` — theme support + VS Code theme adaptation.

**Config** (VERIFIED files): `~/.claude/settings.json` (settings),
`~/.claude/keybindings.json` — JSON with `"bindings":[{context,bindings:{...}}]`
mapping `ctrl+alt+g` → `"command:orca-agent-view"` (custom command actions),
`~/.claude/CLAUDE.md` (project instructions, auto-discovered).

**Onboarding** (VERIFIED via `--help`): `--bare` mode skips CLAUDE.md
auto-discovery, hooks, LSP, skills, memory — i.e. CLAUDE.md + hooks + skills +
MCP are the onboarding/context model. First run is a settings/setup wizard.

---

## 2. Codex (`codex`) — VERIFIED

**Subcommands** (VERIFIED `codex --help` / `codex help`): `agents` (agent
command center — browse/switch sessions), `exec`, `review`, `login`, `logout`,
`mcp`, `plugin`, `app-server`, `remote-control`, `completion`, `update`,
`doctor`, `sandbox`, `debug`, `apply`, `resume`, `queue`, `archive`, `delete`,
`migrate-*`, `cloud`, `exec-server`, `features`, `help`.

**Slash commands** (VERIFIED from the `codex` binary strings, by count):
`/list`, `/config`, `/mcp`, `/plugin`, `/tool`, `/agent`, `/context`,
`/status`, `/model`, `/skill`, `/resume`, `/voice`, `/update`, `/compact`,
`/review`, `/init`, `/plan`, `/add`, `/memory`, `/help`, `/web`, `/export`,
`/rename`, `/theme`, `/clear`, `/permission`, `/cost`, `/apply`.

**Config `~/.codex/config.toml`** (VERIFIED) — richest appearance config seen:
- `[desktop]`: `appearanceTheme="dark"`, `appearanceDarkCodeThemeId="vercel"`,
  `show-context-window-usage=true`, `followUpQueueMode="steer"`,
  `conversationDetailMode="STEPS_PROSE"`, `defaultTerminalLocation="right"`,
  `preventSleepWhileRunning`, `composerPlainTextMode`, `browser-show-full-url`,
  `avatar-overlay-pet-visible`, `toys-confetti-enabled=true`.
- `enabled-reasoning-efforts = [low,medium,high,xhigh,ultra,persistent,max]`
  (a *range* of reasoning levels, not on/off).
- `[desktop.appearanceDarkChromeTheme]`: `accent="#006efe"`, `surface="#000000"`,
  `ink="#ededed"`, fonts `code="Geist Mono"`, `ui="Geist, Inter"`.
- `[desktop.appearanceDarkChromeTheme.semanticColors]`: `diffAdded="#00AD3A"`,
  `diffRemoved="#F13342"`, `skill="#9540D5"` (per-role colour palette).
- `[projects."<path>"]` `trust_level="trusted"` (per-project trust).
- `[features]` hooks/memories/multi_agent; `[memories]` generate+use.

**Themes** (VERIFIED): full theme + code-theme + semantic-colour system, light/
dark split, custom Chrome theme hex, font families.

**Onboarding** (VERIFIED binary strings): `onboard` (48), `wizard` (8);
`configure` guided setup; `doctor` health check; login via device_code.

**Keybindings** (UNSURE exact map): binary contains `resumeSession` action id
and `end`/`home`/`escape` keys, so a keymap exists, but defaults are not
recoverable from strings.

---

## 3. OMP (`omp`) — VERIFIED

**Flags** (VERIFIED `omp --help`, v18.20.0) — very feature-rich for a TUI:
`--model`, `--smol` (fast model), `--slow` (reasoning), `--plan` (planning),
`--goal`, `--prewalk`, `--provider`, `--thinking <off|minimal|low|medium|
high|xhigh|max|auto>`, `--hide-thinking`, `--advisor` (per-turn reviewer),
`--tools`/`--no-tools`, `--no-lsp`, `--mode <text|json|rpc|rpc-ui>`,
`--models <patterns>` (**Ctrl+P model cycling**), `--spinner-interval`,
`--max-fps`, `--motion <full|reduced|none>`, `--export`, `--warn-cost`,
`--max-cost`, `--auto-approve`, `--approval-mode <always-ask|write|yolo>`,
`--restore` (resume + finish leftover work), `--from-claude`, `--from-codex`,
`--wait-async`, `--max-time`.

**Config `~/.omp/agent/config.yml`** (VERIFIED) — status line + display tuned:
- `statusLine: preset=full, separator=pipe, contextLine=embedded,
  sessionAccent=true, showHookStatus=true, compactThinkingLevel=true,
  transparent=false` — a **rich, configurable status line**.
- `composer.tokenRate=true`.
- `display: subagentLivePreview=true, showTokenUsage=true, showTurnTime=true,
  cacheMissMarker=true, pinnedAgents=collapsed`.
- `tui: titleSpinner=braille, hyperlinks=auto, tight=false`.
- `modelRoles: default, task` (separate default + "smol" task model — **multi
  model roles**).
- `hideThinkingBlock`, `theme.light/dark`, `symbolPreset=unicode`,
  `defaultThinkingLevel=medium`, `checkpoint.enabled`, `vault.enabled`,
  `github.enabled`, `bash.gitGuard=true`.
- `followUpMode=one-at-a-time`, `interruptMode=immediate`, `steeringMode=all`.

**Onboarding** (VERIFIED): `setupVersion: 3` — a completed guided setup.

**Keybindings** (VERIFIED partial): binary contains `pageUp` (52),
`pageDown` (51), `newSession` (45), `toggleThink` (7), `resumeSession` (4),
`f1`–`f12` — so page-scroll, new-session, toggle-thinking exist. Full map UNSURE.

---

## 4. Pi (`pi`) — VERIFIED (best-documented)

**Subcommands**: `install`, `remove`, `uninstall`, `update`, `list`, `config`,
`auth`, `mcp`. Flags: `--model`, `--thinking`, `--models` (Ctrl+P, globs+fuzzy),
`--theme`, `--use-theme`, `--no-themes`, `--extension`, `--skill`,
`--prompt-template`, `--exclude-tools`, `--tui-mode`, `--approve`, `--offline`,
`--export`, `--list-models`.

**Slash commands** (VERIFIED from `docs/slash-commands.md`, authoritative):
- Settings: `/settings`, `/model [provider/model]`, `/thinking [level]`,
  `/scoped-models`, `/login`, `/logout`, `/llama`.
- Sessions: `/new`, `/resume`, `/name`, `/session`, `/tree`, `/fork`,
  `/clone`, `/compact [instructions]`, `/import <path>`.
- Export: `/copy`, `/export [path]`, `/share`, `/bug`.
- Runtime: `/trust`, `/reload` (reload keybindings/extensions/skills/
  templates/themes/context), `/hotkeys` (show shortcuts), `/changelog`, `/quit`.
- Extensions/skills add their own (e.g. `/skill:name`).

**Keybindings** (VERIFIED from `docs/keybindings.md`): named action ids with a
`$schema`, e.g. `app.session.new`, `tui.editor.cursorUp/Down/Left/Right`,
`tui.editor.historyPrevious/Next`, `deleteWordBackward=ctrl+w`,
`deleteToLineStart=ctrl+u`, `deleteToLineEnd=ctrl+k`, `yank=ctrl+y`,
`pageUp/pageDown`, `cursorWordLeft=alt+left`, `cursorLineEnd=ctrl+e`. Custom
`<agent-dir>/keybindings.json` supported. Full default table in the doc.

**Themes** (VERIFIED from `docs/themes.md`): `system` (default — queries the
terminal's foreground/ANSI colours and rebuilds Pi's palette, enforces WCAG
4.5:1 contrast), `dark`, `light`; `light/dark` split; custom palettes; a
reserved `system` name. Terminal-colour adaptation with a 100 ms wait + fallback.

**Settings `~/.pi/agent/settings.json`** (VERIFIED): `hideThinkingBlock`,
`packages=[pi-permission-system, pi-web-access, pi-subagents]`,
`terminal.clearOnShrink`, `terminal.showTerminalProgress=false`,
`editorPaddingX`, `images.blockImages`, `enableSkillCommands`,
`showHardwareCursor=false`, `steeringMode=all`, `tuiMode=fullscreen`,
`theme=dark`.

---

## 5. OpenCode (`opencode`) — VERIFIED

**Subcommands**: `upgrade`, `uninstall`, `acp`, `api`, `debug`, `auth`, `mcp`,
`plugin`, `models`, `stats`, `mini` (minimal interface), `run`, `session`,
`service`, `reload`, `pair`, `serve`. Flags: `--standalone`, `--server`,
`--auto`, `--continue/-c`, `--session/-s`, `--prompt`, `--wizard`,
`--completions`, `--log-level`.

**Config** (VERIFIED files): `~/.config/opencode/opencode.json` — provider
a local server with **local models** (long-context, 65k–131k),
context/output limits; `~/.config/opencode/tui.jsonc` — **TUI plugins**:
`./herdr-tui-session.js` and `orca-opencode-status-tui/tui.js` (a **status TUI
plugin**). `~/.config/opencode/cli.json`, `service.json`.

**Onboarding** (VERIFIED): `--wizard` first-run mode.

---

## 6. Hermes (`hermes`) — VERIFIED

**Subcommands** (VERIFIED `hermes --help`): `chat`, `model`, `moa`, `fallback`,
`worktree`, `browser`, `secrets`, `egress`, `migrate`, `codex-runtime`,
`gateway`, `proxy`, `lsp`, `setup`, `whatsapp`, `slack`, `send`, `logout`,
`auth`, `status`, `pause`, `resume`, `cron`, `webhook`, `peer`, `portal`,
`kanban`, `project`, `hooks`, `doctor`, `verify`, `security`, `approvals`,
`dump`, `debug`, `backup`, `checkpoints`, `import`, `config`, `skin`,
`console`, `pairing`, `skills`, `bundles`, `curator`, `pets`, `clipboard`,
`journey`, `memory`.

**Config `~/.hermes/config.yaml`** (VERIFIED) — display + reliability tuning:
- `display: memory_notifications, compact, cleanup_progress,
  busy_input_mode=interrupt, background_process_notifications=concise,
  bell_on_complete=false, bell_on_prompt=false, streaming=true, skin=default,
  message_reactions=true, personality`.
- `tool_loop_guardrails: warnings_enabled=true, hard_stop_after={
  exact_failure:5, same_tool_failure:8, idempotent_no_progress:5},
  warn_after={...}` — **tool-failure progress/stop behaviour** (VERIFIED).
- `agent.max_turns=90, reasoning_effort=xhigh`.
- `compression`, `prompt_caching`, `stt` (local whisper), `code_execution`,
  `delegation.max_iterations=250`.

**`~/.hermes/ui-preferences.json`** (VERIFIED): `statusbarVisible.v2=true`,
user themes (`vsc-claude-theme` — a VS Code Claude colour theme),
`hermes-desktop-theme-v2=vsc-black`, and a **tips system** — retired tips:
`new-session, skills, messaging, composer-mentions, right-pane, cron,
command-palette` (progressive onboarding hints).

**Onboarding** (VERIFIED): `setup` wizard; `onboarding.seen.profile_build_offered=true`.
Skins via `hermes skin` + `hermes pets` (animated pets).

---

## 7. OpenClaw (`openclaw`) — VERIFIED

**Subcommands** (VERIFIED `openclaw --help`, 2026.10.9): `acp`, `agent`,
`agents`, `approvals`, `attach`, `audit`, `automations`, `backup`, `browser`,
`capability`, `channels`, `chat`, `clawbot`, `completion`, `config`,
`configure`, `connect`, `cron`, `daemon`, `dashboard`, `database`, `devices`,
`directory`, `dns`, `docs`, `doctor`, `exec-approvals`, `exec-policy`,
`file-transfer`, `gateway`, `health`, `help`, `hooks`, `infer`, `logs`, `mcp`,
`memory`, `message`, `migrate`, `models`, `node`, `nodes`, `onboard`, `pairing`,
`plugins`.

**Slash commands** (VERIFIED from `dist/control-ui` strings): `/model`,
`/config`, `/gateway`, `/mcp`, `/docs`, `/plugin`, `/device`, `/cron`, `/node`,
`/hooks`, `/help`, `/compact`, `/message`, `/memory`, `/dashboard`, `/status`,
`/channel`, `/pairing`, `/clear`.

**UI assets** (VERIFIED in `dist/control-ui/assets`): `command-palette`,
`keyboard-shortcuts-dialog`, `theme-catalog`, `theme-color`, `theme-palette`,
`theme-mode-toggle`, `theme-mascot`, `theme-artwork-loader` — a **command
palette, keyboard-shortcuts dialog, and a theme catalog with import**.

**Onboarding** (VERIFIED): `onboard` wizard with `firstRun`, `modelSelect`,
`step1/2/3`, `accessMode=full`; `configure` guided setup.

**Config `~/.openclaw/openclaw.json`** (VERIFIED): `wizard.accessMode=full`,
`agents.defaults.model`, `gateway.mode=local`, `tools.profile=full`,
`hooks.internal.session-memory`.

---

## 8. Herdr (`herdr`) — VERIFIED

**Nature** (VERIFIED `herdr --help`): a **terminal workspace manager** for AI
agents (not an agent itself) — persistent sessions, SSH machines, tabs, panes,
worktrees. Commands: `launch/attach`, `status`, `update`, `completion`,
`server stop/reload-config`, `channel set`, `machine`, `api`, `workspace`,
`worktree`, `tab`, `notification`, `agent`, `pane`, `session`, `integration`.
Flags: `--session`, `--machine`, `--remote`, `--remote-keybindings
(local|server)`, `--handoff`, `--default-config`, `--skill`.

**Config `~/.config/herdr/config.toml`** (VERIFIED): `onboarding=false`;
custom keybindings (reset via `herdr config reset-keys`).

---

## 9. Kova's current TUI (VERIFIED — `src/kova/tui/*`)

**Commands** (`COMMANDS`, app.py): `/help, /new, /resume, /mode [chat|work],
/permissions [manual|auto|full], /web [on|off], /model [name], /cd <folder>,
/think, /clear, /exit|/quit` — **10 commands**.

**Keybindings** (VERIFIED `SHORTCUTS` + `_cmd_help`): pgup/pgdn scroll,
ctrl+t toggle reasoning, ctrl+o toggle tool details, `!` shell, `/` commands,
`@` attach file, `#` remember, esc interrupt, esc esc clear input,
shift+enter new line, shift+tab cycle permissions, ctrl+c stop, ctrl+d exit,
ctrl+l clear screen, tab complete, ↑↓ history.

**Status bar** (VERIFIED `status_bar`): `(mode chip) · model · offline dot ·
permission · plan · web-off · stats · cwd`, with **rank-based truncation** that
drops least-useful parts on narrow terminals (never wraps).

**Themes** (VERIFIED `theme.py` + `style.py`): one dark theme — ACCENT
`#8b7cff` (violet), USER `#5fd7af`, etc.; a **truecolor→256→16→none
downscaler** (`style.py`) that auto-adapts to terminal colour depth; gradient
logo; `CODE_THEME=monokai`. **No light/dark split, no theme engine.**

**Onboarding** (VERIFIED `_welcome_lines`/`welcome`): animated welcome card
(`WELCOME_SECONDS=0.9`) with a shimmering tagline "your private AI" and
model/mode/folder/memory rows; if no model, "Run kova setup to add one."

**Thinking** (VERIFIED): collapsed by default; `/think` + ctrl+t expand;
ctrl+o toggles tool-call details.

**Other**: permissions manual/auto/full (shift+tab cycle); memory via `#`
prefix (MEMORY colour rows); shell via `!` prefix (BASH colour); file attach via
`@`.

---

## 10. Comparison matrix (VERIFIED unless noted)

| Feature | Kova | claude | codex | omp | pi | opencode | hermes | openclaw | herdr |
|---|---|---|---|---|---|---|---|---|---|
| Slash commands | 10 | ~28 | ~29 | config-only | ~30 + ext | few | console | ~19 | n/a |
| Command palette | ❌ | ? | ? | `--models` cycle | `/hotkeys` | ? | tips | **command-palette** | ? |
| Model cycling (Ctrl+P) | ❌ | ? | ? | **yes** | **yes (globs+fuzzy)** | ? | ? | ? | ? |
| Thinking levels (not on/off) | toggle only | ? | effort range | **7 levels** | `/thinking` | ? | effort | ? | ? |
| Rich status line | partial | config | ctx-usage | **full** | ? | status-plugin | statusbar | ? | ? |
| Token/cost/turn-time | stats only | /cost | **usage** | **yes** | ? | stats | ? | ? | ? |
| Themes (light+dark) | dark only | adapt | **full** | light/dark | **system/dark/light** | ? | skins | **catalog** | ? |
| Custom keybindings | fixed | json | ? | ? | **json+schemas** | ? | ? | **dialog** | keybindings |
| Context compaction | /clear only | /compact | /compact | ? | /compact | ? | ? | /compact | ? |
| Session picker/tree | /resume list | /resume | **agents center** | ? | **tree/fork/clone** | session | ? | agents | sessions |
| Onboarding tips | welcome card | CLAUDE.md | onboard | setup | /changelog | wizard | **tips** | **wizard** | onboarding |
| Tool-failure guardrails | ? | ? | sandbox | ? | permission-sys | ? | **hard-stop** | approvals | ? |
| Subagent live preview | ? | ? | ? | **yes** | subagents pkg | ? | ? | ? | ? |
| Custom theme engine | ❌ | VS Code | Chrome hex | — | palette | ? | vsc theme | import | ? |

---

## 11. Ranked — 10 most valuable UI/UX things Kova should adopt

Ranked by user value × feasibility, given Kova already has the basics
(welcome card, status bar, commands, thinking/tool toggles, permissions,
memory, shell, attach).

1. **Searchable command / model / skill palette (Ctrl+P).** OMP (`--models`),
   pi (`/hotkeys`, `--models` globs+fuzzy), OpenClaw (`command-palette`) all
   have one; Kova has only a static `/help` list. Lowest-effort, highest-impact
   discoverability win. **Source: OMP `--help`, pi `docs/slash-commands.md`,
   openclaw `dist/control-ui/assets/command-palette*.js`.**

2. **Visible thinking-level control (not just on/off).** OMP offers 7 levels
   (`--thinking off…max`), Codex a range of reasoning efforts, pi `/thinking`,
   Hermes `reasoning_effort`. Kova's thinking is collapsed↔full only; a level
   selector in the composer/status line is a big quality lever for local models.
   **Source: omp `agent/config.yml`, codex `config.toml`, pi `slash-commands.md`.**

3. **Token / cost / turn-time in the status line.** OMP `statusLine` +
   `showTokenUsage/showTurnTime/cacheMissMarker`; Codex `show-context-window-
   usage`; Claude `/cost`. Kova's status bar has a `stats` slot but no
   token/cost/turn-time. Incremental — reuse the existing rank-based truncation.
   **Source: omp `agent/config.yml`, codex `config.toml`.**

4. **Context compaction (`/compact`).** Claude, Codex, pi, OpenClaw all expose a
   compaction command; Kova only has `/clear` (screen only). Essential for long
   local sessions that blow the context window. **Source: claude/codex/pi/
   openclaw binary strings.**

5. **Session picker / tree (not just `/resume` list).** pi `/tree /fork /clone`,
   Codex `agents` command center, Claude `/resume`. A searchable session list
   with rename/clone beats a linear resume list. **Source: pi
   `slash-commands.md`, codex `--help`.**

6. **Custom keybindings.** pi (`keybindings.json` + `$schema` + named actions),
   OpenClaw (`keyboard-shortcuts-dialog`), Claude (`keybindings.json` with
   `command:` actions). Kova's keymap is hardcoded in `widgets.py`/`app.py`.
   **Source: pi `docs/keybindings.md`, `~/.claude/keybindings.json`,
   openclaw `keyboard-shortcuts-dialog*.js`.**

7. **Theme engine + light/dark.** pi `system`/`dark`/`light` with terminal-
   colour adaptation + WCAG contrast; Codex full Chrome-theme + semantic-colour
   system; OpenClaw theme catalog + import. Kova has one dark theme in
   `theme.py`. Kova already owns the truecolor downscaler — a theme layer is a
   natural extension. **Source: pi `docs/themes.md`, codex `config.toml`,
   openclaw `theme-catalog*.js`.**

8. **Progressive onboarding tips.** Hermes retires a tip per feature
   (`new-session, skills, command-palette, …`); OpenClaw a multi-step `onboard`
   wizard; pi `/changelog`. Kova's welcome card is one-shot. **Source:
   `~/.hermes/ui-preferences.json`, openclaw control-ui strings, pi
   `slash-commands.md`.**

9. **Tool-call transparency: cache markers, thinking-level pill, per-turn time,
   subagent live preview.** OMP `cacheMissMarker`, `compactThinkingLevel`,
   `subagentLivePreview`, `showTurnTime`. Kova renders tool rows but shows no
   cache/level/time signal. **Source: omp `agent/config.yml`.**

10. **Approval / tool-failure guardrails with a visible indicator.** Hermes
    `tool_loop_guardrails` (hard-stop after N failures) + visible approval mode;
    Codex `sandbox` permissions; OMP `approval-mode`. Kova has permission modes
    (manual/auto/full) but no visible per-action approval state or failure
    guardrails. **Source: `~/.hermes/config.yaml`, codex `config.toml`,
    omp `--help`.**

Honourable mentions: `--advisor` (OMP per-turn reviewer injected into the turn),
`--restore` (resume + finish interrupted work), model roles (OMP default vs
`smol` task model), and a `--motion`/spinner-interval accessibility toggle — all
one-flag adds Kova could copy with little code.
