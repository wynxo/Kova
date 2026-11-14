# Claude Code vs Codex — research notes for Kova

Research sub-agent output, 2026-11-13. Read-only: nothing in `~/Kova` was modified.

**Subjects:** Claude Code (Anthropic CLI, `claude` 2.1.323 installed locally) and Codex (OpenAI CLI 0.167.2 + desktop app).

**Evidence rules used below:** every claim carries either

- `VERIFIED — <url>` for official docs,
- `VERIFIED — <repo path:line>` for source in a shallow clone of `openai/codex` at `/tmp/kova-research/src-codex`,
- `VERIFIED — <~/.path>` for the local installation,
- `UNSURE` when I could not confirm it from docs, source, or the local install.

No secrets, tokens or credential values were read. `~/.claude/.credentials.json`, `~/.codex/auth.json` and the sensitive parts of `~/.claude/settings.json` were skipped; only key *names* and hook *commands* were inspected.

---

## 0. Local evidence inventory

| What | Path | Notes |
|---|---|---|
| Claude Code binary | `~/.local/bin/claude` → `~/.local/share/claude/versions/2.1.323` | `claude --version` → `2.1.323 (Claude Code)` — VERIFIED |
| Codex binary | `~/.local/bin/codex` → `~/.codex/packages/standalone/current/bin/codex` | `codex --version` → `codex-cli 0.167.2` — VERIFIED |
| Codex desktop app | `/usr/lib/chatgpt/resources/codex`, `app.asar` | Electron app; version `26.1103.60933` in `~/.codex/config.toml` env — VERIFIED |
| Codex home | `~/.codex/` | `AGENTS.md`, `config.toml`, `hooks.json`, `skills/`, `plugins/cache/`, `memories*/`, plus *separate SQLite files per feature* (`state_5.sqlite`, `thread_history_1.sqlite`, `logs_2.sqlite`, `memories_1.sqlite`, `goals_1.sqlite`, `queue_1.sqlite`) — VERIFIED |
| Claude home | `~/.claude/` | `settings.json` (56 KB, hooks-heavy), `keybindings.json`, `CLAUDE.md` (symlink into an Obsidian "second brain" vault), `skills/`, `plugins/marketplaces/`, `projects/`, `file-history/`, `history.jsonl` — VERIFIED |

Interesting local signal: both tools are already used with *external* control planes. `~/.codex/hooks.json` and `~/.claude/settings.json` both wire every lifecycle event to an `orca-agent-hook.sh` dispatcher, and to a git-backed "brain" vault (`brain-sync.sh pull` on `SessionStart`, async push on `PostToolUse`/`Stop`). That is the single strongest real-world signal for what these hooks are for: **state reporting to an external supervisor, and memory sync** — not just linting.

---

# PART 1 — Claude Code

## 1.1 Agent loop and tool design

**Loop shape.** Claude Code is a streaming message loop: model → content blocks → `tool_use` blocks → execute → `tool_result` blocks → model, repeated until the turn ends. The externally observable contract is documented for headless use: `--output-format stream-json --verbose --include-partial-messages` emits newline-delimited JSON events, one per line, including partial message deltas and `tool_use` / `tool_result` blocks. Subagent messages appear in the same stream tagged with `parent_tool_use_id`, so the whole nesting tree can be rebuilt. VERIFIED — https://code.claude.com/docs/en/headless.md

**Tool set** (canonical names, which are the same strings used in permission rules, hook matchers and subagent tool lists). VERIFIED — https://code.claude.com/docs/en/tools-reference.md

- File: `Read`, `Edit`, `Write`, `NotebookEdit`, `Glob`, `Grep`
- Shell: `Bash`, `PowerShell`, `Monitor` (background command whose output lines are fed back to the model mid-conversation; also WebSocket source)
- Web: `WebFetch`, `WebSearch`
- Delegation: `Agent`, `SendMessage`, `ListAgents`, `TaskStop`, `SubagentHandback`
- Planning/state: `EnterPlanMode`, `ExitPlanMode`, `TaskCreate`/`TaskGet`/`TaskList`/`TaskUpdate` (newer), `TodoWrite` (legacy, disabled by default via `CLAUDE_CODE_ENABLE_TASKS=0`)
- Interaction: `AskUserQuestion` (multiple-choice, stays open by default), `PushNotification`, `SendUserFile`
- Code intelligence: `LSP` (definitions, references, type errors)
- Environment: `EnterWorktree`/`ExitWorktree`, `CronCreate`/`CronDelete`/`CronList`, `ScheduleWakeup`
- Meta: `Skill`, `Artifact`, `ReportFindings`, `SendFeedback`, `EndConversation`

Two details worth stealing:

1. `Glob` and `Grep` are **absent by default on macOS/Linux/WSL** — Claude uses `Bash` with `rg`/`find` instead, and the file tools only exist where a shell is a bad fit. VERIFIED — tools-reference.md, tool table. This is a deliberate "fewer, more general tools" choice.
2. `Monitor` is a real feature, not a shell trick: a background command whose every output line becomes a model-visible event, so the agent can react to logs *mid-turn* without polling. VERIFIED — tools-reference.md § Monitor.

**Edit format: exact string replacement, not a diff.** `Edit` takes `old_string` / `new_string` and replaces the first occurrence; no regex, no fuzzy matching. Three gates before an edit applies: VERIFIED — https://code.claude.com/docs/en/tools-reference.md § Edit tool behavior

- **Read-before-edit**: the file must have been read in this conversation (a `PARTIAL view` from a truncated read does *not* count). Newer models may edit an unread file when reading it would not need a permission prompt.
- **Match**: `old_string` must appear byte-exactly. One whitespace character off is a miss.
- **Uniqueness**: `old_string` must appear exactly once, or `replace_all: true`.

There is an explicit allowance list of read-only shell commands (`cat`, `nl`, `bat`, `head`, `tail`, `sed -n`, `grep`, `rg`) that count as a read for this purpose, and doing so also lazily loads that file's subdirectory `CLAUDE.md` and path-scoped rules. VERIFIED — same section.

**Tool output budget.** Bash streams output to a working file; valid results come back inline up to ~30,000 chars, beyond which the model gets a file path plus a 2,000-char preview and can `Read`/search the rest. Failures are capped lower (~10,000 chars head-and-tail). `BASH_MAX_OUTPUT_LENGTH` (default 30,000, ceiling 150,000) controls the read-back window. VERIFIED — tools-reference.md § Bash tool behavior.

**Read budget.** One `Read` returns at most 25,000 tokens by default; whole files over 256 KB are refused and must be paged with `offset`/`limit` (or `allow_large: true` on v2.1.296+, sized against remaining context). Images, PDFs (page ranges via `pdftoppm`) and notebooks are special-cased. VERIFIED — tools-reference.md § Read tool behavior.

## 1.2 Permission / sandbox / approval model

**Three concepts, deliberately separate.** VERIFIED — https://code.claude.com/docs/en/permissions.md and https://code.claude.com/docs/en/sandboxing.md

1. **Permission rules** — `allow` / `ask` / `deny`, evaluated in that order: **deny, then ask, then allow**, first match wins, and *specificity does not reorder them*. A broad `Bash(aws *)` deny cannot be carved out by `Bash(aws s3 ls)`. VERIFIED — permissions.md § Manage permissions.
2. **Permission modes** — `default`/`manual`, `acceptEdits`, `plan`, `auto`, `dontAsk`, `bypassPermissions`. VERIFIED — permissions.md § Permission modes.
3. **OS sandbox** — an actual kernel boundary around *shell commands only* (Seatbelt on macOS, bubblewrap on Linux/WSL2), built on the open-source `@anthropic-ai/sandbox-runtime`. Native Windows runs unsandboxed. VERIFIED — sandboxing.md intro + § What runs outside the sandbox.

**Rule syntax** is `Tool` or `Tool(specifier)`:

