# Hermes Agent (Nous Research) & OpenClaw — research notes for Kova

Research sub-agent output. Read-only: nothing was edited, committed, or pushed in any repo.

**Method / sources.** Both projects are installed on this machine, so most claims below are verified
against the shipped source and the shipped copy of each project's official docs:

| Subject | Install | Version | Source of truth used |
|---|---|---|---|
| Hermes Agent | `~/.local/bin/hermes` → `~/.hermes/hermes-agent` | 0.23.1 (`~/.hermes/hermes-agent/install-stamp.json`) | Python source in `~/.hermes/hermes-agent/` + `~/.hermes/hermes-agent/website/docs/**` + live <https://hermes-agent.nousresearch.com/docs/> |
| OpenClaw | `~/.openclaw/tools/node/bin/openclaw` → `…/node_modules/openclaw` | 2026.10.9 (`…/openclaw/package.json`) | Bundled `dist/` + `docs/**` + `skills/**`, both shipped in the npm package; live <https://docs.openclaw.ai/> |

I deliberately did **not** open `~/.hermes/config.yaml`, `.env`, `auth.json`, `sessions/`, `state.db`,
`~/.openclaw/openclaw.json`, `state/openclaw.sqlite`, or any chat transcript. Persona/onboarding
templates (`SOUL.md`, `BOOTSTRAP.md`) were read because they are shipped defaults, not user data.

Labels: **VERIFIED** = I read the file or fetched the URL myself (path/URL given). **UNSURE** = inference,
second-hand, or unverified.

Useful Kova baseline for comparison (all VERIFIED by reading the file):
`src/kova/agent.py` is 305 lines: `build_system_prompt()` at `agent.py:56`, an 11-step tool loop at
`agent.py:214`, `trim_history()` char-budget trimming at `agent.py:133`. Memory is Markdown notes with
YAML frontmatter and a hand-rolled keyword scorer (`src/kova/memory.py:169`). SQLite `chats`,
`messages`, `reminders` + FTS5 live in `src/kova/store.py:111`. Tools are ~13 functions registered as
`Tool(...)` (`src/kova/tools/*.py`). Reminders are a 15 s tick loop with `notify-send`
(`src/kova/reminders.py:47`). Surfaces: Typer CLI (`src/kova/cli.py`), Textual TUI (`src/kova/tui/`),
FastAPI server (`src/kova/server.py`), packaged web UI (`src/kova/web_assets/`), Electron shell
(`desktop/`).

---

# Part A — Hermes Agent

Design thesis, stated in its own contributor guide: (a) **per-conversation prompt caching is sacred** —
nothing may mutate past context or rebuild the system prompt mid-conversation, because that breaks the
cached prefix and multiplies user cost; (b) **the core is a narrow waist** — new capability should arrive
as a CLI command + skill, a service-gated tool, or a plugin, almost never as a new core tool.
VERIFIED: `~/.hermes/hermes-agent/AGENTS.md` ("Two invariants shape almost every design decision").

## A1. Agent loop and tool design

- **Facade + siblings layout.** `run_agent.py` is a thin `AIAgent` facade; the loop body is
  `agent/conversation_loop.py`, and each turn phase is its own module (`agent/turn_iteration_prep`,
  `turn_api_call`, `turn_api_error`, `turn_overflow`, `turn_truncation`, `turn_recovery`). There are 370
  files under `agent/`. VERIFIED: `developer-guide/agent-loop.md`;
  `ls ~/.hermes/hermes-agent/agent | wc -l`.
- **Three API modes converge on one internal message format**: `chat_completions`,
  `codex_responses` (OpenAI Responses), `anthropic_messages` (native Messages + prompt-cache markers).
  Mode resolution order: explicit arg → provider detection → base-URL heuristic → default. VERIFIED:
  `~/.hermes/hermes-agent/website/docs/developer-guide/agent-loop.md` (section "API Modes").
- **Turn lifecycle**: append user msg → build/reuse cached system prompt → preflight compression if
  >50% of context → build provider messages → inject *ephemeral* prompt layers (budget warnings) behind
  the cache boundary → make an **interruptible** API call → if `tool_calls`, execute and loop; if text,
  persist and return. VERIFIED: same file, "Turn Lifecycle".
- **Interruptible calls.** The HTTP call runs on a background thread while the main thread waits on
  {response, interrupt event, timeout}. On interrupt the thread is abandoned and no partial response is
  injected into history. This is what makes "send a new message to redirect the agent" safe. VERIFIED:
  same file, "Interruptible API Calls".
- **Tool execution.** One call → main thread. Multiple calls → `ThreadPoolExecutor`, except tools marked
  interactive (`clarify`) which force sequential. Results are re-inserted in the original call order
  regardless of completion order. Per call: registry resolve → `pre_tool_call` plugin hook → danger check
  + user approval → execute → `post_tool_call` hook → append `role:"tool"`. VERIFIED: same file, "Tool
  Execution".
- **Four agent-level tools are intercepted before the registry** because they mutate agent state:
  `todo`, `memory`, `session_search`, `delegate_task` (`agent/tool_executor.py`, table
  `INLINE_TOOL_EXECUTORS`). VERIFIED: `developer-guide/agent-loop.md` + `tools/AGENTS.md`.
- **Tool registry**: `tools/registry.py` has no deps and is imported by every tool file; each
  `tools/*.py` calls `registry.register()` at import time; discovery is automatic. Every handler returns
  a **JSON string** (except vision tools, which may return a multimodal envelope). Availability is
  `check_fn` TTL-cached process-wide — it answers "is this reachable/opted-in", never "should this
  surface exist". VERIFIED: `~/.hermes/hermes-agent/tools/AGENTS.md`.
- **Toolsets are the gating layer.** A tool is registered by discovery but only *exposed* if a toolset
  names it; `_HERMES_CORE_TOOLS` is the default bundle every platform inherits, and `TOOLSETS` is a
  single dict keyed `browser, clarify, code_execution, cronjob, debugging, delegation, file, image_gen,
  kanban, memory, messaging, moa, rl, safe, search, session_search, skills, terminal, todo, tts, video,
  vision, web`. VERIFIED: `~/.hermes/hermes-agent/toolsets.py:11` and `:70`; `tools/AGENTS.md`.
- **Budgets and failover.** Default iteration budget 500 (`agent.max_turns`); subagents get an
  independent cap (default 50). On 429/5xx/401/403 the loop walks `fallback_providers`; a 401/403 first
  tries credential refresh. Auxiliary tasks (vision, compression, titling, background review) each have
  their own fallback chain. VERIFIED: `developer-guide/agent-loop.md`, "Budget and Fallback Behavior".
- **Compression.** Preflight at >50% of window; gateway auto-compression at >85% between turns; memory is
  flushed to disk *before* summarizing; middle turns become a summary; last N messages
  (`compression.protect_last_n`, default 20) stay verbatim; tool-call/result pairs are never split; a
  compression generates a child session lineage id. VERIFIED: `developer-guide/agent-loop.md`,
  "Compression and Persistence"; `developer-guide/context-compression-and-caching.md` (title only).
- **Programmatic tool calling** (`execute_code`): the agent writes a Python script that imports a
  generated `hermes_tools.py` stub; tool calls travel over a Unix-socket RPC back to the agent host; only
  `print()` output returns to the model. Explicit trigger heuristic: "3+ tool calls with processing logic
  between them". VERIFIED: `user-guide/features/code-execution.md`.
- **Tool Search** (progressive disclosure for many MCP/plugin tools): replaces deferred tool schemas with
  three bridge tools and loads each schema on demand. Core tools stay eager; a `defer` list can defer
  cold built-ins. VERIFIED: `user-guide/features/tool-search.md`.
- Docs claim "60+ built-in tools"; the package ships 306 files under `tools/` (not the same number).
  VERIFIED for the claim: <https://hermes-agent.nousresearch.com/docs/>; UNSURE for any exact tool count.

## A2. Memory, user profile, session search, skill creation/self-improvement

**Two bounded curated files, injected as a frozen snapshot.**
- `MEMORY.md` = agent's notes (environment facts, conventions, lessons), **2,200 chars (~800 tokens)**.
- `USER.md` = user profile (identity, preferences, style, expectations), **1,375 chars (~500 tokens)**.
- Both in `~/.hermes/memories/`, rendered into the system prompt at session start with a header showing
  `67% — 1,474/2,200 chars` and entries separated by `§`. The snapshot is **frozen for the session** to
  preserve prefix cache; writes land on disk immediately but appear in the prompt only next session.
  VERIFIED: `~/.hermes/hermes-agent/website/docs/user-guide/features/memory.md`, "How It Works" and
  "Frozen snapshot pattern".

**Tool surface: `add` / `patch` / `remove`, with a required top-level `target` of `memory` or `user`.**
There is deliberately no `read` (content is already in the prompt) and **no `replace`** — it was removed
because models used it as find-and-swap and wiped sibling facts in multi-fact entries. `patch` replaces
one span (`old_text` → `content`), and rejects a span that matches more than once. VERIFIED: same file,
"Memory Tool Actions", "Two Targets Explained".