- `Bash`, `Bash(*)`, `Bash(npm run build)`, `Read(./.env)`, `WebFetch(domain:example.com)`
- newer: `Tool(param:value)` matching a top-level input parameter
- wildcard rules; `mcp__<server>__<tool>` namespacing with `mcp__memory__.*` matchers

VERIFIED — permissions.md § Permission rule syntax.

**A bare tool name in a `deny` rule removes the tool from the model's context entirely**, so the model never sees it; a scoped rule leaves the tool available and blocks matching calls at call time. That is a genuinely clever context-economy + safety trick. VERIFIED — permissions.md § Manage permissions.

**What "don't ask again" persists, and where** — this granularity is the thing most agents get wrong:

| Tool type | Approval | "Don't ask again" lifetime |
|---|---|---|
| Read-only (Read, Grep) | No, inside working dir | n/a |
| Bash | Yes, except built-in read-only commands | **Permanent, per repository and command** |
| File modification | Yes | **Until session end** |
| WebFetch | Yes, except pre-approved doc domains | Permanent, per repo + domain |
| WebSearch | Yes | Permanent, per repo |

VERIFIED — permissions.md § Permission system. Permanent rules land in `.claude/settings.local.json` at the git root, resolved through worktrees. VERIFIED — same section.

**Comment on a permission answer.** Press `Tab` on Yes/No to attach a free-text note. On Yes the note is delivered *after the tool result*; on No it becomes the denial reason the model reads, and a bare "No" from the main conversation stops the turn. VERIFIED — permissions.md § Add a comment when you answer a permission prompt. This is high-leverage UX: it turns a binary gate into an instruction channel.

**Auto mode = classifier, with a documented decision order.** Rules resolve first; then read-only actions and in-workdir edits auto-approve; then everything else goes to a classifier. Deliberate hardening: on entering auto mode, blanket `Bash(*)`, wildcarded interpreters (`Bash(python*)`), package-manager run commands, `Agent` and `Monitor` allow rules are **dropped**; and the classifier request contains user messages, tool calls and CLAUDE.md, but **tool results are stripped**, so hostile file/web content cannot manipulate the classifier. VERIFIED — https://code.claude.com/docs/en/permission-modes.md § How the classifier evaluates actions.

**Sandbox specifics worth copying:**

- Default writes: working dir + a per-user temp dir + added dirs; protected paths stay write-denied.
- Default reads: **most of the machine**, including `~/.ssh` and `~/.aws/credentials`, unless `filesystem.denyRead` or credential masking is configured. (I.e. the sandbox is write- and network-centric by default, not read-centric.)
- Network: no direct route out; everything goes through a local proxy that checks hosts against an allow-list which starts empty.
- `autoAllowBashIfSandboxed` (default true) means sandboxed commands skip the prompt entirely — the sandbox *substitutes* for the prompt. Content-scoped ask rules like `Bash(git push *)` still force a prompt.
- Built-in **file/web tools, hooks, MCP servers, status line scripts and LSP servers run outside the sandbox**; only shell commands are wrapped.

VERIFIED — https://code.claude.com/docs/en/sandboxing.md § What the sandbox restricts, § Sandbox modes.

**Workspace trust.** Project-supplied settings can choose an `autoMemoryDirectory`, so there is an explicit trust rule: some things do not run until you trust a folder. VERIFIED — permissions.md § What runs before you trust a folder.

## 1.3 Memory and context management

Claude Code runs **two** memory systems side by side and says plainly that both are treated as *context, not enforced configuration* — "to block an action regardless of what Claude decides, use a `PreToolUse` hook instead". VERIFIED — https://code.claude.com/docs/en/memory.md

**(a) CLAUDE.md — you write it, instructions.**

- Load order broadest → narrowest: managed policy (`/etc/claude-code/CLAUDE.md`) → user (`~/.claude/CLAUDE.md`) → project (`./CLAUDE.md`, `./CLAUDE.local.md`) → parent directories from filesystem root down → launched directory.
- All discovered files are **concatenated, not overridden**; within a directory `CLAUDE.local.md` comes last.
- `CLAUDE.md` files in *subdirectories* are not loaded at launch. They load lazily the first time Claude reads/writes/edits a file in that subdirectory.
- `.claude/rules/*.md` splits instructions into per-topic files; YAML `paths:` frontmatter makes a rule load only when matching files are touched.
- **Block-level HTML comments are stripped before injection** (outside code fences) — free maintainer notes that cost zero context tokens.
- `@path` imports pull other files in (a user-level `CLAUDE.md` can itself just import other files).

VERIFIED — memory.md § How CLAUDE.md files load, § Organize rules with `.claude/rules/`; local `~/.claude/CLAUDE.md`.

**(b) Auto memory — Claude writes it, learnings.**

- Four typed notes recorded in frontmatter `type`: `user`, `feedback`, `project`, `reference`.
- Explicitly *skips* anything derivable from the codebase or already in CLAUDE.md.
- Storage: `~/.claude/projects/<project>/memory/` with a `MEMORY.md` **index** plus one topic file per memory. Shared across worktrees of the same repo. Machine-local.
- **Load budget: first 200 lines or 25 KB of `MEMORY.md`, whichever first.** Topic files are *not* loaded at startup; Claude reads them on demand with normal file tools. When `MEMORY.md` approaches the cap, Claude Code reminds the model to shorten it; past the cap the write succeeds but returns an error telling the model to rewrite the index.
- Writes get a `modified` ISO-8601 frontmatter stamp so freshness is visible on read-back.
- Auto memory is **not** loaded into subagents.

VERIFIED — memory.md § Auto memory, § Storage location, § How it works.

**(c) Compaction.** Compaction summarizes history to free context; `/compact <instructions>` lets you steer what is preserved, and CLAUDE.md can carry standing compaction instructions. VERIFIED — https://code.claude.com/docs/en/costs.md § reduce token usage. The auto-compact window defaults to the model's context limit and is configurable per session/model/subagent (`autoCompactWindow`, `CLAUDE_CODE_AUTO_COMPACT_WINDOW`); for a ~1M-token model the default trigger is ~967K. VERIFIED — https://code.claude.com/docs/en/model-config.md § Context window and auto-compaction.

The single best table in either product is **"What survives compaction"** — it makes post-compaction state a designed contract instead of an accident:

| Mechanism | After compaction |
|---|---|
| System prompt + output style | still apply |
| Project-root CLAUDE.md + unscoped rules | **re-injected from disk** |
| Auto memory | **re-injected from disk** |
| Skill *descriptions* | rebuilt and re-injected |
| Git status snapshot | re-read fresh |
| Plan from plan mode | re-injected from disk |
| Path-scoped rules, nested CLAUDE.md | reload on demand |
| Files read/edited | **up to five re-read, most recently modified first**; >5k tokens returns as a path reference |
| Invoked skill bodies | re-injected, capped 5,000 tokens/skill, 25,000 total, oldest dropped |
| Background commands/subagents | keep running; model is reminded which |
| Context added by hooks | summarized with the rest |
| `SessionStart` hooks matching `compact` | run again, output added |

VERIFIED — https://code.claude.com/docs/en/context-window.md § What survives compaction.

**(d) Skills.** `SKILL.md` with frontmatter. Body loads **only when used**, so long reference material is nearly free at rest; only name + description sit in context every turn. Custom slash commands were merged into skills (`.claude/commands/deploy.md` and `.claude/skills/deploy/SKILL.md` both create `/deploy`). Follows the cross-tool `agentskills.io` standard. VERIFIED — https://code.claude.com/docs/en/skills.md intro + first note.

**(e) Subagents.** Own context window, own system prompt, own tool allow-list, own permissions. Built-ins: `Explore` (read-only, skips CLAUDE.md and git snapshot to stay cheap), `Plan`, and a general-purpose agent. Descriptions are what the model routes on, so *descriptions are budgeted*: over 15,000 combined tokens of non-built-in descriptions triggers a startup warning. VERIFIED — https://code.claude.com/docs/en/sub-agents.md intro, § Built-in subagents.

**(f) Checkpointing / rewind.** Every prompt that starts a turn creates a checkpoint; file snapshots for the **100 most recent** checkpoints. `Esc Esc` on an empty prompt (or `/rewind`) opens a menu with: restore code+conversation, restore conversation only, restore code only, **summarize from here**, **summarize up to here**. Restores put the original prompt back in the input box. VERIFIED — https://code.claude.com/docs/en/checkpointing.md

## 1.4 UI / UX design

**Terminal layout** (from the local install + docs): transcript above, prompt box below, a footer of key hints, an optional user-defined status line row above the footer. Permission prompts and dialogs render as overlay panels in the transcript area; `Left`/`Right` switch dialog tabs.

**Shortcuts that define the feel** (VERIFIED — https://code.claude.com/docs/en/interactive-mode.md § Keyboard shortcuts):

| Key | Effect |
|---|---|
| `Ctrl+C` | interrupt; on empty input clears, twice exits |
| `Esc` | interrupt mid-turn, keep work done so far |
| `Esc Esc` | empty input → **rewind menu**; with text → save draft & clear |
| `Ctrl+O` | **toggle transcript viewer** — full tool usage, timestamps, model per message |
| `Ctrl+R` | reverse search history |
| `Ctrl+B` | background running tasks |
| `Ctrl+T` | toggle the task checklist in the status area |
| `Ctrl+G` / `Ctrl+X Ctrl+E` | edit the prompt in `$EDITOR` |
| `Shift+Tab` | cycle permission modes |
| `Option+P` / `Alt+P` | switch model without losing the prompt |
| `Option+T` / `Alt+T` | toggle extended thinking |
| `Ctrl+S` | stash/restore the prompt (restores cursor, pasted content, *and input mode*) |
| `Ctrl+V` | paste image as an `[Image #N]` chip, referenceable positionally |

Details that make it feel considered:

- **Collapsed-by-default tool lines** with expansion, e.g. MCP calls collapse to `Called slack 3 times`. VERIFIED — interactive-mode.md, `Ctrl+O` row.
- **Queue messages while it works**, `Up` on an empty first row takes the queue *back*, `Ctrl+Enter` sends now. VERIFIED — interactive-mode.md § Queue messages while Claude works.
- **Ambient hints**: "prompt suggestions", a "You should know" note system, `/btw` side questions that don't pollute the main thread, `/diff` with a diff panel *and* a dialog view, emoji shortcodes, spell-check-as-you-type, invisible-character warnings. VERIFIED — interactive-mode.md headings.
- **Status line is a user script, not a fixed widget**: JSON on stdin (model display name, `context_window.used_percentage`, cost, git info), whatever it prints is the bar; `/statusline` generates the script from a natural-language request; multi-line supported. VERIFIED — https://code.claude.com/docs/en/statusline.md
- **Output styles** (Default / Proactive / Concise / Explanatory / Learning) set role, tone and verbosity for a whole session; `Learning` deliberately leaves small pieces of code for the human. VERIFIED — https://code.claude.com/docs/en/output-styles.md
- **`/doctor`** — a setup checkup that inspects install health, unused skills/MCP/plugins *against their context cost*, slow hooks, duplicated CLAUDE.md, permissions you keep denying, and offers fixes. `claude doctor` for non-interactive. VERIFIED — skills.md § Check your setup with `/doctor`.
- **IDE/desktop**: same hooks fire in terminal, IDE extensions, desktop app and cloud sessions — one extension surface, four hosts. VERIFIED — https://code.claude.com/docs/en/hooks.md intro.

## 1.5 Config & extensibility

- **Settings layers**: managed policy, user (`~/.claude/settings.json`), project (`.claude/settings.json`), local (`.claude/settings.local.json`), `--settings`, plugin. MERGE across levels for hooks ("user, project and local settings add their own hooks without removing managed ones"). VERIFIED — hooks.md § Hook locations; settings.md.
- **Hooks** are the extensibility backbone. Events: `SessionStart`, `Setup`, `UserPromptSubmit`, `UserPromptExpansion`, `PreToolUse`, `PermissionRequest`, `PermissionDenied`, `PostToolUse`, `PostToolUseFailure`, `PostToolBatch`, `Notification`, `MessageDisplay`, `SubagentStart/Stop`, `TaskCreated/Completed`, `Stop`, `StopFailure`, `TeammateIdle`, `InstructionsLoaded`, `ConfigChange`, `CwdChanged`, `DirectoryAdded`, `FileChanged`, `WorktreeCreate/Remove`, `PreCompact`/`PostCompact`, `PreModelSwitch`/`PostModelSwitch`, `Elicitation`/`ElicitationResult`, `SessionEnd`.
  - Five handler types: `command`, `http`, `mcp_tool`, `prompt` (single-turn LLM), `agent` (subagent that may use tools to decide).
  - Communicates via **stdin JSON + exit codes + stdout JSON**; `exit 0` with no output means "no decision", not "approve".
  - `if` field uses *permission-rule syntax* to pre-filter, so the process is not even spawned when it cannot match (`"Bash(rm *)"`, `"Edit(*.ts)"`). Explicitly documented as best-effort, with "use the permission system rather than a hook to enforce a hard deny".
  - `async`, `asyncRewake` (wake the model on exit code 2 with the stderr as a system reminder), `statusMessage` (custom spinner text), `once`, `onFailure: block|continue`, exec-form vs shell-form.
  - Handlers in the same event run **in parallel**.
  VERIFIED — https://code.claude.com/docs/en/hooks.md (all of the above).
- **Plugins** bundle skills + agents + hooks + MCP servers + LSP + monitors behind `.claude-plugin/plugin.json`, distributed via marketplaces, installed with `/plugin`. `/plugin` shows a **context cost estimate** before install and a "Not used recently" group after. VERIFIED — https://code.claude.com/docs/en/plugins.md
- **MCP**: namespaced `mcp__server__tool`; **tool search defers tool definitions** so only names + server instructions load at session start, which is why adding servers has "minimal impact" on context. Description lengths are truncated at 4,096 chars for upfront tools and 16,384 for searched tools — with the advice "put the words Claude would search for near the start". VERIFIED — https://code.claude.com/docs/en/mcp.md § Scale with MCP tool search.
- **Keybindings** are user-remappable via `~/.claude/keybindings.json` with `context` scoping. Local example: `{"context":"Chat","bindings":{"ctrl+alt+g":"command:orca-agent-view"}}`. VERIFIED — `~/.claude/keybindings.json`.

## 1.6 Notable details that make it feel good

1. **Every denial is an opportunity to teach.** Comment-on-deny reaches the model as the reason, and it continues working instead of stopping. VERIFIED — permissions.md.
2. **Tool calls collapse.** Default view is a one-line summary; details are one `Ctrl+O` away. VERIFIED — interactive-mode.md.
3. **Rewind is a first-class verb**, and it can rewind *only the conversation* or *only the code* — plus summarize half the thread. VERIFIED — checkpointing.md.
4. **`/doctor` treats context as a budget you can audit**, including "unused skills compared with their context cost". VERIFIED — skills.md.
5. **Descriptions are budgeted and warned about** (15k-token subagent description ceiling). VERIFIED — sub-agents.md.
6. **HTML comments are stripped** from CLAUDE.md before injection — a free channel for human notes. VERIFIED — memory.md.
7. **Post-compaction is a spec, not a hope** (the survives-compaction table). VERIFIED — context-window.md.
8. **Sandbox replaces the prompt, not just the risk.** `autoAllowBashIfSandboxed` is the reason auto mode feels fast rather than naggy. VERIFIED — sandboxing.md.
9. **Progress is narrated in the status area** — task checklist on `Ctrl+T`, running background shells/subagents via `/tasks`. VERIFIED — interactive-mode.md.
10. **The startup warning set is curated** (description overflow, unparseable settings, duplicate installs, rules that match no tool). VERIFIED — permissions.md § deny/ask startup warning.

---

# PART 2 — Codex

Codex's advantage is that we can read the implementation. All repo paths below are relative to `/tmp/kova-research/src-codex` (shallow clone of `openai/codex`).

## 2.1 Agent loop and tool design

**Loop shape.** A `Session` drives turns; each turn is a step loop of model request → streamed response items → tool dispatch → repeat, with a first-class `TurnContext` and a `WorldState` snapshot per step.

- `codex-rs/core/src/session/turn.rs`, `session/step_context.rs` — VERIFIED (file layout; I read `compact.rs`, `tools/registry.rs`, `tools/handlers/*`).
- Tool registry is keyed by `ToolName` and holds a spec + a runtime executor; dispatch goes through `tools/router.rs` and `tools/parallel.rs` (a batch of tool calls can resolve in parallel). VERIFIED — `codex-rs/core/src/tools/registry.rs`, `parallel.rs`.
- Tool *lifecycle events* (`notify_tool_start` / `notify_tool_finish`) and a per-tool `ToolDispatchTrace` / `direct-tool-timing` crate exist purely so the UI can draw timing. VERIFIED — `core/src/tools/lifecycle.rs`, `core/src/direct_tool_timing/`.

**Tool set** (from `core/src/tools/handlers/`):

`exec_command` + `write_stdin` (unified exec, PTY, session IDs), `apply_patch`, `update_plan`, `view_image`, `tool_search`, `plan`, `request_user_input` / `request_user_input_async`, `request_permissions`, `send_message_to_user`, `mcp` / `mcp_resource`, `multi_agents` (v1 + v2), `new_context_window`, `get_context_remaining`, `current_time`, `sleep`, `wait_for_environment`, `test_sync`, `list_available_plugins_to_install`, `request_plugin_install`.

VERIFIED — directory listing of `codex-rs/core/src/tools/handlers/`.

Three design choices stand out:

**(a) `exec_command` returns a *session*, not just output.** The schema is explicitly a PTY runner: `cmd`, `workdir`, `tty`, `yield_time_ms` (default 10,000; range 250–30,000), `max_output_tokens` (default 10,000), `shell`, `login`, `environment_id`, plus approval params. It "returns output **or a session ID for ongoing interaction**", and `write_stdin` feeds that session. That is how Codex supports REPLs, dev servers, and interactive prompts from a single tool instead of `background_bash` + `bash_output` + `kill_shell` triples. VERIFIED — `codex-rs/core/src/tools/handlers/shell_spec.rs:41-110`.

**(b) `apply_patch` is a FREEFORM tool with a grammar, not JSON.** `ToolSpec::Freeform` with `format: {type: "grammar", syntax: "lark"}`, description literally: *"The `apply_patch` tool can be used to edit files. This is a FREEFORM tool, so do not wrap the patch in JSON."* VERIFIED — `codex-rs/core/src/tools/handlers/apply_patch_spec.rs:8-27`.

Grammar (`codex-rs/core/assets/tools/apply_patch.lark`):

```
start: begin_patch hunk+ end_patch
hunk: add_hunk | delete_hunk | update_hunk
add_hunk:    "*** Add File: " filename LF add_line+
delete_hunk: "*** Delete File: " filename LF
update_hunk: "*** Update File: " filename LF change_move? change?
change_move: "*** Move to: " filename LF
change: (change_context | change_line)+ eof_line?
change_context: ("@@" | "@@ " /(.+)/) LF
change_line: ("+" | "-" | " ") /(.*)/ LF
eof_line: "*** End of File" LF
```

Notable: `*** Move to:` renames in the same hunk; `*** End of File` anchors a hunk to EOF; the parser is deliberately **lenient** (comment in `parser.rs`: "the parser below is a little more lenient than the explicit spec and allows for leading/trailing whitespace around patch markers"). VERIFIED — `codex-rs/apply-patch/src/parser.rs:1-45`.

**(c) Patch application is tolerant by design.** `seek_sequence` matches a hunk's context lines at *three* strictnesses: exact, then ignore trailing whitespace, then ignore both sides; with an `eof` mode that searches from the end first. It has explicit defensive cases (empty pattern → no-op; pattern longer than file → `None`, with a comment naming a pre-2025-04-12 panic that used to occur). VERIFIED — `codex-rs/apply-patch/src/seek_sequence.rs:1-60`.

**Streaming.** `codex-rs/core/src/client.rs` streams `ResponseEvent`s; `stream_events_utils.rs` maps them to TUI/protocol events. A **streaming patch parser** exists (`apply-patch/src/streaming_parser.rs`) so a partially-received patch can be parsed and rendered while it is still arriving. VERIFIED — file exists; behaviour inferred from the name and `parser.rs` use of `StreamingPatchParser`. UNSURE on the exact UI update cadence.

**Deferred tools.** `tool_search` takes `query` + `limit` and returns tools "for deferred tools"; specs carry a `defer_loading` flag. Same idea as Claude Code's MCP tool search, applied to a broader set. VERIFIED — `codex-rs/core/src/tools/handlers/tool_search_spec.rs:1-25`, `defer_loading` field usage in `tools/handlers/*.rs`.

**Context introspection as a tool.** `get_context_remaining` and `new_context_window` are model-callable. So the model can ask "how much room do I have?" and can start a fresh context window deliberately instead of waiting to be compacted. VERIFIED — handler files exist; semantics from names + `compact.rs` use of `start_new_context_window`.

## 2.2 Permission / sandbox / approval model

**Two orthogonal layers, stated crisply in the docs:** sandbox mode = *what is technically possible*; approval policy = *when it must ask*. VERIFIED — https://learn.chatgpt.com/docs/agent-approvals-security

**Sandbox modes** (the exact strings injected into the model's instructions):

| Mode | Injected text |
|---|---|
| `read-only` | "The sandbox only permits reading files." |
| `workspace-write` | "permits reading files, and editing files in `cwd` and `writable_roots`. Editing files in other directories requires approval." |
| `danger-full-access` | "No filesystem sandboxing - all commands are permitted." |

Each appends `Network access is {{ network_access }}`. VERIFIED — `codex-rs/prompts/templates/permissions/sandbox_mode/*.md`.

Defaults: **no network access, writes limited to the active workspace**. VERIFIED — agent-approvals-security doc.

**Approval policies**: `never`, `on-request`, `unless-trusted`, plus the retired `untrusted` (removed; keeping it can prevent startup). Each has a model-facing template — e.g. `never.md` is *"Approval policy is currently never. Do not provide the `sandbox_permissions` for any reason, commands will be rejected."* VERIFIED — `codex-rs/prompts/templates/permissions/approval_policy/*.md` and the security doc § Migrate from the retired `untrusted` approval policy.

**Escalation is a tool parameter, not a modal.** `exec_command` accepts `sandbox_permissions`:
- `"with_additional_permissions"` + `additional_permissions` = `{network.enabled, file_system.read[], file_system.write[]}` — stay inside the policy, add only what this command needs. **Preferred mode.**
- `"require_escalated"` + `justification` (a question to the user) + optional `prefix_rule` (a suggested reusable allow rule).

The model is instructed: if a command fails for a likely sandbox reason, **rerun it with escalation and a justification, do not message the user first**. And banned prefixes are spelled out: don't request `["python3"]` or `["python","-"]`, never a `prefix_rule` for `rm`, never one with a heredoc. VERIFIED — `prompts/templates/permissions/approval_policy/on_request.md` and `on_request_rule_request_permission.md`.

**Command segmentation.** Commands are split at `|`, `&&`, `||`, `;`, `(...)`, `$(...)`, and each segment is evaluated independently. Commands using redirection, substitution, `VAR=`, or wildcards are **not** matched against rules at all — the whole invocation is treated as one opaque call. VERIFIED — same templates, § Command segmentation / § When Codex does not split the script.

**Exec policy rules are Starlark with inline unit tests.** `~/.codex/rules/*.rules`:

```python
prefix_rule(
    pattern = ["gh", "pr", "view"],
    decision = "prompt",          # allow | prompt | forbidden
    justification = "Viewing PRs is allowed with approval",
    match     = ["gh pr view 7888", "gh pr view --json title,body"],
    not_match = ["gh pr --repo openai/codex view 7888"],
)
```

Most restrictive decision wins (`forbidden` > `prompt` > `allow`). `match` / `not_match` are validated at load time, so a bad rule fails loudly instead of silently over-permitting. Rules live next to every active config layer; project-local `<repo>/.codex/rules/` only loads when that layer is trusted. `codex execpolicy check --pretty --rules … -- cmd…` prints the resolved decision and the matching justification. VERIFIED — https://learn.chatgpt.com/docs/agent-configuration/rules

**OS enforcement**: `codex-rs/sandboxing/` — `landlock.rs` + `bwrap.rs` (+ `seccomp.rs`, `linux_pid_namespace.rs`) on Linux, `seatbelt.rs` with `.sbpl` policy files on macOS, `windows.rs` / `windows_mxc.rs` on Windows. There is a separate `linux-sandbox` binary crate and a `sandbox_integrity` module that reports integrity findings. VERIFIED — directory listings.

## 2.3 Memory and context management

**(a) AGENTS.md discovery — a real spec, with a byte budget.**

- Global: `~/.codex/AGENTS.override.md` if it exists, else `~/.codex/AGENTS.md`; **only the first non-empty file** at this level.
- Project: walk from project root (git root by default, configurable `project_root_markers`) down to cwd; in each directory check `AGENTS.override.md` → `AGENTS.md` → `project_doc_fallback_filenames`. **At most one file per directory.**
- Concatenate root → cwd, blank-line separated; later files win by position.
- Stop when the combined size hits **`project_doc_max_bytes` (32 KiB default)**; emit a warning naming the remaining budget and telling you to trim or raise it.
- **Never walk past the project root.**

VERIFIED — https://learn.chatgpt.com/docs/agent-configuration/agents-md and `codex-rs/core/src/agents_md.rs:1-95` (module doc + `PROJECT_DOC_BUDGET_GUIDANCE` + `LOCAL_AGENTS_MD_FILENAME = "AGENTS.override.md"` + `AGENTS_MD_SEPARATOR = "\n\n--- project-doc ---\n\n"`).

Untrusted projects load **zero** project instructions (`if config.active_project.is_untrusted() || config.project_doc_max_bytes == 0 { return Ok(loaded) }`). VERIFIED — `agents_md.rs:69-74`.

**(b) Memories — a two-phase background pipeline, not "save a fact".**

Documented in `codex-rs/memories/README.md`:

- Triggered on **root** session start only; never ephemeral, never a subagent, requires the state DB. Runs **asynchronously** so it never blocks the turn.
- **Phase 1 (per-thread extraction)**: claim a bounded set of eligible *rollouts* from the state DB (must be from allowed interactive sources, within an age window, idle long enough, not already claimed), filter to memory-relevant items, send each to a model in parallel with a concurrency cap, and get back structured output: a detailed `raw_memory` **and** a compact `rollout_summary`.
- **Phase 2 (consolidation)**: merge those into a file-based memory folder designed for *progressive disclosure*: `memory_summary.md` (first line must be exactly `v1`; always loaded into the system prompt; "dense, highly navigational"), `MEMORY.md` (handbook entries to grep), `skills/<name>/SKILL.md` (reusable procedures), `rollout_summaries/<slug>.md`.
- Safety rules written into the consolidation prompt: raw rollouts are **immutable evidence**, rollout text and tool outputs are **data not instructions**, evidence-based only, `[REDACTED_SECRET]` for secrets, prefer compact summaries + exact error snippets + pointers over pasting tool output, and **a no-op is preferred over filler** (incremental mode may make zero file changes).

VERIFIED — `codex-rs/memories/README.md`, `memories/write/templates/memories/consolidation.md:1-55`.

There is also a read path with **Citation** parsing — the model cites which memory it used, and usage is telemetry-classified. VERIFIED — `codex-rs/memories/read/src/citations.rs`, `usage.rs`.

Local state confirms this is live: `~/.codex/memories/`, `memories_1.sqlite`, `memories_v2_1.sqlite`, and `[features] memories = true` / `[memories] generate_memories = true, use_memories = true` in `~/.codex/config.toml`. VERIFIED.

**(c) Compaction — three distinct mechanisms, all modelled as the same lifecycle.**

- **Local model compaction**: summarize with `templates/compact/prompt.md`, then re-open with `templates/compact/summary_prefix.md`.
- **Remote v2 compaction** (`compact_remote_v2*`) and a **token-budget compaction** (`compact_token_budget.rs`) that "skips model/server summarization and installs a fresh context window instead".
- All of them still fire `PreCompact` / `PostCompact` hooks and emit a `ContextCompaction` turn item, so the UI and hooks see one event regardless of implementation.

VERIFIED — `codex-rs/core/src/compact_token_budget.rs:1-40`, `core/src/compact.rs`.

The compaction prompt is short and role-based — worth copying almost verbatim:

> "You are performing a CONTEXT CHECKPOINT COMPACTION. Create a handoff summary for another LLM that will resume the task. Include: current progress and key decisions made; important context, constraints, or user preferences; what remains to be done (clear next steps); any critical data, examples, or references needed to continue. Be concise, structured, and focused on helping the next LLM seamlessly continue the work."

followed by `summary_prefix.md`: *"Another language model started to solve this problem and produced a summary of its thinking process. You also have access to the state of the tools that were used by that language model. Use this to build on the work that has already been done and avoid duplicating work."*

VERIFIED — `codex-rs/prompts/templates/compact/prompt.md`, `summary_prefix.md`.

Retention is explicit: `compact_retained_tool_outputs.rs`, `COMPACT_USER_MESSAGE_MAX_TOKENS = 20_000`, and a `AutoCompactionDisabled` thread-local policy to opt a thread out. VERIFIED — `core/src/compact.rs:1-80`.

**(d) Skills.** `~/.codex/skills/`, per-project skills, and the `SKILL.md` convention; `/skills` manages them. Local install has an empty `~/.codex/skills/` but bundled plugin skills under `~/.codex/plugins/cache/*/skills/*/SKILL.md`. VERIFIED — `~/.codex/skills`, `codex-rs/skills/` crate, `/skills` description in `tui/src/slash_command.rs`.

**(e) Subagents.** `[features] multi_agent = true` locally; `multi_agents` + `multi_agents_v2` tool handlers; `/subagents`, `/agents` commands; a whole `core/src/agent/` module with `role.rs`, `registry.rs`, `child_config.rs`, `status.rs`, and an `agent-message-board` crate for cross-agent messaging. Sandbox/permission profiles are per-agent (`child_config`). VERIFIED — paths + `~/.codex/config.toml`.

## 2.4 UI / UX design

**Rendering stack**: `ratatui`-style cell model with a `history_cell` module per content type — `messages.rs`, `exec.rs`, `patches.rs` (diffs), `plans.rs`, `approvals.rs`, `mcp.rs`, `search.rs`, `notices.rs`, `hook_cell.rs`, `activity_group.rs`, `session.rs`, plus `markdown_render_cache.rs` and `separators.rs`. Snapshots are committed next to them (`history_cell/snapshots/`), i.e. **the TUI is snapshot-tested**. VERIFIED — `codex-rs/tui/src/history_cell/`.

**Status indicator**: an animated header with elapsed time and an interrupt hint, exactly `Working (0s • esc to interrupt)`, formatted as `59s` / `1m 01s` / `1h 02m 03s` (`fmt_elapsed_compact`). The label stays visually stable while the underlying animation changes ("so that core … the more important elapsed/interrupt hint"). VERIFIED — `codex-rs/tui/src/status_indicator_widget.rs:57-268`, test at line 458.

**Input**: a `chat_composer` with slash-command popup, `@` file mentions (`mention_syntax.rs`), paste handling (`clipboard_paste/`), sparkle/completion tests, and a `bottom_pane` that hosts approvals, async questions, and user-input requests.
VERIFIED — directory names under `tui/src/`.

**Slash commands** are an ordered enum where **enum order is popup order** ("DO NOT ALPHA-SORT! … more frequently used commands should be listed first"), with per-command descriptions and aliases. The full local set (67 entries) includes: `/model`, `/permissions`, `/memories`, `/skills`, `/hooks`, `/review`, `/plan`, `/compact`, `/recap`, `/status`, `/statusline`, `/theme`, `/diff`, `/mention`, `/new`, `/resume`, `/fork`, `/archive`, `/worktree`, `/init`, `/agents`, `/subagents`, `/ps`, `/stop`, `/mcp`, `/plugins`, `/apps`, `/ide`, `/vim`, `/keymap`, `/import` ("import setup, this project, and recent chats from Claude Code"), `/app` ("continue this session in the Desktop app"), `/feedback`, `/raw`, `/usage`, `/quit`.

VERIFIED — `codex-rs/tui/src/slash_command.rs:1-140`.

**Desktop app** is the other half of the UX, and its settings are unusually opinionated. From `~/.codex/config.toml [desktop]`:
`conversationDetailMode = "STEPS_PROSE"`, `show-context-window-usage = true`, `preventSleepWhileRunning = true`, `followUpQueueMode = "steer"`, `enabled-reasoning-efforts = [low, medium, high, xhigh, ultra, persistent, max]`, custom chrome theme with explicit `diffAdded` / `diffRemoved` / `skill` semantic colors, `toys-confetti-enabled = true`, `avatar-overlay-pet-visible = true`, plus a `/pets` slash command and a `pets/` directory. VERIFIED — `~/.codex/config.toml`.

**Keyboard model (desktop)**: `⌘K`/`⌘⇧P` command menu, `⌘J` bottom panel, `` ⌃` `` terminal, `⌘⌥A` *next chat needing attention*, `⌘⌥1–6` recent chats, `⏎` approve / `Esc` decline an approval, `⌃⇧G` review tab, `⌘⇧.` toggle browser comment mode, `⌘⌘` Appshot. VERIFIED — https://learn.chatgpt.com/docs/reference/commands

## 2.5 Config & extensibility

- **One TOML file with layered config and a debug view.** `~/.codex/config.toml` holds `model`, `model_reasoning_effort`, `personality`, `[desktop]`, `[features]`, `[memories]`, `[projects."<path>"] trust_level = "trusted"`, `[mcp_servers.*]`, `[model_providers.*]`, `[marketplaces.*]`, `[plugins."name@marketplace"] enabled`, and `[hooks.state."<file>:<event>:<i>:0"] trusted_hash + enabled`. `/debug-config` "shows config layers and requirement sources", `codex execpolicy check` resolves rules. VERIFIED — local config + slash_command.rs descriptions.
- **Hooks** are the same shape as Claude Code's (SessionStart, UserPromptSubmit, PreToolUse, PermissionRequest, PostToolUse, SubagentStart/Stop, Stop, PreCompact/PostCompact, SessionEnd) with **per-hook trust hashes**: each hook records `trusted_hash = "sha256:…"` and an `enabled` flag, so editing a hook silently disables it until re-trusted. That is a genuinely good security property that Claude Code handles differently (workspace trust at the folder level). VERIFIED — `~/.codex/config.toml [hooks.state]`, `~/.codex/hooks.json`.
- **Plugins + marketplaces, with local and remote sources.** `[marketplaces.openai-bundled] source_type = "local"`, per-plugin `enabled` flags, versioned cache dirs (`plugins/cache/openai-bundled/browser/26.1103.60933`). Plugins can ship skills, MCP servers, apps, and even a node REPL bridge (`[mcp_servers.node_repl]` with `NODE_REPL_TRUSTED_SERVICES`, `NODE_REPL_TRUSTED_CODE_PATHS`). VERIFIED — `~/.codex/config.toml`, `~/.codex/plugins/cache/`.
- **MCP** with per-server `command`, `args`, `env`, `env_vars` (an allowlist of which parent env vars pass through), and `startup_timeout_sec`. VERIFIED — `~/.codex/config.toml`.
- **`codex app-server`** exposes the same engine over a protocol (`codex-rs/app-server`, `app-server-protocol`, `app-server-daemon`, `app-server-client`), which is how the desktop app, IDE integrations and remote control all reuse one core. VERIFIED — crate names; https://learn.chatgpt.com/docs/app-server (listed in llms.txt).
- **Import from a competitor as a first-class command**: `/import` = "import setup, this project, and recent chats from Claude Code". Local artifacts: `~/.codex/claude-cowork-import-history.json`, `external_agent_session_imports.json`. VERIFIED — slash_command.rs + local files.

## 2.6 Notable details that make it feel good

1. **`Working (12s • esc to interrupt)`** — one line that answers "is it alive, how long, how do I stop it". VERIFIED — status_indicator_widget.rs.
2. **`/recap` vs `/compact`** are separate: recap summarizes *now*, compact summarizes *to survive*. VERIFIED — slash_command.rs descriptions.
3. **`exec_command` sessions** make REPLs and dev servers first-class instead of a special "background" mode. VERIFIED — shell_spec.rs.
4. **Rules carry their own tests** (`match` / `not_match`) and a human-readable `justification` that is surfaced in the approval prompt and the rejection message. VERIFIED — rules doc.
5. **`forbidden` rules can include the recommended alternative** — e.g. justify "Use `rg` instead of `grep`". VERIFIED — rules doc, `justification` field.
6. **Per-hook trust hashes** mean a tampered hook stays off. VERIFIED — config.toml.
7. **"Use `apply_patch`; do not wrap the patch in JSON"** — the tool description fights the model's JSON habit explicitly. VERIFIED — apply_patch_spec.rs.
8. **Compaction prompt tells the next model that it is a continuation** and that tool state is still live, which reduces duplicate work. VERIFIED — summary_prefix.md.
9. **Memory consolidation prompt forbids fabrication and prefers a no-op** over filler; it also names rollout text as data, not instructions. VERIFIED — consolidation.md.
10. **`/ps` and `/stop`** for background terminals, and `/raw` for "copy-friendly terminal selection" — the TUI notices when selection is what you actually need. VERIFIED — slash_command.rs.

---

# PART 3 — Side-by-side

| Dimension | Claude Code | Codex |
|---|---|---|
| Edit primitive | exact-string `old_string`/`new_string`, first occurrence, uniqueness required, read-before-edit gate | FREEFORM `apply_patch` with a Lark grammar, multi-file, rename + EOF anchors, 3-level fuzzy context match |
| Shell primitive | `Bash` (+ `PowerShell`, `Monitor`); no session object, backgrounding is a UI concept | `exec_command` returns a session ID; `write_stdin` continues it; PTY + `yield_time_ms` built in |
| Safety layer 1 | allow/ask/deny rules, deny-first precedence, specificity irrelevant | Starlark `prefix_rule` with `allow`/`prompt`/`forbidden` and inline tests |
| Safety layer 2 | OS sandbox around shell only (Seatbelt/bwrap), `autoAllowBashIfSandboxed` | OS sandbox (landlock+bwrap/seccomp, seatbelt) around execution; mode injected into prompt |
| Escalation UX | permission prompt with `Tab`-to-comment, "don't ask again" with per-tool lifetimes | `sandbox_permissions` + `justification` + suggested `prefix_rule` inside the tool call |
| Instructions file | `CLAUDE.md` + `CLAUDE.local.md` + `.claude/rules/` with `paths:` scoping; HTML comments stripped; `@imports` | `AGENTS.md` / `AGENTS.override.md`, one file per directory, 32 KiB budget, never past project root |
| Learned memory | `MEMORY.md` index + topic files, 4 typed kinds, 200 lines/25 KB load cap | Two-phase rollout extraction + consolidation into `memory_summary.md` / `MEMORY.md` / skills / rollout summaries, with citations |
| Compaction | `/compact [instructions]`, configurable window, explicit "what survives" contract | three implementations (local, remote-v2, token-budget) behind one `PreCompact`/`PostCompact` lifecycle |
| Extensibility | hooks (5 handler types) + skills + subagents + plugins + MCP tool search | hooks (per-hook trust hash) + skills + multi-agent + app-server + plugins/marketplaces |
| TUI signature | `Ctrl+O` transcript, `Esc Esc` rewind, status line script, collapsed tool lines | `Working (12s • esc to interrupt)`, diff cells, `/ps`, `/raw`, snapshot-tested rendering |
| Context economy trick | tool search defers MCP definitions; bare-tool deny removes the tool from context | `defer_loading` + `tool_search`; `get_context_remaining` as a callable |

---

# PART 4 — What Kova should adopt, ranked

Current Kova, for calibration: `src/kova/agent.py` is 305 lines with a `for step in range(11)` loop, `trim_history()` that simply drops the oldest messages when over `context_tokens * 4 * 0.75`, `MAX_TOOL_RESULT_CHARS = 32_000`, ten-plus tool rounds, and inline approval handling. `src/kova/permissions.py` is a 120-line regex heuristic (`_DESTRUCTIVE_PATTERNS`) plus a boolean `session_allowed`. `src/kova/project_instructions.py` reads `KOVA.md` and `AGENTS.md` from the cwd only, caps at 4,096 chars. `src/kova/memory.py` is a Markdown-note store with YAML frontmatter and FTS search; `search(query, limit=5)` is called per turn.

Ranked by (value to Kova) ÷ (effort), with the file each item touches.

### 1. Replace truncation with real compaction, and make post-compaction state a contract

**What.** When history exceeds budget, summarize instead of dropping. Use Codex's two-part prompt (checkpoint/handoff instructions + "another model started this, tool state is still live, build on it"). Then re-inject from disk rather than trusting the summary: system prompt, `KOVA.md`/`AGENTS.md`, memory index, current plan, and the ~5 most recently touched files. Keep a `/compact [focus]` escape hatch and a `PreCompact`/`PostCompact` event pair.

**Why.** `trim_history()` currently amputates the oldest turns, which silently deletes the user's original constraints — the worst failure mode in a long session. Claude Code makes the survives-compaction set an explicit table and it is the difference between "long sessions feel reliable" and "long sessions feel like a new assistant".

**Touches.** `src/kova/agent.py` (`trim_history` → `compact_history`, prompt assembly), `src/kova/store.py` (store compaction checkpoints + summary rows), new `src/kova/compact.py`, `src/kova/config.py` (window/budget keys), `src/kova/events.py` (new event types).

### 2. Upgrade approvals from a boolean to deny > ask > allow rules with sane lifetimes

**What.** Three rule lists with deny-first precedence and no specificity reordering; rules persisted per project (Bash command, web domain) and per session (file edits); a `Tool(specifier)`-ish syntax; and a free-text note attached to an approval answer that reaches the model as guidance on deny.

**Why.** `permissions.py` is a regex blocklist with one `session_allowed` bool, so "allow for this chat" and "always allow this exact command" are the same thing, and a user's "no, because X" is thrown away. Precedence-by-category (not by specificity) is what makes rules auditable; the denial note turns every rejection into an instruction.

**Touches.** `src/kova/permissions.py` (rewrite around rule objects), `src/kova/store.py` (persist project rules), `src/kova/config.py` (defaults), `src/kova/tui/app.py` + `src/kova/tui/widgets.py` (approval overlay with note field), `src/kova/web_assets/*` + `desktop/` (same overlay in the GUI).

### 3. Real sandboxing for `run_command`, and let the sandbox replace the prompt

**What.** Wrap shell execution in bubblewrap on Linux / Seatbelt on macOS with read-only-root + writable-cwd + no-network defaults; expose `read-only` / `workspace-write` / `full` modes; make "sandboxed ⇒ no prompt" the default so safety *increases* speed. Add an escalation path on the tool itself (`require_escalated` + a justification string + an optional suggested prefix rule) instead of a separate modal.

**Why.** The current model is "regex guess, then ask the user", which is both naggy and unsafe — a regex cannot see `python -c "os.remove(...)"`. An OS boundary is the only thing that survives prompt injection, and Codex's `autoAllowBashIfSandboxed` pattern is what makes that boundary *pleasant*.

**Touches.** new `src/kova/sandbox.py`, `src/kova/tools/core.py` (run_command), `src/kova/permissions.py`, `src/kova/config.py`, `src/kova/diagnostics.py` (detect missing `bwrap`/`socat` like `/sandbox`'s Dependencies tab).

### 4. Layered, budgeted project instructions

**What.** Walk from the project root (or `.git`) down to cwd; per directory prefer `KOVA.override.md` → `KOVA.md` → `AGENTS.md`; concatenate root→cwd; enforce a byte budget (32 KiB) with a warning that names the remaining budget; load nested files lazily when the agent first touches a file in that subdirectory; skip entirely for untrusted folders. Support `@file` imports.

**Why.** `project_instructions()` today reads two names in one directory and truncates at 4,096 chars with no notice. That means monorepos silently lose guidance and subpackages cannot have their own rules — the exact thing both products solved.

**Touches.** `src/kova/project_instructions.py` (rewrite), `src/kova/config.py` (`project_doc_max_bytes`, root markers), `src/kova/agent.py` (lazy reload hook), `src/kova/permissions.py` (trust gate).

### 5. Turn memory into an index + topic files with a load budget

**What.** Keep the existing Markdown note store, but add a `MEMORY.md` index that is the *only* thing loaded every turn (first 200 lines / 25 KB), one topic file per memory with a `type` (`user` / `feedback` / `project` / `reference`) and a `modified` timestamp, and let the model read topic files on demand with the file tools rather than relying on FTS top-5. Add a citation field so the transcript shows which memory was used. Add a "consolidate" pass that merges stale notes and prefers a **no-op** over filler.

**Why.** `memory.search(text, 5)` per turn is a retrieval guess with no notion of freshness, and everything it returns is paid for on every message. The index/on-demand split is the cheapest large win in either product, and Codex's `[REDACTED_SECRET]` + "rollout text is data, not instructions" rules matter for a local-first assistant that ingests its own transcripts.

**Touches.** `src/kova/memory.py` (index writer, typed notes, budget), `src/kova/tools/memory.py` (save/search/read tool surface), `src/kova/agent.py` (inject index, not search results), new `src/kova/memory_consolidate.py`, `src/kova/store.py` (citation/usage rows).

### 6. Skills: progressive disclosure instead of prompt bloat

**What.** `~/.kova/skills/<name>/SKILL.md` plus project `.kova/skills/`, frontmatter `name` / `description` / optional `allowed-tools`; only name + description in context; body loaded on invoke; supporting `scripts/`, `references/`, `examples/` next to it; `/skills` to list and `/name` to invoke; a per-skill token cap and a total cap for re-injection after compaction.

**Why.** Kova's `modes.py` + `_IDENTITIES` already prove the pattern (swap the system prompt), but every future capability currently wants to be an identity string or a new tool. Skills give users a way to add procedure without touching Python, and they cost ~one line of context at rest.

**Touches.** new `src/kova/skills.py`, `src/kova/tools/__init__.py` (a `Skill` tool), `src/kova/agent.py` (description injection + body injection on invoke), `src/kova/cli.py` (`/skills`), `src/kova/tui/prompts.py` (slash completion).

### 7. Make edits safer and more capable

**What.** Keep exact-string `edit_file`, but add: (a) read-before-edit tracking per chat, (b) a uniqueness requirement with a `replace_all` escape hatch, (c) whitespace-tolerant fallback matching (exact → rstrip → trim both sides) when the exact text misses, and (d) multi-hunk/rename support so a refactor is one call. Report *which* fallback was used so the model learns the file's real shape.

**Why.** `code_files.edit_file(old_text, new_text)` with `_EditConflict` is close, but without read-tracking and tolerance the agent burns turns on near-misses — precisely the failure `seek_sequence` was written to absorb in Codex.

**Touches.** `src/kova/tools/code_files.py` (`edit_file`, new `apply_patch`-style tool, `FileSession` read tracking), `src/kova/agent.py` (`FILE_TOOLS` prepare/check path), `src/kova/tools/core.py` (schema).

### 8. Subagents with their own context and tool allow-list

**What.** A `delegate` tool that spawns a child agent with its own message list, its own system prompt, a restricted tool set (read-only explore variant by default), and a hard output budget back to the parent. Built-ins worth having first: `explore` (read-only search) and `review` (diff review). Budget the *descriptions* and warn above a threshold.

**Why.** Kova's single loop means every search result and file dump stays in the main history forever — the exact problem compaction is patching around. A read-only explorer subagent is the highest-value, lowest-risk version, and it composes with item 1.

**Touches.** new `src/kova/subagents.py`, `src/kova/agent.py` (tool dispatch + nested event stream), `src/kova/tools/core.py` (schema), `src/kova/events.py` (parent/child tagging), `src/kova/tui/render.py` (nesting display).

### 9. Terminal polish: liveness line, context meter, expandable transcript, diffs

**What.** (a) A single status line: `Working (12s • esc to interrupt)` with the compact duration formatter; (b) a context-usage meter (`used / window`, or a percentage) in the status line, toggled by a key; (c) collapsed-by-default tool rows that expand with a key (`ctrl+o` already exists — make the collapsed form genuinely one line, e.g. `Called search_files 3 times`); (d) dedicated diff cells for edits with additive/removal coloring; (e) a `/status` equivalent showing model, permissions, cwd, token usage, and session id.

**Why.** Kova already has `ctrl+t`/`ctrl+o` and `render.tool_detail`/`args_preview`/`duration`, so the bones exist. The missing pieces are the *elapsed-time interrupt hint* (the single most reassuring UI element in Codex) and the context meter, which is what makes compaction legible instead of mysterious.

**Touches.** `src/kova/tui/render.py` (`tool_detail`, `duration`, new diff/meter renderers), `src/kova/tui/app.py` (status line, keys), `src/kova/tui/widgets.py` (meter widget), `src/kova/tui/style.py` (diff colors), `src/kova/ui.py` (non-TUI parity).

### 10. A hooks system with a documented JSON contract

**What.** Fire lifecycle events (`session_start`, `user_prompt_submit`, `pre_tool_use`, `permission_request`, `post_tool_use`, `post_tool_use_failure`, `pre_compact`, `post_compact`, `stop`, `session_end`) to user-configured commands with JSON on stdin and a JSON decision on stdout (`allow` / `deny` / `ask` + reason), `exit 0` = no opinion, per-hook timeout, `async` for observational hooks, and a `statusMessage` for the spinner. Add per-hook trust hashes so an edited hook stays disabled until re-approved.

**Why.** Hooks are how both products became *platforms* — and the local installs show the real use case: reporting agent state to an external supervisor and syncing a memory vault. For Kova this is also the cleanest way to add linting, formatting, notifications and policy without growing `agent.py`.

**Touches.** new `src/kova/hooks.py`, `src/kova/agent.py` (event call sites), `src/kova/config.py` (hook config + trust hashes), `src/kova/permissions.py` (pre-tool-use veto), `src/kova/tui/app.py` (status message).

### Honorable mentions (do later, but design for them now)

- **Deferred tool search** (`defer_loading` / `ToolSearch`): keeps the schema payload small as MCP servers are added. Would touch `src/kova/tools/__init__.py` (`openai_schemas`) and `src/kova/agent.py`.
- **Long-lived shell sessions** (`exec_command` + `write_stdin`): turns `run_command` into REPL/dev-server capable. `src/kova/tools/core.py`, `src/kova/tui/app.py` (`Job` already models a running process).
- **Plan mode** with an explicit `EnterPlanMode`/`ExitPlanMode` pair and a re-injected plan after compaction. Kova already stores `chat['plan']`; it needs the mode gate.
- **Rewind / checkpointing**: Kova writes `file-history/`-style backups already (`src/kova/backup.py`, 427 lines) — surfacing "restore code only / conversation only / summarize from here" would be a small addition with outsized trust payoff.
- **Output styles / personality presets**: a `Concise` / `Explanatory` toggle maps naturally onto `modes.py`.
- **`/doctor`**: `diagnostics.py` (45 lines) is the seed; make it audit unused tools/skills against context cost and detect a missing sandbox dependency.
- **`/import`**: a one-command import of `~/.claude` / `~/.codex` config, `CLAUDE.md`/`AGENTS.md`, and recent sessions would make Kova the easiest switch in this space.

---

# PART 5 — Open questions (explicitly UNSURE)

1. **UNSURE** — Claude Code's in-terminal *rendering* internals are not documented and the installed binary is a bundled/minified JS blob (`~/.local/share/claude/versions/2.1.323`); everything I state about its TUI comes from docs, not source.
2. **UNSURE** — whether Claude Code streams partial tool-call *arguments* into the UI. `--include-partial-messages` streams message text deltas; tool-input streaming is implied by the SDK's partial-message events but I did not verify a UI behaviour for it.
3. **UNSURE** — Claude Code's exact default auto-compact trigger as a percentage. Docs say "compacts when the conversation reaches the model's context limit" and give ~967K for a ~1M model; whether there is a hidden safety margin I could not confirm.
4. **UNSURE** — Codex's remote-v2 compaction protocol (`compact_remote_v2.rs` is large and I only read its call sites); the *behaviour* I describe is the local path plus the shared lifecycle.
5. **UNSURE** — Codex's streaming-patch rendering cadence. `streaming_parser.rs` exists and `parser.rs` imports `StreamingPatchParser`, but I did not trace the UI update path.
6. **UNSURE** — Claude Code `Glob`/`Grep` "absent by default on macOS, Linux and WSL": I quote the tool table, but I did not confirm whether the model gets an equivalent through a `Bash` wrapper or simply uses `rg` ad hoc.
7. **UNSURE** — whether Kova's `desktop/` Electron shell and `src/kova/web_assets` share a rendering layer with `src/kova/tui/`. I mapped GUI items to both directories rather than guess which one owns them.

---

## Appendix — primary sources

**Claude Code docs** (all fetched 2026-11-13): `overview`, `memory`, `sub-agents`, `skills`, `permissions`, `permission-modes`, `sandboxing`, `hooks`, `settings`, `slash-commands`, `interactive-mode`, `statusline`, `plugins`, `mcp`, `checkpointing`, `tools-reference`, `context-window`, `model-config`, `output-styles`, `costs`, `headless` — `https://code.claude.com/docs/en/<page>.md`.

**Codex docs**: `https://learn.chatgpt.com/docs/agent-approvals-security`, `/agent-configuration/agents-md`, `/agent-configuration/rules`, `/agent-configuration/subagents`, `/reference/commands`, `/sandboxing`; plus `https://developers.openai.com/codex/llms.txt` for the index. NOTE: `developers.openai.com/codex/*` returns a client-rendered SPA shell (≈430 KB HTML, no content); the `.md` twins on `learn.chatgpt.com` are the usable source. VERIFIED.

**Codex source** (shallow clone, `main` as of 2026-11-13): `codex-rs/core/src/{compact.rs,compact_token_budget.rs,agents_md.rs,tools/registry.rs,tools/handlers/*}`, `codex-rs/{apply-patch,prompts,sandboxing,tui,memories}/`, `codex-rs/prompts/templates/{compact,permissions}/`.

**Local installs**: `~/.codex/{config.toml,AGENTS.md,hooks.json,plugins,memories}`, `~/.claude/{settings.json,keybindings.json,CLAUDE.md,skills,plugins}`.