**Capacity is enforced, not silently truncated.** An over-limit add returns a structured error with
`current_entries` and the exact overflow ("…would exceed the limit by 153 chars. Retry as ONE 'operations'
batch that removes or shortens … the limit is checked only on the batch result"). The agent then frees
space in the same turn. Verified failure-mode notes: small local models (<~30B) and weak tool-callers
*claim* they saved a memory without calling the tool. VERIFIED: same file, "What Happens When Memory is
Full", "Troubleshooting".

**What to save vs skip is prompt-level policy, not user discipline.** Memory guidance is generated by
`build_memory_guidance()` and routes by scope: "Skills come first" for procedures/preferences tied to a
kind of work; memory is "the narrow exception for facts that apply to EVERY session". It also demands
**declarative** phrasing ("User prefers concise responses" ✓, "Always respond concisely" ✗) because
imperatives get re-read as directives later and can override a live request. VERIFIED:
`~/.hermes/hermes-agent/agent/prompt_builder.py:195-237`.

**Security of memory writes.** Entries are scanned for injection/exfiltration patterns and invisible
Unicode before acceptance (they go straight into the system prompt). `memory.write_approval: true` stages
every write outside the interactive CLI (including background-review writes) behind
`/memory pending|approve|reject|approval on`. VERIFIED: `memory.md`, "Security Scanning",
"Controlling memory writes".

**Session search is the unbounded half of memory.** All CLI + messaging sessions live in
`~/.hermes/state.db` with FTS5; `session_search` returns real messages, **no LLM call**, ~20 ms
discovery / ~1–2 ms scroll. Four shapes inferred from arguments: discovery (`query`), scroll
(`session_id` + `around_message_id` + `window`), browse, and detail levels. Discovery is
"adaptive": top hit gets full context + bookends, lower hits stay compact. VERIFIED:
`user-guide/features/memory.md` "Session Search"; `user-guide/sessions.md:954-994`.

**Skills = procedural memory, with progressive disclosure.** Level 0 `skills_list()` (~3k tokens of
name+description), level 1 `skill_view(name)` full `SKILL.md`, level 2 `skill_view(name, path)` for a
`references/` file. Skills are `SKILL.md` with YAML frontmatter (`name`, `description`, `version`,
`platforms`, `metadata.hermes.{tags,category,fallback_for_toolsets,requires_toolsets,config}`) and a
fixed body shape: When to Use / Procedure / Pitfalls / Verification. VERIFIED:
`user-guide/features/skills.md`, "Progressive Disclosure", "SKILL.md Format".

**`skill_manage` actions**: `create`, `patch` (preferred; targeted `old_string`→`new_string`), `patch`
with `content` (full rewrite; `edit` is a legacy alias), `delete`, `write_file`, `remove_file`. Advisory
linter flags "incident-log-shape" (dense PR/issue numbers), "references-sprawl" (>60 reference files),
"oversized-body" (>~24k chars). VERIFIED: `skills.md`, "Agent-Managed Skills".

**`/learn`** is the cheapest self-improvement primitive: point it at a directory, a URL, a PDF, or "the
workflow you just walked me through", and the live agent authors the skill with its normal tools — no
separate ingestion engine, so it works on every surface and terminal backend. Large corpora become a
lean `SKILL.md` + one distilled file per chapter under `references/` (loaded on demand). Re-running
`/learn` on the same topic folds into the existing skill. VERIFIED: `skills.md`, "Learning a skill from
sources".

**The closed loop is a background fork of the agent, not a cron job.** After a turn, `run_conversation`
may spawn a daemon thread that replays the conversation snapshot in a **forked `AIAgent`** — same
provider/model/credentials/cached system prompt so it hits the same prefix cache — and asks
"should any skill/memory be saved or updated?". Writes go straight to the stores; the main conversation
and its cache are never touched. VERIFIED: docstring of `~/.hermes/hermes-agent/agent/background_review.py`
lines 1–8.

- Trigger: `_memory_nudge_interval = 10` and `_skill_nudge_interval = 10` **turns**
  (`agent/agent_init.py:1160`, `:1194`, `:1236`; ticked in `agent/turn_context.py:813-830`).
- The review prompt is blunt and quality-controlled. Memory prompt includes a **routing block** (one
  fact, one store, never both) and a **grounding rule**: "Save only facts GROUNDED in this conversation —
  a user message or tool result you can point to. Never infer the user's traits, skill level, role, mood
  or preferences from topic, tone or tools used… If unsure, leave it out." VERIFIED:
  `agent/background_review.py:363-392`.
- The skill prompt says the opposite of "do nothing": "Be ACTIVE — most sessions produce at least one
  skill update, even if small. A pass that does nothing is a missed learning opportunity, not a neutral
  outcome", plus a target library shape (class-level skills, small topical `references/`) and an explicit
  **do-not-capture** list: environment-dependent failures, negative claims about tools ("browser tools do
  not work"), transient errors that resolved, one-off task narratives, unresolved failures dressed up as
  "recommended approach". VERIFIED: `agent/background_review.py:398-470`.
- Cost/shape controls: `auxiliary.background_review.{enabled,max_input_tokens,extra_tools,defer,defer_max_age_s}`;
  routing the review to a cheaper model switches it to a **digest** replay (recent turns verbatim +
  older summary) because a different model can't reuse the cache anyway; on the **managed local
  llama-server** reviews are **deferred by default** until the machine is idle, and a preempted review is
  re-queued rather than discarded. VERIFIED: `user-guide/features/memory.md`, sections
  "Running the review on a cheaper model", "Capping review cost", "Local models: reviews wait for an idle
  GPU".
- Gating: `memory.write_approval` / `skills.write_approval` stage writes, with `/memory pending` and
  `/skills pending|diff|approve|reject`. Skill diffs are deliberately out-of-band because a `SKILL.md` is
  too big for a chat bubble. VERIFIED: `memory.md`, `skills.md`.

**Curator = garbage collection for self-written skills.** Inactivity-triggered (not cron): on CLI start,
gateway housekeeping, and the `serve` maintenance timer, if `interval_hours` (7 d) and `min_idle_hours`
(2 h) both passed, it runs a fork of the agent. Phase 1 is deterministic: unused 14 d → `stale`, 30 d →
archive into `~/.hermes/skills/.archive/` (never delete), with a grace floor for never-used skills and
skips for pinned skills or any skill referenced by a cron job. Phase 2 (LLM umbrella-building /
consolidation) is **opt-in** (`consolidate: false`). First run on a new install only seeds `last_run_at`.
VERIFIED: `user-guide/features/curator.md`, "How it runs".

**Learning Journey (`/journey`) = the learning loop made visible.** A timeline of learned skills + memory
chunks plotted over time, with a "constellation" scrubber that replays the build-up; also where you
prune/correct (`journey list|delete|edit`). Same data in terminal, TUI overlay, and Desktop "Star Map".
VERIFIED: `memory.md`, "Learning Journey".

**External memory providers** (Honcho dialectic user modelling, Hindsight, Mem0, Supermemory, RetainDB,
ByteRover, …) run *alongside* the built-in files and add knowledge graphs / semantic search / automatic
fact extraction. VERIFIED: `memory.md`, "External Memory Providers"; `user-guide/features/honcho.md`
(title only).

## A3. Gateway / multi-channel and background/scheduled work

- **One gateway process, many platforms.** The table in the docs lists 28 surfaces: Telegram, Discord,
  Slack, Google Chat, WhatsApp (+ Cloud API), Signal, SMS, Email, Home Assistant, Mattermost, Matrix,
  DingTalk, Feishu/Lark, WeCom (+ callback), Weixin, BlueBubbles (iMessage), Photon, QQ, Yuanbao,
  Microsoft Teams, LINE, ntfy, Raft, IRC, Buzz, SimpleX, plus a webhook/API-server front end.
  Capability matrix per platform (voice / images / files / threads / reactions / typing / streaming) is
  explicit rather than assumed. VERIFIED: `~/.hermes/hermes-agent/website/docs/user-guide/messaging/index.md`.
- **Deny-by-default access.** Unknown users are denied unless allowlisted or paired by DM; unknown senders
  get a one-time pairing code (`hermes pairing approve telegram XKGH5N7P`). "The safe default for a bot
  with terminal access." VERIFIED: `messaging/index.md:392,418-427`.
- **One chat = one continuous session.** A Telegram DM survives restarts/crashes/reboots; the next
  message continues where it left off. The docs are candid that this is a double-edged sword (compaction
  cost grows, the forget→recall→search loop rarely fires) and recommend `/new` at natural boundaries.
  VERIFIED: `memory.md`, "Memory Needs Session Boundaries".
- **Presence and delivery polish**: live typing indicators per platform, progressive message edits for
  streaming, tool-progress bubbles with auto-delete (`cleanup_progress`), a one-shot "the agent is back" /
  "was interrupted" message on gateway restart (`gateway_restart_notification`, default true), "still
  working" heartbeat, reasoning blocks, `[SILENT]`/`NO_REPLY` suppression. VERIFIED: `messaging/index.md`
  (sections around lines 891–1009); `features/heartbeat.md`.
- **Cron is owned by the gateway daemon**, ticking every 60 s, jobs in `~/.hermes/cron/jobs.json`,
  execution history in `~/.hermes/cron/executions.db`, a `.tick.lock` file preventing double ticks, and
  each due job running as a **fresh `AIAgent` session** with an optional set of attached skills.
  VERIFIED: `user-guide/features/cron.md`, "How it works" + `~/.hermes/cron/` listing.
- **Cron is agent-manageable.** One `cronjob_manage` tool with action-style ops (create/list/pause/edit/
  run/remove) so "every weekday at 7 summarise overnight updates and post it here" works by asking. Model
  resolution at fire time is per-job pin → `cron.model` → main model, and the agent **cannot** repoint a
  job at a different model (user-owned decision). Per-job reasoning effort pins exist the same way.
  Runaway protection: cron sessions cannot create more cron jobs. VERIFIED: `cron.md`,
  "What cron can do now", the warning block, "Creating scheduled tasks".
- **No-agent mode**: a job can be a pure script whose stdout is delivered verbatim, zero LLM cost
  (`wakeAgent` / `--no-agent`). Plus `context_from` to chain job outputs, webhook-triggered jobs, catch-up
  windows, misfire policy, per-job timezone, delivery to origin chat / files / any platform, delivery
  ledger with an explicit `UNVERIFIED` state, failure-incident alerts with cooldown, `hermes cron doctor`
  for fleet health, and "Overdue since" surfacing when the ticker stops. VERIFIED: `cron.md` headings +
  sections "Delivery confirmation and the UNVERIFIED state", "Failure incidents", "Fleet health check".
- **`/heartbeat` is a different primitive**: one recurring instruction *inside the current session*,
  injected as an ordinary user-role message between turns, idle-only, missed ticks coalesce to one,
  user messages always win, cache-safe, survives restart via `SessionDB.state_meta`, and can end with
  `NO_REPLY` so a quiet check posts nothing. The injected prompt includes a **don't-invent-work guard**.
  Rule of thumb in the docs: needs this conversation's context → `/heartbeat`; self-contained →
  cron. VERIFIED: `user-guide/features/heartbeat.md`.
- **Related autonomy primitives**: `/loop` (recurring prompt with cadence + stop conditions), `/goal`
  (a standing objective judged by a lightweight model after each turn; feeds continuation prompts back
  into the same session — their take on the "Ralph loop"), and Kanban (a board of cards dispatched to
  worker processes, optionally in goal mode). VERIFIED: `features/loops.md`, `features/goals.md`,
  `features/kanban.md` (title + intro).
- **Delegation**: `delegate_task` spawns child `AIAgent`s with isolated context; only the child's final
  summary enters the parent's context; batch up to 10 concurrent by default; children get independent
  iteration budgets; an optional `output_schema` makes the child's answer a validated JSON contract with
  exactly one bounded correction turn on failure, and a failed contract keeps the child's raw work
  (`schema_valid: false`) instead of discarding it. Background completions are durable and at-least-once
  across restarts. VERIFIED: `user-guide/features/delegation.md`.

## A4. UI/UX: TUI, desktop/web, onboarding

- **TUI** (`hermes` / `hermes tui`): "instant first frame" banner painted before the app finishes loading,
  non-blocking input (you can type and queue your first prompt while tools load), modal overlays for model
  picker / session picker / approvals / clarify, a live session panel that fills in progressively,
  alternate-screen differential rendering (no flicker, no scrollback junk), paste-collapse for long
  snippets, mouse-friendly selection. Startup banner has four collapsible sections (Tools open by default;
  Skills, System Prompt, MCP Servers collapsed). The TUI talks to its own gateway over a socket and
  degrades gracefully on disconnect. VERIFIED: `user-guide/tui.md`, "Why the TUI", "Collapsible banner
  sections", "How the TUI talks to its gateway".
- **The status line is a dashboard**: agent state (`starting agent… / ready / thinking… / running… /
  interrupted`), working dir + live git branch, `⏱ 12s/3m 45s` per-prompt and session timers, `🗜️ N`
  auto-compressions, `▶ N` background tasks, `⚠ YOLO` when auto-approve is on. VERIFIED: `tui.md`,
  "Status line".
- **Desktop** is "chat-first with a left sidebar": streaming replies with structured tool-call summaries,
  expandable tool rows that can show the image a vision tool looked at, a right-hand preview rail
  (web pages/files/tool output side-by-side), **browser comment mode** (click elements on a live page, get
  numbered pins; "Add N comments" attaches cropped screenshots + CSS selector + computed styles, and
  redacts password/hidden fields and anything key/token-shaped before sending), hide-vs-close for stateful
  panes (a hidden browser keeps form state and timers; a hidden terminal keeps its shell), a conversation
  timeline rail, reading-position memory, in-transcript find, per-session YOLO toggle, a context-usage
  meter that breaks down system prompt / tools / skills / memory / rules / MCP / conversation, optional
  cache-hit-rate and tok/s readouts, and a composer model picker that never silently rewrites your
  default. VERIFIED: `user-guide/desktop.md`, "Chat", "Status bar".
- **Same session everywhere**: sessions started in Desktop resume in CLI/TUI and vice versa; config,
  skills, and memory are shared per profile. VERIFIED: `desktop.md`, "Chat" bullets.
- **Bot Mode** (desktop): each *profile* appears as a bot with its own avatar (geometric face, upload,
  AI-generated portrait, or pixel pet), its own canonical "Bot Chat", its own **Routines** (recurring
  tasks backed by cron), a `Sessions | Bots` tab strip, group chats where several bots deliberate
  (member avatars, latest room line, "needs you" badge), `@researcher have a look at this` handoffs, and a
  bot-to-bot messaging protocol injected into each bot's Bot Chat. VERIFIED: `desktop.md`, "Bot Mode
  (built in)".
- **HUD mode**: `⌘/Ctrl+Shift+H` detaches the chat into a chrome-free always-on-top bar over whatever
  you're working on; the bar's position is *context* ("this", "here", "that page" resolve to what's
  underneath), tap-to-summon gesture, snap-to-pointer hotkey. VERIFIED: `desktop.md`, "HUD mode".
- **Onboarding**: installers (`install.sh` / `install.ps1`) then `hermes setup` (full wizard) or
  `hermes setup --portal` (one OAuth for model + web search + image gen + TTS + cloud browser). Desktop
  first-run has a redesigned overlay flow with an explicit **"Choose provider later"** escape hatch, and
  Settings is a real UI for providers/keys/tools/MCP/gateway rather than YAML editing. Telegram setup has
  a **"Create with QR"** card that creates the bot, detects your user id for the allowlist, saves
  credentials and restarts the gateway. VERIFIED: `README.md`; `desktop.md`, "Settings & onboarding";
  <https://hermes-agent.nousresearch.com/docs/>.
- **Profiles** isolate config/skills/sessions/memory per `HERMES_HOME`; one process can serve many
  profiles, which is also why memory is per profile. VERIFIED: `memory.md` caution block;
  `user-guide/profiles.md` (exists).

## A5. Local models and their quirks

- **Managed llama.cpp, zero knobs.** Hermes downloads and manages the inference engine, prices **every
  catalog model against your machine before download** (green "Fits your GPU" / amber "Uses system RAM" /
  red "Too big"), picks the highest-quality quant that runs fully in GPU memory, **never offers below
  4-bit**, guarantees **at least a 64K context window** for recommended models, and recommends the
  highest-quality model predicted to hit 20 tok/s. VERIFIED: `user-guide/local-models.md`.
- **Context grows, then compresses.** A local model starts at a window that fully fits the GPU and grows
  toward its native max as the conversation needs room ("Context window grown" in the status feed); when
  it can't grow further it compresses instead of claiming a window the server didn't get. Overflow is
  placed in system RAM in the least harmful order (expert weights first, never the attention cache).
  Idle models unload after 15 minutes and reload on the next message. VERIFIED: same file, "How memory
  management works".
- **Local-model behaviour quirks are documented, not hidden**: small local models (<~30B) and weak
  tool-callers will say "I've saved that" without calling the memory tool (fix = stronger model for
  setup, not more instructions); grammar-constrained local backends never emit another action's parameter
  slot, and the tool schema is designed so such a call is rejected before any op in a batch applies;
  background reviews are deferred while the local GPU is busy; review input budgets derive from the
  model's window (a 65,536-token model gets 49,152). VERIFIED: `memory.md` troubleshooting +
  "Local models: reviews wait for an idle GPU"; `skills.md`, "Actions" note.

## A6. What makes Hermes feel alive

- **A durable identity file, slot #1 in the system prompt.** `~/.hermes/SOUL.md` is seeded if missing,
  never overwritten, loaded only from `HERMES_HOME` (not cwd) so personality can't change by accident when
  you `cd`. `/personality` presets layer per session; skins change the UI live. VERIFIED:
  `user-guide/features/personality.md`.
- **It notices affection.** A regex detector (`good bot`, `ily`, `thank you`, `<3`, hearts) fires a
  `reaction_callback` so the CLI pet, TUI heart and desktop hearts react — deliberately narrow ("this is
  great" does not fire) and token-free. VERIFIED: `~/.hermes/hermes-agent/agent/reactions.py:1-45`.
- **A pet that mirrors work state.** Optional Petdex mascot with six animation states mapped from real
  agent activity (failed → `failed`, all todos done → `jump`, turn done → `wave`, tool running → `run`,
  thinking → `review`, blocked on you → `waiting`), rendered at full fidelity via kitty/Ghostty/WezTerm/
  iTerm2/sixel or a truecolor half-block fallback. Purely cosmetic by design. VERIFIED:
  `user-guide/features/pets.md`.
- **Memory you can see and prune** (`/journey` star map with playback), plus a "💾 Memory updated" chat
  line after background saves (`display.memory_notifications: off|on|verbose`). VERIFIED: `memory.md`.
- **Ambient surfaces**: HUD over your work, voice mode + wake word, `/insights`, Bot Mode rosters with
  avatars and group chats, restart notices ("the agent is back"), and a learning loop the product markets
  as the differentiator. VERIFIED: `desktop.md`; `README.md`; `features/heartbeat.md`.

---

# Part B — OpenClaw

Design thesis: **trusted gateway, untrusted execution, deterministic policy** — one local control plane
for sessions/tools/events/channel connections, with model providers and agent harnesses as swappable
plugins. Stewarded by the OpenClaw Foundation (501(c)(3)), MIT, no paid tier; built for "Molty", a space
lobster. VERIFIED: `~/.openclaw/tools/node/lib/node_modules/openclaw/README.md`;
<https://docs.openclaw.ai/>.

## B1. Agent loop and tool design

- **Gateway RPC is the entry point**: `agent` validates params, resolves the session, persists metadata
  and returns `{runId, acceptedAt}` **immediately**; `agent.wait` waits for the terminal outcome and
  returns `{status: ok|error|timeout, startedAt, endedAt, error?}` plus `terminalReply` and a
  `terminalReceipt` (with `sourceReplyDelivered` — proof the reply actually reached the external chat, not
  just the transcript). VERIFIED: `~/.openclaw/tools/node/lib/node_modules/openclaw/docs/concepts/agent-loop.md`,
  "Run sequence".
- **Serialization with lanes.** Runs are serialized per session key, then admitted to a global `main` lane
  (parallelism `max(8, cpu*4)` by default) or a subagent budget. Before streaming, an admitted run records
  a durable `activeWriterRunId` claim and **every transcript append supplies `expectedWriterRunId`**, so a
  superseded run cannot commit stale transcript data; a state-dir lock prevents two gateways owning the
  same state. VERIFIED: same file, "Queueing and concurrency".
- **Command queue modes are a UX primitive, not plumbing**: `/queue` = `steer` (default; inject into the
  running turn, including mid-tool-execution, checked after each tool batch), `followup`, `collect`,
  `interrupt`, with a 500 ms debounce, cap 20, and `drop: summarize`. Priority has a *bounded* head start
  (foreground can overtake by ≤15 s, background by ≤30 s) so newer work can't starve older work. Typing
  indicators fire on enqueue so the wait feels acknowledged. VERIFIED: `docs/concepts/queue.md`.
- **Layered architecture, deliberately**: provider (auth/model discovery) / model / **agent runtime**
  (`openclaw` embedded, `codex`, `copilot`, or a CLI backend like `claude-cli`) / channel. A turn can be
  "Anthropic model executed through the Claude CLI", so OpenClaw is a gateway around other harnesses
  rather than a rival to them. VERIFIED: `docs/concepts/agent-runtimes.md`.
- **Two hook systems.** Internal `HOOK.md` scripts for command/lifecycle events (`command:new`,
  `command:reset`, `command:stop`, `agent:bootstrap`), and typed plugin hooks
  `api.on(...)`: `before_model_resolve`, `before_prompt_build` (can inject `prependContext`, `systemPrompt`,
  or narrow the tool surface with `toolsAllow`), `before_agent_reply` (can claim the turn and return a
  synthetic reply **or silence it entirely**), `agent_end`, `before/after_compaction`,
  `before/after_tool_call`, `tool_result_persist` (synchronous rewrite before transcript write),
  `message_received/sending/sent`, `session_start/end`, `gateway_start/stop`, `before_install`. Block
  semantics are explicit: `{block:true}` is terminal, `{block:false}` is a no-op that does **not** clear a
  prior block. HTTP webhooks are separate from hook events. VERIFIED: `docs/concepts/agent-loop.md`,
  "Hooks".
- **Tools: act / instruct / extend.** A *tool* is a typed callable (`exec`, `browser`, `web_search`,
  `message`, `image_generate`, `read/write/edit`, `sessions_*`, `memory_*`); a *skill* is a `SKILL.md`
  instruction pack; a *plugin* adds runtime capability (tools, providers, channels, hooks, packaged
  skills). The model only sees tools that survive profile + allow/deny policy + provider restrictions +
  sandbox + channel permissions + plugin availability. VERIFIED: `docs/tools/index.md`.
- **Notable tool shapes worth stealing:**
  - `ask_user`: 1–3 structured questions with options, a real timeout, **main session only** (subagents
    never get it), rendered as native buttons on Telegram/Discord/Slack/Mattermost, a stepper panel in the
    Control UI, arrow-key/`Other…`/`Skip` in the TUI, and a plain-text answer accepted on any channel when
    permissions match. Rationale is spelled out: only for decisions that genuinely belong to the user.
    VERIFIED: `docs/tools/ask-user.md`.
  - `progress_card`: *one* durable card per session holding a ≤50-step plan (at most one `in_progress`)
    and/or a compact Markdown note; every call replaces the whole card; the card is session state the UI
    reads on reconnect, and the transcript keeps only a short receipt. Subagents never get it — the parent
    owns progress. A reminder prompt is injected only when a renderer is actually paired, and it forbids
    invented percentages. VERIFIED: `docs/tools/progress-card.md`.
  - `message` with `react`: emoji reactions per channel, `trackToolCalls: true` to reuse one status
    reaction for the turn's lifecycle. VERIFIED: `docs/tools/reactions.md`.
  - `/btw` (alias `/side`): a one-shot side question about the current session that **never enters
    history**, with a deliberate separation between BTW (session's real model, one-shot overhaul) and
    Side chat (persistent read-only RPC thread for Control UI clients). VERIFIED: `docs/tools/btw.md`.
  - Code Mode / Tool Search / Swarm / Lobster / subagents / ACP agents — a large optional surface for
    orchestrating concurrent agents. VERIFIED: `docs/tools/index.md` routing table.
- **Safety is layered and specific.** Permission modes `deny / allowlist / ask / auto / full`; approvals
  are evaluated on the **execution host** and can only *tighten* config; the effective policy is the
  stricter of `tools.exec.*` and approvals defaults. Approved host runs bind **canonical execution
  context** — cwd, exact argv, env binding, pinned executable real-path plus a content hash for writable
  executables — and are denied if the resolution drifts during a human approval wait; for shell scripts
  they also try to bind one concrete local file operand. They state plainly that file binding is
  best-effort and that they refuse to mint an approval-backed run rather than pretend coverage.
  VERIFIED: `docs/tools/exec-approvals.md`.
- **Loop protection.** Hard-coded: a turn always stops after **three consecutive identical tool errors**
  (same tool, args, error), independent of config. Optional `tools.loopDetection`: rolling pattern
  detection + unknown-tool retries, and a **post-compaction guard** that aborts if the same
  `(tool, args, result)` triple repeats after a compaction retry (breaking the
  overflow→compact→same-loop cycle). VERIFIED: `docs/tools/loop-detection.md`.

## B2. Memory: files, user model, search, dreaming, self-learning

- **"No hidden state": memory is plain Markdown in the workspace** (default `~/.openclaw/workspace`).
  Four layers: `USER.md` (optional; stable preferences, style, relationships, active projects, written as
  directives), `MEMORY.md` (durable non-profile facts, standing decisions), `memory/YYYY-MM-DD.md` (daily
  working notes; today's and yesterday's load on a bare `/new`), `DREAMS.md` (dream diary + sweep
  summaries for humans). VERIFIED: `docs/concepts/memory.md`.
- **Explicit division of labour**: `MEMORY.md` = compact curated layer available at session start;
  daily files = the working layer, indexed but **not** injected; distillation into `MEMORY.md` is done by
  the dreaming sweep, while the generated workspace instructions still encourage saving durable facts as
  you work. If `MEMORY.md` outgrows the bootstrap budget it stays intact on disk but the injected copy is
  **truncated** (visible via `/context list`, `/context detail`, `openclaw doctor`). VERIFIED: same file.
- **User model with supersession.** Preferences are written as imperative directives with observed-date
  and active/superseded metadata; when a preference changes you **supersede it in place** instead of
  appending a contradiction. On a shared Gateway, personal `USER.md` files live at
  `users/<canonical-profile-id>/USER.md` and are selected by *authenticated identity* (owner first, else
  creator), never by a display name or a model-supplied id; `personal_instructions` reads before saving
  with a content hash; budget 4,000 chars. VERIFIED: `docs/concepts/user-model.md`.
- **Built-in memory engine = per-agent SQLite index with hybrid search**: FTS5 keyword/BM25 + vector
  embeddings (many providers incl. `local` GGUF via llama.cpp, Ollama, LM Studio) + hybrid with **MMR
  diversity on by default**, deterministic ranking by relevance/recency/write-time importance, CJK trigram
  tokenization, optional `sqlite-vec` run in a **separate read-only process** with ≤2 processes, 30-minute
  idle retirement. Chunking: 400 tokens / 80 overlap. VERIFIED: `docs/concepts/memory-builtin.md`.
- **Timed-out recall degrades instead of failing.** If semantic retrieval hits the 30 s tool deadline but
  keyword hits are ready, `memory_search` returns them with a partial-result warning. VERIFIED: same file.
- **Dreaming = scheduled consolidation with a reviewable audit trail** (this is the single most
  interesting memory idea in either project). Enabled by default, one managed cron job, default
  `0 3 * * *`. Three phases per sweep: **Light** (stage + dedupe recent short-term material, no durable
  write), **REM** (themes/reflections, no durable write), **Deep** (score, gate, promote — the only phase
  that writes `MEMORY.md`). VERIFIED: <https://docs.openclaw.ai/concepts/dreaming> (fetched live; identical
  shipped copy at `docs/concepts/dreaming.md`).
  - **Gates before the model sees anything**: `minScore`, `minRecallCount`, `minUniqueQueries` must all
    pass; then a **structural taint gate** removes candidates whose provenance is `untrusted` or `system`
    (a taint gate, "not a score penalty").
  - **The model returns operation decisions, not replacement prose.** A writer applies add/merge/supersede
    decisions to the existing file using each candidate's bounded sourced entry, so the LLM cannot
    hallucinate memory text into the file.
  - **Rewrite safety**: preimages stored in SQLite before an accepted rewrite; a rewrite is rejected if it
    removes more than `phases.deep.maxPriorEntryLossFraction` (default 0.25) of prior entries, if any
    promoted candidate lacks its `Source: path#Lx-Ly` reference, or if it exceeds the bootstrap-safe
    budget; on model-unavailable or validation failure it **falls back to append-only promotion**.
  - **Scoring weights** are published: relevance .30, frequency .24, query diversity .15, recency .15,
    consolidation .10, conceptual richness .06, plus a small recency-decayed boost for light/REM hits.
  - **Dream Diary**: `DREAMS.md` gets a short narrative per sweep (even when nothing promoted), generated
    by a tool-free completion; a failed/empty diary writes a local fallback entry so degradation leaves a
    visible trace. The diary is human-facing and explicitly **not** a promotion source.
  - **Injection defence inside memory**: ingestion excludes non-interactive sessions (cron, heartbeat,
    subagent, incognito), removes runtime-marked recalled context so recalled snippets can't be relearned
    as new memory, and honors a memory admission policy + `memory forget` with recorded reasons.
  - CLI: `memory promote [--apply]`, `memory promote-explain "router vlan"`, `memory rem-harness`,
    `memory session-backfill`, all with `--rollback` lanes.
- **Session search** (`sessions_search` + `sessions_history`): searches user/assistant text in visible
  past sessions, returns `sessionKey`/`messageId`/excerpt, and requires all three ids to reopen the exact
  matched context — including pre-reset retained history. Visibility defaults to `all` (cross-agent,
  including other users' conversations!) and is governed by `tools.agentToAgent`; excerpts are redacted
  before returning; incognito sessions are always excluded. Control-UI search does prefix matching on the
  final word; the agent tool keeps exact-word matching. VERIFIED: `docs/concepts/session-search.md`.
- **Skills**: `SKILL.md` + YAML frontmatter, filtered at load time by environment/config/binary presence,
  with a 7-tier precedence (workspace > project `.agents/skills` > personal `~/.agents/skills` > managed >
  workshop > bundled/custodian > extra dirs/plugins) and collision summaries per discovery root.
  VERIFIED: `docs/tools/skills.md`.
- **Skill Workshop = learned skills with history and undo.** Agent-written skills are owned by that agent
  and bypass allowlists; every change applies immediately, **saves the previous version first**, and is
  undoable. Unused learned skills are archived after 30 days. It's explicit that learned skills are
  *procedures*, not facts (facts → memory), and that knowledge about one codebase belongs in that repo's
  `AGENTS.md` rather than a skill. VERIFIED: `docs/tools/skill-workshop.md`.
- **Self-learning** is on by default (`skills.workshop.autonomous.mode: "auto"`) and happens in three
  places: the foreground agent patches a skill that misled it or saves a procedure after hard multi-step
  work; a **background review** catches what the foreground missed; unused-skill cleanup archives. The
  review is queued when a session reaches **10 model iterations** since the last review, or immediately
  after any turn that read a learned skill (so a skill that just helped or hurt gets a fresh look); it
  starts after **30 s with no active run**, runs one at a time, and the foreground reply never waits.
  VERIFIED: `docs/tools/self-learning.md`.
  - It forks the conversation, reuses the same provider/model/tool schemas (cache reuse), disables model
    fallbacks, and runs under a private session. Read-only tools plus `skill_workshop` as the only
    mutator; `exec`/`write`/`message` return "not available in this background run", which is not treated
    as failure. The review **must `view` a skill before patching it**, and an archive needs
    `absorbed_into` or a `reason`.
  - It prefers patch > add `references/templates/scripts` > create, and **merges skills covering the same
    class of task into one umbrella**, archiving the rest with `absorbed_into`.
  - Its do-not-capture list matches Hermes': transient/environment failures, negative tool claims,
    unresolved guesses, single-codebase knowledge, one-off tasks, personal facts, secrets.
  - **Announcement is one line with an undo affordance**: "💾 Learned: updated `deploy-staging` (tightened
    the rollback step). Say 'undo' to revert this skill change."
- `USER.md`/`MEMORY.md` imports from Codex, Claude Code and **Hermes** exist in the Control UI
  (`Settings → Import Memory`), landing under `memory/imports/<source>/` and staying out of the bootstrap
  file. Nice migration affordance. VERIFIED: `docs/concepts/memory.md`, "Import from coding assistants".

## B3. Gateway / channels and background work

- **Gateway = local control plane** for sessions, tools, events, channel connections; Control UI, CLI and
  TUI are clients of it. Channels include Discord, iMessage, Slack, Teams, Telegram, WhatsApp, Signal,
  Google Chat, Matrix, Mattermost, IRC, LINE, Feishu, WeCom, WeChat, SMS, Twitch, Nostr, X, QQ, Zalo,
  Synology Chat, Nextcloud Talk, and more (IMAP exists as an automation trigger rather than a chat channel)
  — plus platform "nodes" (macOS/iOS/Android/Windows/Linux apps) that add voice, Canvas, camera, screen and
  device-local actions. VERIFIED: `README.md`; `ls docs/channels`; `docs/automation/imap.md`.
- **Pairing is an explicit two-place concept**: DM pairing (who may talk to the bot) and node pairing
  (which devices may join). Unknown senders get an **8-character uppercase code with no ambiguous
  characters (`0O1I`), expiring in 1 hour, max 3 pending per channel account**, and their message is not
  processed; approval grants DM access only, never group access. The first CLI pairing can bootstrap
  `commands.ownerAllowFrom` so there is an owner for privileged commands. VERIFIED: `docs/channels/pairing.md`.
- **Ambient room events — the best "feels alive" mechanism in either project.** For always-on group
  rooms (`unmentionedInbound: "room_event"` + `visibleReplies: "message_tool"`), the agent **processes
  unmentioned chatter as quiet context**: it can update memory and session state but the room stays silent
  unless it explicitly calls `message(action=send)`. That kills the old `NO_REPLY` prompt hack: the agent
  listens, decides when a reply is useful, and *cannot* accidentally speak. Supported on Discord guild
  channels, Slack channels/MPDMs, Telegram groups. Two silent gotchas are documented (mention gating must
  be off; the tool profile must include `message`). VERIFIED: `docs/channels/ambient-room-events.md`.
- **Automations (alias `openclaw cron`)** run **inside the gateway process**; definitions, runtime state
  and run history persist in SQLite; one-shots auto-delete after confirmed delivery; per-run wall clock
  `--timeout-seconds`, isolated/detached jobs bounded by a 60-minute scheduler watchdog; overdue jobs are
  *rescheduled, not replayed* at startup; timer ticks coalesce after sleep. VERIFIED:
  `docs/automation/cron-jobs/how-it-works.md`.
- **Run reconciliation is unusually rigorous**: finalized results are matched by run identity rather than
  start time; a run whose delivery attempt was recorded but not confirmed stays **Unknown** and disabled
  for inspection instead of being retried; recovery never replays side effects; a reclaimed dead owner
  records an interruption without replaying a consumed one-shot; catch-up delays pace recovery but never
  expire it. VERIFIED: same file, "Run reconciliation".
- **Job promotion is a product behaviour, not a metaphor**: when you ask for substantially the same job
  several times, the agent offers to schedule it; there is **no repetition-detection engine** — the agent
  recognizes the repeat from the conversation and checks `automations(action:"list")` first. It restates
  the schedule in plain words for confirmation ("Every weekday at 07:00 Europe/Vienna, I summarize
  overnight updates and post them here"), then creates the job **enabled** and immediately runs it once in
  `force` mode as a visible test. The reasoning for enabled-not-disabled is worth stealing verbatim:
  "a job left disabled waiting for a confirmation that never arrives is invisible to every guard, hidden
  from the default list, and will never fire or explain itself — a silent non-outcome, which is a worse
  failure than a job that runs and visibly complains." Failing jobs raise a notification and auto-disable
  after repeated errors. VERIFIED: same file, "Promoting a repeated job into an automation".
- **Heartbeat** is a *system-owned monitor automation* on the main session, default **30 min**, delivering
  owner-routed alerts or staying silent; distinct from user-authored automations. VERIFIED:
  `docs/automation/index.md` comparison table.
- **Standing orders** = permanent operating authority written into `AGENTS.md` (auto-injected every
  session): scope, triggers, approval gates, escalation rules, plus an explicit "What NOT to do" list, and
  automations that *reference* the standing order rather than duplicating it. VERIFIED:
  `docs/automation/standing-orders.md`.
- Also: webhooks, Gmail PubSub/IMAP triggers, internal hooks, Lobster resumable pipelines, subagents,
  ACP coding-harness sessions, Swarm. VERIFIED: `docs/automation/index.md`.

## B4. UI/UX: Control UI, TUI, apps, onboarding

- **Control UI is a small Vite + Lit SPA served by the Gateway** on `http://<host>:18789/` (optional
  `basePath`), speaking **directly to the Gateway WebSocket on the same port**. It shows a **presence**
  sidebar: per-person rows with avatar presence rings (solid green = active, amber = idle, hollow =
  connected but activity unavailable), running/open counts, hovercards with recent sessions and "Viewing
  now", live narration that pauses when the tab is hidden. Drafts survive reloads and are shared across
  tabs; unsent messages that couldn't be restored get a review path. VERIFIED: `docs/web/control-ui.md`.
- **TUI** runs in two modes: gateway mode (`openclaw tui`, remote via `--url/--token`) or **local embedded
  mode** (`openclaw chat`, `openclaw tui --local`). Bare `openclaw` *picks a target automatically*: an
  unconfigured install runs inference onboarding, invalid config opens doctor guidance, a reachable
  configured gateway opens the TUI in gateway mode, otherwise a configured local model opens local mode.
  That single dispatch rule is a very good default. VERIFIED: `docs/web/tui.md`.
- **Progress drafts**: one channel message edited in place while the agent works (status headline +
  authored plan steps + approvals), instead of a stack of "still working" replies; intermediate tool
  failures and nonzero exits stay out of the draft unless `toolProgress: true`. VERIFIED:
  `docs/concepts/progress-drafts.md`.
- **Typing indicators are configurable per moment**: `never / instant / thinking / message`, with
  different defaults for DMs, mentioned groups, unmentioned groups, message-tool-only replies and
  heartbeat runs, 6 s keepalive. VERIFIED: `docs/concepts/typing-indicators.md`.
- **Onboarding = CLI wizard + a first-turn "birth sequence".** Installer starts the wizard; `openclaw
  onboard --install-daemon` verifies model access, creates the workspace, configures the Gateway.
  Then, on the first real turn, `BOOTSTRAP.md` drives a short ritual: (1) ask what to call the agent (and
  explicitly *not* invent a name), (2) one vibe line + signature emoji, (3) **four generated avatar
  options in a single 2×2 sheet**, then crop the chosen quadrant into `avatars/avatar.png` (≤2 MiB),
  (4) persist identity **twice** — `IDENTITY.md` + `SOUL.md` *and* `openclaw agents set-identity` so
  channels and UI agree — then offer the minimal vs maximum plugin set and delete `BOOTSTRAP.md`. It
  front-loads a rule that matters: "The user's request always comes first… This file is a ritual, not a
  gate." VERIFIED: `~/.openclaw/workspace/BOOTSTRAP.md` (shipped template);
  `docs/start/bootstrapping.md`.
- **`SOUL.md` gets its own personality guide** (tone, opinions, brevity, humour, boundaries — "short beats
  long, sharp beats vague") and a copy-paste "Molty prompt" that rewrites it to have opinions, delete
  corporate rules, ban "Great question", and end with "Be the assistant you'd actually want to talk to at
  2am. Not a corporate drone. Not a sycophant. Just… good." VERIFIED: `docs/concepts/soul.md`.
- Desktop-native surfaces: macOS app onboarding explains the trust model up front (personal agent = one
  trusted operator boundary; shared setups need lock-down), picks local vs remote Gateway, installs
  missing runtimes, then hands off to dashboard onboarding. VERIFIED: `docs/start/onboarding.md`.

## B5. Local models and quirks

- **`llama-cpp` plugin** manages a local `llama-server` (or connects to one you run); ownership is decided
  by whether `models.providers.llama-cpp.localService` exists. Setup reads the **Gateway host's** hardware
  (memory, GPU, free disk) and recommends a model; a browser connected to a remote Gateway installs on the
  Gateway, not on the browser's machine. VERIFIED: `docs/plugins/llama-cpp.md`.
- **Many local providers** are first-class: Ollama (with `think`/`thinking` control overrides), LM Studio,
  llama.cpp/GGUF, and local GGUF embeddings for `memory.search.provider: "local"`. Model refs must be
  provider-prefixed in allowlists (`ollama/gemma4:26b`) — bare filenames are not enough. VERIFIED:
  `docs/concepts/models.md:82,261`; `docs/concepts/memory-builtin.md`.
- **Local-model quirks are engineered around, not wished away**: `BOOTSTRAP.md` is kept **out of the
  privileged system context** on embedded/local runs and passed through the **user prompt** instead, "so
  models that don't reliably call the `read` tool can still complete the ritual"; a run with no safe
  workspace access gets a limited-bootstrap note instead of a generic greeting. VERIFIED:
  `docs/start/bootstrapping.md`, "Embedded and local model runs".
- Model failover, compaction and a pluggable context engine exist; model refs + runtime are separate so a
  local model and a cloud CLI backend can coexist in one deployment. VERIFIED: `docs/concepts/model-failover.md`
  (title), `docs/concepts/compaction.md` (title), `docs/concepts/context-engine.md` (title).

## B6. What makes OpenClaw feel alive

- **It has a body and a birthday.** Identity files (`IDENTITY.md`, `SOUL.md`), a chosen name, a vibe line,
  an emoji, a generated avatar, and a first-run "hatching" ritual that ends with "Ask me anything; for
  system things I'll ask OpenClaw." VERIFIED: `~/.openclaw/workspace/BOOTSTRAP.md`;
  `docs/start/bootstrapping.md`.
- **It sleeps and dreams.** Literal sleep phases (light → REM → deep), a `DREAMS.md` dream diary with a
  visual "dream scene" in the Control UI, `/dreaming status|on|off`. Mechanically it is memory
  consolidation; experientially it gives the agent a life-cycle story. VERIFIED:
  <https://docs.openclaw.ai/concepts/dreaming>.
- **It notices who is around.** A `presence` tool the agent can call about *people*: `list`, `person: me`,
  `device: …`, with `devices`/`network`/`location` detail and `observedAt` — with careful honesty rules
  ("missing activity is `null`, not evidence that someone is inactive"; matching names or IPs never
  establish device↔person identity). VERIFIED: `docs/concepts/presence.md`.
- **It can sit in a room and say nothing** (ambient room events) — the strongest "colleague, not bot"
  behaviour in either project. VERIFIED: `docs/channels/ambient-room-events.md`.
- **It reacts, types, and shows work.** Emoji reactions used as turn status, typing-mode tuning, and one
  message edited into a live progress draft. VERIFIED: `docs/tools/reactions.md`,
  `docs/concepts/typing-indicators.md`, `docs/concepts/progress-drafts.md`.
- **It learns out loud and can be told "undo".** One-line `💾 Learned: …` announcements with a spoken undo
  path, plus history/undo in the UI. VERIFIED: `docs/tools/skill-workshop.md`.
- **Lore as product design** ("Molty", space lobster, `soul.md`, `/lore`). Cheap, memorable, and it makes
  the agent feel authored. VERIFIED: `README.md`.

---

# Part C — Sharp contrasts, and what *not* to copy

| Dimension | Hermes | OpenClaw |
|---|---|---|
| Core metaphor | A self-improving personal agent with a single Python loop | A trusted local gateway around swappable harnesses |
| Memory | 2 tiny curated files injected every session + FTS5 session search | Markdown workspace (MEMORY/USER/daily/DREAMS) + SQLite hybrid index + scheduled dreaming |
| Learning loop | Fork of the agent after N turns; memory + skills; curator prunes | Foreground patching + background review after 10 iterations; workshop w/ undo + 30-day archive |
| Scheduling | Gateway cron (60 s tick) + per-session `/heartbeat` + `/goal` | Gateway automations (SQLite) + heartbeat monitor automation + standing orders |
| Channel access | Deny-by-default allowlist/pairing per platform | Pairing codes (8 char, 1 h, 3 pending) + access groups + ambient rooms |
| Execution surface | Toolsets per platform; approvals; 6 terminal backends | Tool policy + permission modes + host exec approvals binding argv/executable hash |
| UI | TUI + Desktop (chat-first, HUD, Bot Mode) + web | Control UI (SPA over gateway WS) + TUI (gateway/local) + native apps/nodes |
| Local models | Managed llama.cpp, hardware-priced catalog, 64K floor, idle unload | llama.cpp plugin (managed or external), Ollama/LM Studio, local embeddings |
| Feel | Pets, reactions, star map, SOUL.md, Bot Mode | Birth ritual, avatar, dreams, presence, ambient rooms, undo-able learning |

**Over-engineered for Kova (do not copy wholesale).** Hermes' 317-file `gateway/` (hosted rooms, relaying,
multiplexing, per-profile leases, delivery ledgers, 306 tool files) and OpenClaw's multi-user Gateway
(person profiles, access groups, Swarm/ACP/Lobster, cloud workers) are answers to multi-tenant scale that
Kova doesn't have. Kova's edge is that it is small, single-user, local-first and understandable end to
end; the transferable value is in the *behaviours and contracts*, not the org chart. UNSURE: whether Kova
ever wants team/multi-user support — that decision should drive how much of the profile/multi-user layer
is worth importing.

**One genuine disagreement between the two.** Hermes optimises for prompt-cache purity: memory is a
frozen snapshot, and new facts are invisible to the running session. OpenClaw refreshes `USER.md` on later
turns of a long-lived session. Hermes' reasoning (cache = cost) is verifiable and matters for cloud
models; OpenClaw's matters for long-lived chat sessions. For Kova, whose sessions are short and whose
models may be local (where cache matters far less), the OpenClaw behaviour is probably the better default
— UNSURE, worth a measurement.

---

# Part D — The 10 most valuable things for Kova to adopt (ranked)

Ranked by (value to a local-first single-user assistant) × (fits Kova's current architecture) ÷ (cost).
Kova-specific file paths are what the change would touch.

### 1. A background self-improvement review (memory + skills), forked from the turn
- **What.** After every N user turns (start at 10, mirroring both projects' trigger — Hermes counts user
  turns, OpenClaw counts model iterations), spawn a background fork that
  replays the just-finished conversation and asks one narrow question: *should anything be saved?* Two
  prompts, not one — a **memory** prompt with a routing rule (user-profile vs environment fact, one fact
  one store) and a grounding rule ("save only facts you can point to in a user message or tool result;
  never infer traits from tone"), and a **skill** prompt that says "be active — most sessions produce at
  least one small update" plus an explicit do-not-capture list (environment failures, "tool X is broken",
  transient errors, one-off narratives, unresolved guesses). Writes go to disk immediately; the live chat
  is untouched. Announce it in one line, with undo.
- **Why.** This is the whole differentiator of both projects and the single biggest gap in Kova: today
  Kova only remembers what the user explicitly asks (`memory_save`). Both projects independently arrived
  at "fork the conversation, same model, cache-warm, narrow prompt, whitelisted tools" as the cheapest
  correct shape. It also directly fixes Kova's weakest UX moment — the user says "remember that" and
  nothing durable happens.
- **Kova files.** New `src/kova/review.py` (fork + prompts + tool whitelist). `src/kova/agent.py`
  (`run_turn` end-of-turn hook, turn counter, `build_system_prompt` at `agent.py:56` to carry the new
  guidance). `src/kova/tools/memory.py` (needs `patch`/`remove`, not just overwrite-by-title).
  `src/kova/memory.py` (entry-level ops + budget). `src/kova/store.py` (persist `last_review_turn` /
  pending writes). `src/kova/events.py` + `src/kova/tui/app.py` + `src/kova/web_assets/app.js` (the
  "💾 Memory updated · undo" line).

### 2. Skills as procedural memory, with progressive disclosure
- **What.** A `skills/` directory of `SKILL.md` files (YAML frontmatter: name, description, tags; body:
  When to Use / Procedure / Pitfalls / Verification), exposed to the model as three tools —
  `skills_list()` (~name+description only), `skill_view(name)`, `skill_view(name, path)` for
  `references/*` — plus `skill_manage` (`create`/`patch`/`write_file`/`delete`). Load the *list*, never
  the bodies. Add a `/learn <source>` command that authors a skill from a folder, URL, PDF, or the
  workflow just completed.
- **Why.** Kova's prompt is currently identity + time + tool names + 5 memory notes; there is no place
  for "how we do X here". Both projects converge on `SKILL.md` + progressive disclosure, and both treat
  preferences-about-a-kind-of-work as *skill* content rather than memory — that routing rule alone would
  stop Kova's memory notes from filling with procedures. It is also the highest-leverage, lowest-risk
  capability: pure files + 4 tools, no infra.
- **Kova files.** New `src/kova/skills.py` (discovery, precedence, frontmatter, size caps),
  `src/kova/tools/skills.py` (the four tools), `src/kova/agent.py` (`select_tools` at `agent.py:44`,
  prompt construction at `agent.py:56`), `src/kova/cli.py` (`kova skills list|show|edit`, `/learn`),
  `src/kova/project_instructions.py` (do not duplicate what `AGENTS.md` already teaches — mirror both
  projects' rule).

### 3. Session search as a tool (FTS5 over past chats)
- **What.** Expose `session_search(query)` → top-N past chats with a highlighted snippet, then
  `session_search(chat_id, around_message_id, window)` to scroll. No LLM calls, no summarisation — return
  real messages. Adaptive detail: best hit gets full context plus first/last bookends, others stay compact.
- **Why.** Kova already has the hard part: `messages` + FTS5 (`src/kova/store.py:164`) and
  `search_chats()` at `store.py:380`. It just isn't reachable by the model. Both projects treat
  "unlimited recall that costs nothing until used" as the necessary complement to a small curated memory —
  Hermes documents the split explicitly (memory ≈1,300 tokens always-on vs unlimited free search). This is
  probably the cheapest high-value item on the list: one tool file.
- **Kova files.** New `src/kova/tools/sessions.py`; `src/kova/store.py` (snippet/highlight + window query
  helpers next to `search_chats`); `src/kova/agent.py` (`select_tools`, plus one guidance line in the
  system prompt: "when the user references something from a past conversation, search before asking").

### 4. Memory with a budget, patch semantics, and an approval gate
- **What.** Split memory into two stores — `MEMORY.md` (environment/project facts, ~800 tokens) and
  `USER.md` (who the user is, ~500 tokens) — injected as a block with a live capacity header
  (`67% — 1,474/2,200`). Replace `memory_save`-overwrites-by-title with `add` / `patch(old_text→new)`
  / `remove(old_text)`. Over-budget writes **fail with the exact overflow and the current entries** so the
  agent frees space itself. Add `memory.write_approval` for staged writes reviewable from CLI/TUI/web.
- **Why.** Kova's memory is unbounded in principle and keyword-scored at query time (`memory.py:169`), so
  it neither guarantees the important facts are in context nor caps prompt cost. Both projects independently
  concluded: small, always-injected, curator-managed, with the *agent* doing the consolidation under a hard
  limit — and both found that a `replace`/overwrite action destroys sibling facts, which is why Hermes
  deleted `replace` and kept `patch`. The capacity-header trick (the model can see it's at 80% and
  consolidate proactively) is free.
- **Kova files.** `src/kova/memory.py` (two stores, budget accounting, `patch`/`remove`, atomic writes
  already exist at `memory.py:98`), `src/kova/tools/memory.py` (three ops + targets),
  `src/kova/agent.py:56` (render the block with capacity), `src/kova/config.py` (limits, approval flag),
  `src/kova/cli.py` (`kova memory pending|approve|reject`).

### 5. Scheduled tasks the agent can create (cron with delivery + no-agent mode)
- **What.** Promote reminders into a general job store: prompt jobs (fresh agent turn, optional skills
  attached, optional per-job model), **script-only jobs** (stdout delivered verbatim, zero tokens), one-shot
  and recurring schedules, delivery to the originating chat / a file / a webhook, and a `schedule` tool with
  action-style ops (`create/list/pause/run/remove`) so "every weekday at 7 send me a summary" works by
  asking. Surface "overdue since" when the ticker is dead. Refuse schedule creation from inside a scheduled
  run.
- **Why.** Kova's reminders (`src/kova/reminders.py`) already prove the tick + delivery + notification
  pattern works; extending it is mostly schema. Scheduled *agent* work is what turns an assistant into
  staff, and both projects found the same guardrails necessary: no recursive scheduling, a durable run
  record, explicit overdue surfacing, and a cheap no-LLM path for "just run the script and send me the
  output".
- **Kova files.** New `src/kova/scheduler.py` (tick, job store, delivery), `src/kova/tools/schedule.py`,
  `src/kova/store.py` (jobs + run history tables beside `reminders` at `store.py:130`),
  `src/kova/server.py` + `src/kova/events.py` (delivery + UI events), `src/kova/cli.py`
  (`kova schedule …`), `src/kova/tui/app.py` (job panel).

### 6. `/heartbeat` — a proactive, idle-only recurring prompt *inside* the current chat
- **What.** One recurring instruction per chat, injected as an ordinary user message only when the chat is
  idle and the interval elapsed; missed ticks coalesce into one; user messages always win; a `NO_REPLY`
  marker means "nothing changed, say nothing"; the injected prompt carries a don't-invent-work guard;
  state persists across restarts. Distinct from scheduled jobs, which run in fresh isolated sessions.
- **Why.** This is the cheapest credible answer to "why does it feel alive?" — it lets the agent keep
  watching something *with the conversation's context* without spamming. Hermes ships it precisely because
  cron's isolated sessions can't do that, and it is small enough for Kova: a row in `state` + a check in the
  turn loop. Pair it with silent delivery so a quiet check is genuinely quiet.
- **Kova files.** `src/kova/reminders.py` (second loop: idle heartbeat rather than due-time), `src/kova/app.py`
  / `src/kova/server.py` (idle detection + inject a synthetic user turn), `src/kova/agent.py` (accept an
  injected turn + honour a silence marker), `src/kova/store.py` (heartbeat state per chat),
  `src/kova/cli.py` + `src/kova/tui/app.py` (`/heartbeat every 10m …`, status, pause/clear).

### 7. `ask_user` + a durable progress card
- **What.** (a) A structured question tool: 1–3 questions, each with options + an "Other" escape, a real
  timeout, honoured on every surface (TUI keys, web panel, CLI prompt), returning skipped/expired as
  explicit outcomes. (b) Extend the existing `update_plan` into **one durable card per chat** — plan steps
  (≤50, at most one in progress) and/or a short Markdown note, replaced wholesale on each call, stored as
  session state so the UI restores it after reconnect, with the transcript keeping only a short receipt.
- **Why.** Kova already has `update_plan` (`src/kova/tools/core.py:156`) and an approvals flow
  (`src/kova/permissions.py`), but nothing for "this decision is genuinely yours" and no durable progress
  surface. Both projects invest heavily here; OpenClaw's constraints are the valuable part — main session
  only (never subagents), no invented percentages, replace-don't-append, and session state rather than
  transcript spam.
- **Kova files.** `src/kova/tools/core.py` (progress card replacing `update_plan`, new `ask_user`),
  `src/kova/permissions.py` style pending-promise plumbing for answers, `src/kova/server.py` (SSE events),
  `src/kova/tui/app.py` + `src/kova/tui/widgets.py` (prompt panel + card), `src/kova/web_assets/app.js`
  (question panel above composer, card renderer), `src/kova/store.py` (card state on the chat row).

### 8. Mid-turn steering / message queue
- **What.** While a turn is running, a new user message should either **steer** (injected into the running
  turn, visible to the model after the current tool batch) or be **queued** as a follow-up, per a chat-level
  mode; typing/working indicators acknowledge receipt immediately; queued items are visible and editable,
  and survive a restart.
- **Why.** Kova currently blocks on a turn (`agent.py:214` loop) and its only interaction is approval.
  Both projects treat "you can talk to it while it works" as a core feel difference, and OpenClaw's bounded
  priority head start (so newer work can't starve older) and "typing fires on enqueue" are the two details
  that make it feel responsive rather than chaotic. Kova's `EventBus` already exists to carry the events.
- **Kova files.** `src/kova/agent.py` (turn-level inbox checked between tool batches),
  `src/kova/server.py` (accept message while a turn runs; queue state),
  `src/kova/store.py` (queued messages), `src/kova/tui/app.py` + `src/kova/ui.py` (queue strip, edit/delete),
  `src/kova/web_assets/app.js` (queue UI).

### 9. Loop / stuck detection, and tool-approval binding
- **What.** (a) Hard-stop a turn after **three consecutive identical tool errors** (same tool, same args,
  same error) with a recovery message; reset the streak on any change or success; optionally detect repeated
  no-progress patterns and re-loops after context trimming. (b) When the user approves a command, bind the
  decision to the canonical argv + cwd + resolved executable (and a content hash for writable binaries);
  re-check before launch and refuse if it drifted.
- **Why.** Kova's loop runs 11 steps and `trim_history` can repeat a failing pattern cheaply; a stuck agent
  is the most common "it's broken" report for every agent. The 3-identical-errors rule is 15 lines, always
  on, and catches the real failure mode (identical retry) without punishing legitimate polling. Approval
  binding closes the "user approved `git status`, we ran something else" gap that Kova's
  `permissions.py` currently leaves open.
- **Kova files.** `src/kova/agent.py` (error-streak state + abort reason in the loop at `agent.py:214`),
  `src/kova/permissions.py` (bind approvals to fingerprints; `kova/tools/core.py:64 fingerprint()` already
  exists and can be reused), `src/kova/tools/core.py` (command prep/check path at `core.py:82-96`).

### 10. Onboarding as identity, not credential entry
- **What.** Keep the provider wizard, but after it, run a short first-run ritual in chat: ask what to call
  it (never invent a name), one vibe line + a signature emoji, an avatar (generated or a chosen glyph),
  then persist identity **twice** — a human-readable `SOUL.md`/`IDENTITY.md` in the Kova home *and* the
  config used by CLI/TUI/web/Electron — and close with one line about what it is. Explicit rule: if the
  user's first message is real work, do the work and skip the ritual. Also add the escape hatch both
  projects have: "choose a provider later" so setup never blocks the first conversation.
- **Why.** Kova already has a `SOUL`-shaped hole: identity lives in a dict literal inside
  `agent.py:27-39` (`_IDENTITIES`), so the agent's name/voice is a code constant, not the user's. Both
  projects converge on "identity is a file, persisted twice, chosen by the user in the first five minutes"
  — and both report it as the thing that makes the product feel authored. It is also the cheapest possible
  retention feature.
- **Kova files.** `src/kova/agent.py` (`_IDENTITIES` → load a persona file; prompt slot #1),
  `src/kova/onboarding.py` (post-provider ritual + skip path), `src/kova/config.py` (persona path/fields),
  `src/kova/tui/brand.py` (name/emoji in the banner), `src/kova/web_assets/index.html` + `app.js`
  (name/avatar), `desktop/main.js` (window/tray identity), new `src/kova/persona.py`.

---

## Runner-ups (11–15), for when 1–10 land

11. **Ambient awareness** (OpenClaw): process unmentioned messages in a watched room/file/feed as quiet
    context, reply only via an explicit send tool. Kova's equivalent is a watched folder or an RSS/IMAP
    source, not a chat room. Files: `src/kova/scheduler.py`, `src/kova/tools/messaging.py` (new).
12. **Dreaming-style memory consolidation** (OpenClaw): a nightly sweep with deterministic gates, a
    provenance/taint filter, operation-decisions-not-prose, and a human-readable diary. Kova can start with
    the deterministic half (dedupe/merge/stale-after-N-days) and add the LLM pass later. Files:
    `src/kova/memory.py`, new `src/kova/consolidate.py`, `src/kova/scheduler.py`.
13. **Skill curator** (Hermes): usage counters, `stale`/`archive` after 14/30 days, never delete, skip
    pinned skills and anything a scheduled job references. Files: `src/kova/skills.py`, `src/kova/store.py`.
14. **Programmatic tool calling** (Hermes `execute_code`): let the model write a small Python script that
    calls Kova tools over a local RPC so 10 tool results don't land in context. High value once Kova has
    web/file tools doing multi-step work; high implementation cost. Files: new `src/kova/code_exec.py`,
    `src/kova/tools/__init__.py`.
15. **Tool-search / progressive disclosure** (Hermes): once Kova grows past ~25 tools or adds MCP, replace
    cold tool schemas with a loader. Files: `src/kova/tools/__init__.py`, `src/kova/models.py`.

## Explicitly not recommended for Kova (yet)

- Multi-channel gateway with pairing/allowlists/access groups — real value, but only after the local
  experience is excellent; it drags in a daemon, credentials, and a security surface Kova can't yet test.
- Multi-user profiles / per-person `USER.md` resolution by authenticated identity — OpenClaw's rules are
  excellent and worth re-reading if Kova ever becomes shared, but today they'd be dead code.
- Bot Mode rosters, group chats between bots, HUD, pets, star maps — cheap delight, but cosmetic; do them
  *after* the learning loop and skills exist, otherwise they decorate a product that doesn't yet grow.
- OpenClaw's execution-approval depth (argv/executable hash binding for every interpreter path) — adopt the
  cheap version (item 9b), not the full model.
