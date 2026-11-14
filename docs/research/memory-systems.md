# Memory & Context Systems — Research Notes

Research sub-agent report for Kova. Every claim is labeled **VERIFIED** (source url or file path + line) or **UNSURE**. No edits/commits/pushes were made; secrets and private chat contents were not read.

- Kova files read: `src/kova/memory.py`, `src/kova/store.py`, `src/kova/tools/memory.py`, `src/kova/project_instructions.py`, `src/kova/agent.py`.
- Local agent configs read (structure only, not chat contents): `~/.claude/CLAUDE.md` (vault instructions), `~/.claude/hooks.json`, `~/.omp/agent/config.yml`, `~/.hermes/config.yaml`, OpenClaw `~/.openclaw/state/openclaw.sqlite`, `~/.openclaw/workspace/*`.
- Web docs read: OpenClaw memory/compaction, oh-my-pi (OMP) context-files & memory, OpenClaw docs, Pi/Honcho, Claude Code memory.

---

## 1. How Kova's memory/context works today

### 1.1 Durable memory notes — `src/kova/memory.py` (+ `tools/memory.py`)
**VERIFIED** (`src/kova/memory.py`, `src/kova/tools/memory.py`):

- **What is stored:** user facts saved as Markdown notes with YAML frontmatter `{title, tags, updated}` + a free-text body. One note per `<slug>.md` file under a root folder (`~/.kova/memory` in practice). Slug = slugified title, max 60 chars, collision → `note-<sha256>` of title+body+updated.
- **Storage:** plain files on disk; writes are atomic (temp file + rename) and guarded by a write lock; symlink/escape protection on write and delete. `write_subdir` can redirect writes.
- **Recall:** `memory_search` tool → `Memory.search(query, limit=5)`. Scoring is pure keyword: `title hit ×3, tag hit ×2, body hit ×1` per query word (≥2-char words), sorted by score then `updated` desc, top 5 returned as JSON (`slug,title,body,tags,updated`). **No embeddings, no vectors, no semantic search** (confirmed: no `embed/faiss/chroma/vector` refs anywhere in `src/kova`).
- **Always-in-context vs on-demand:** memories are injected into the **system prompt** (see §1.3), i.e. *always-in-context but capped at top-5*. There is no separate "search then decide" retrieval path — search results ARE the injected context.
- **User edit/inspect:** `memory_save` (same title overwrites old note), `memory_search`. No dedicated list/browse tool exposed to the model beyond search; `Memory.list()`/`get()`/`delete()` exist in the module but are not wired as model tools (only save+search are in `tools/memory.py`).
- **Size/limit:** `search()` default `limit=5`; no per-note size cap; no compaction of memory.

### 1.2 Chat history + search — `src/kova/store.py`
**VERIFIED** (`src/kova/store.py`):

- **What is stored:** SQLite DB (`~/.kova/kova.sqlite` or `~/.local/share/kova/kova.sqlite`). Tables: `chats`, `messages`, `reminders`. Messages have `role ∈ {user, assistant, tool}`, `content`, `tool_call_id`, `tool`, `arguments`, `updated`.
- **Search:** FTS5 virtual index over `messages` for roles `user`/`assistant` only (tool calls/output never match). Falls back to `LIKE %term%` if FTS5 unavailable. Returns best-ranked match per chat with a snippet (`_like_snippet`). This is the **chat-search** feature (`/search`), not memory search.
- **Plan state:** a chat's `update_plan` writes items into the chat row's `plan` JSON column (the working task list).

### 1.3 The agent loop — `src/kova/agent.py`
**VERIFIED** (`src/kova/agent.py`):

- **System prompt** (`build_system_prompt`, lines 56-66): identity + current time + mode + tool names, then — if any memories — `## title\nbody` for each of the top-5 notes under "What you remember about the user:". **All** passed memories are concatenated; no dedup, no per-note truncation.
- **Memory recall wiring** (line 194): `self.memory.search(text, 5)` runs every turn (in a thread); failures degrade to empty. So recall is **semantic-ish keyword, always injected, capped at 5, on every turn**.
- **History trimming** (`trim_history`, lines 133-146): `budget = context_tokens * 4 * 0.75 - reserved_chars`; it **front-trims** oldest messages, always retains the latest user message, and skips a leading tool message. **No summarization, no compaction** — old turns are dropped wholesale.
- **Tool-result cap:** `MAX_TOOL_RESULT_CHARS = 32_000`; longer results truncated with `[truncated]`.
- **Turn budget:** at most 10 tool rounds per turn (line 214 `range(11)`).

### 1.4 Project instructions — `src/kova/project_instructions.py`
**VERIFIED** (`src/kova/project_instructions.py`):

- Reads `KOVA.md` then `AGENTS.md` from the working dir (resolved via `code_files.resolve_path`). Each capped: reads max 12,001 bytes; skips if >12,000 bytes or contains NUL. Blocks concatenated and capped at **4,096 chars** in the prompt, prefixed with a "never permission to act" disclaimer. Subdirectory instructions are **not** auto-discovered (unlike Claude's on-demand nested CLAUDE.md).

### 1.5 Summary of Kova today
| Dimension | Kova today |
|---|---|
| What is stored | Markdown notes (frontmatter) for user facts; SQLite chats/messages/reminders; chat `plan` JSON |
| Where | `~/.kova/memory/*.md`; `~/.kova/kova.sqlite` (or XDG) |
| Recall | Keyword search (title×3/tag×2/body×1), top-5 injected into system prompt every turn; FTS5 chat search |
| Always-in-context | System prompt + top-5 memories + project instructions (≤4KB) |
| Size/compaction | Front-trim history (no summarization); 32k tool-result cap; 4KB instruction cap; no memory compaction |
| User edit/inspect | `memory_save` (overwrite by title), `memory_search`; no list/browse/delete tool exposed |
| Forgetting/privacy | Implicit via front-trim of history and note overwrite; no explicit retention/forget/privacy policy |

---

## 2. How other agents do memory & context

### 2.1 Claude Code — CLAUDE.md + auto-memory + compaction + skills
Sources: `~/.claude/CLAUDE.md` (vault), official docs `https://code.claude.com/docs/en/memory`.

**VERIFIED:**
- **CLAUDE.md / AGENTS.md** load at session start with a **scope ladder**: managed (`/etc/…`) → user (`~/.claude/CLAUDE.md`) → project (`./CLAUDE.md`/`.claude/`) → local (`./CLAUDE.local.md`). Subdirectory CLAUDE.md / `.claude/rules/**` and path-scoped rules (`paths:` frontmatter) load **on-demand** when matching files are read. Rules can be scoped to file globs (budget 1,000 patterns / 4 MiB). AGENTS.md is read in place of CLAUDE.md when no CLAUDE.md is in scope (v2.1.277+).
- **Auto-memory**: Claude writes notes of 4 types (`user`, `feedback`, `project`, `reference`) into `~/.claude/projects/<project>/memory/` — a `MEMORY.md` index (one line per memory) + topic files (`user_role.md`, `feedback_testing.md`). The **first 200 lines or 25 KB** of `MEMORY.md` load every session; topic files load **on-demand** via file tools. Machine-local; shared across worktrees of one repo; excluded from transcript cleanup. Toggle via `/memory` / `autoMemoryEnabled`.
- **Compaction**: summarizes older turns into a summary entry kept in the transcript; project-root CLAUDE.md is **re-read and re-injected after compaction** (survives); nested/on-demand files reload on next file read. Auto-compaction threshold ~75% of window (or window − reserve); `/compact` manual with focus.
- **Skills**: load on-demand when invoked or deemed relevant; package repeatable workflows.
- **Prompt caching**: cached prompt prefix reused across turns.

### 2.2 Codex — AGENTS.md + `~/.codex/memories`
Source: OpenClaw import doc (`https://docs.openclaw.ai/concepts/memory.md`) describing what it reads from Codex.

**VERIFIED (as reported by OpenClaw's importer):**
- Codex stores consolidated `MEMORY.md` and `memory_summary.md` under `~/.codex/memories` (or `CODEX_HOME/memories`). Raw rollout/transcript files are separate and not imported. This implies Codex has a **consolidated long-term memory file** model. (UNSURE on internal mechanics beyond this — only the import contract is documented.)

### 2.3 Hermes — persistent memory, user profile, session search, skills
Source: `~/.hermes/config.yaml` (structure) + the session's own notes.

**VERIFIED** (`~/.hermes/config.yaml`):
- **Persistent memory**: config keys `memory.provider`, `USER.md`/`profile` sections, `envelope_auto_save_memory`, `autosave`, `persist` — Hermes keeps a persistent kernel + workspace with cross-session memory.
- **User profile**: `USER.md` / profile sections hold stable preferences.
- **Session search**: Hermes supports searching session history (the agent notes "session search").
- **Skills**: Hermes has a skills system (loaded into context).
- **Environment persistence**: `persistent_workspaces`, `persist` flags; local env vars; `cwd`/`env` value deprecation noted.
- UNSURE on exact recall algorithm (semantic vs keyword) — config shows *that* memory persists, not *how* it is retrieved.

### 2.4 OpenClaw — USER.md / MEMORY.md / daily notes / dreaming / hybrid search
Source: `https://docs.openclaw.ai/concepts/memory.md`, `/concepts/compaction.md`, `~/.openclaw/workspace/*`, `~/.openclaw/state/openclaw.sqlite`.

**VERIFIED:**
- **Four memory files**: `USER.md` (stable preferences/directives, small budget, session start), `MEMORY.md` (curated durable facts, session start), `memory/YYYY-MM-DD[-slug].md` (daily working notes; today+yesterday load on `/new`/`/reset`), `DREAMS.md` (dream diary for human review). Loaded at session start with a **separate small budget**.
- **Provenance**: workspace provenance records mark files trusted/untrusted; untrusted/remote files are excluded from automatic memory context (logged, listed by `openclaw doctor`).
- **What goes where**: USER.md = directive preferences; MEMORY.md = curated durable facts; daily = raw working context indexed but **not** injected every turn.
- **Dreaming** (background consolidation): collects short-term recall signals, scores, promotes qualified items into `MEMORY.md` (thresholds: score, recall-frequency, query-diversity; untrusted/system items taint-gated out). Follows sleep-time-compute + Generative Agents lessons.
- **Automatic memory flush** before compaction: a private copy of the conversation reminds the agent to save durable facts to daily/MEMORY.md before the conversation is summarized. Native provider flush / file flush / tools-based flush variants.
- **Search**: hybrid = vector (semantic) + keyword (exact IDs/symbols). Default engine SQLite; also PostgreSQL (`ts_rank`), LanceDB, Honcho backends. Tools: `memory_search`, `memory_get`, `intent` (standing/event-conditioned intents).
- **Forgetting**: `memory forget` removes indexed text+vectors + source rows; `openclaw memory reset` clears derived data (keeps files + schema); `memory index --force` rebuilds.
- **Compaction** (`/concepts/compaction.md`): summarizes older turns; keeps tool-call/result pairs together; full history stays on disk; auto-compaction at 75% window or window−reserve; `/compact` manual with focus (≤800 code points); degraded summaries keep latest request + identifiers, drop longest identifiers first; native provider compaction (OpenAI Responses, Codex, Claude) delegated where available.
- **Action-sensitive memories**: notes capture approval/permission, temporary constraints, handoffs, expiry, safe-to-act timing, authority.

### 2.5 oh-my-pi (OMP) — per-project memory, retain/recall/reflect, compaction
Source: `https://github.com/can1357/oh-my-pi/blob/main/docs/context-files.md`, `https://omp.renranz.cn/docs/memory` (Chinese), `~/.omp/agent/config.yml`.

**VERIFIED:**
- **Context files** (not memory): `CLAUDE.md`/`AGENTS.md`-style instructions auto-discovered and injected at session start; **sticky rules** from `RULES.md` are *always-on* (not on-demand). This is the instruction layer, distinct from memory.
- **Memory is per-project (cwd)**, isolated in `~/.omp/agent/memories/`. Two backends: **local** (on-disk, per project) or **Hindsight** (cloud vector bank). Commands: `/memory view`, `/memory clear`, `/memory enqueue`; subcommands `/memory mm seed|list|refresh`.
- **retain / recall / reflect** lifecycle: `retain` = model saves facts; `recall` = injects into context; `reflect` = consolidation. Local memory sits beside **context compression** (compaction) which keeps a single session in-window.
- **Compaction**: turn-overflow / post-turn `?` value / manual `/compact`; summary entries replace old turns + recent tail. Local memory is separate from compaction.

### 2.6 Pi (pi.dev) — cross-session memory via Honcho extension
Source: `https://honcho.dev/docs/v3/guides/integrations/pi.md`.

**VERIFIED:**
- Pi starts each session from scratch; the **pi-honcho** extension adds cross-session memory: peer card + session summary injected at session start; recall every turn (`honcho_chat` / `honcho_search`); automatic capture after every run. Session mapping: `per-directory` (matches Claude Code plugin) / `git-branch` / `chat-instance`. Max 30s recall wait. Shares `~/.honcho/config.json` with the Claude Code Honcho plugin.

---

## 3. The vault — a hand-built second brain
Source: the maintainer's own note-taking setup (described at a high level only).

**VERIFIED** (these are the user's own instructions to me, the AI, describing how the vault works):
- **Structure**: a notes folder with `index.md` (the brain's root/navigation) + `meta/` (conventions). It is a **Markdown second brain**, not a live local memory store — it "lives in GitHub and syncs to the AI, not the user's machine."
- **Hybrid recall**: `index.md` is a **table of contents** (always-in-context pointer); the vault also has a **searchable index** for recall-by-search. So it combines always-in-context navigation + on-demand search.
- **Editing**: the user edits the brain in **Obsidian** (their machine); the AI edits via **GitHub commits** (pushed from the AI machine). Sync is GitHub ⇄ Obsidian.
- **Forgetting/retention**: explicit **forgetting policy** + "memory decay" concept (not all memories are equally durable). The AI writes notes, commits, pushes; the user reviews.
- **Conventions** (`meta/conventions.md`): how the AI should write notes (structure, frontmatter, linking) so they remain searchable and durable.

This is a strong reference model for Kova: **persistent, searchable, human-in-the-loop, with an explicit forgetting policy** — but it is manual and GitHub-mediated, whereas Kova's memory is agent-written and local.

---

## 4. Cross-system comparison (what each does)

| Capability | Kova | Claude Code | Codex | Hermes | OpenClaw | OMP | Pi/Honcho | Vault |
|---|---|---|---|---|---|---|---|---|
| Instruction layer (scope ladder + on-demand) | KOVA.md/AGENTS.md ≤4KB, cwd only | full ladder + rules + on-demand | AGENTS.md | skills | USER.md directives | context files + sticky rules | — | — | index.md |
| Durable memory files | notes (frontmatter) | MEMORY.md index + topics | MEMORY.md + summary | USER.md | USER.md/MEMORY.md/daily | local/Hindsight | — | Obsidian md |
| Recall: keyword | **yes** (title×3/tag×2/body×1) | index (topic on-demand) | ? | ? | keyword part of hybrid | retain/recall | honcho_search | searchable index |
| Recall: semantic/vector | **no** | (topic files) | ? | ? | **yes** (hybrid) | Hindsight yes | honcho (semantic) | Obsidian search |
| Always-in-context | top-5 memories + instr | CLAUDE.md + 25KB MEMORY.md | MEMORY.md | USER.md | USER.md/MEMORY.md budget | sticky rules + local mem | peer card + summary | index.md |
| On-demand recall | none (only search→inject) | topic files, rules | — | session search | memory_get, daily | /memory view | honcho_search | open note |
| Compaction/summarization | **none** (front-trim only) | yes | ? | ? | yes (rich) | compaction | — | — | — |
| Background consolidation | **none** | none (on-write) | — | ? | dreaming | reflect | capture | memory decay |
| Pre-compaction flush | none | re-read CLAUDE.md | — | ? | yes | — | — | — |
| User edit/inspect | save+search | /memory | — | — | /context, doctor | /memory view | /honcho | Obsidian |
| Forgetting/privacy | implicit overwrite + trim | edit/delete | — | — | memory forget/reset | /memory clear | — | forgetting policy |
| Provenance/trust | none | yes | — | — | yes | — | — | (Obsidian-trusted) |

---

## 5. Ranked proposal — 8 memory/context improvements for Kova

Ranked by **impact on user experience × effort × alignment with proven models** (OpenClaw/Claude/vault). Each names the Kova file(s) it touches.

### #1 — On-demand semantic search + hybrid recall (highest impact)
Currently recall = keyword top-5 injected every turn; no semantic path, no on-demand `memory_get`. Add a lightweight embedding or BM25 keyword+semantic hybrid and a `memory_get`/`memory_search` recall flow the agent decides when (OpenClaw model).
- **Files:** `src/kova/memory.py` (add scoring: embeddings or TF-IDF/BM25; `search` + `get`), `src/kova/tools/memory.py` (expose `memory_get`, richer `memory_search`), `src/kova/agent.py` (call recall only when prompt/context doesn't answer; §1.3 `build_system_prompt`).
- **Why #1:** it unlocks the biggest gap (semantic + on-demand) and is the backbone everything else builds on.

### #2 — Compaction / summarization of trimmed history
`trim_history` currently drops old turns with zero summarization (line 141). Replace front-trim with a summarizer that folds older turns into a compact summary entry (OpenClaw/Claude model). Preserves far more usable context per budget.
- **Files:** `src/kova/agent.py` (`trim_history` + a summarize step), `src/kova/store.py` (store summary entries; FTS index should ignore them), `src/kova/agent.py` (re-inject summary boundary).
- **Why:** directly extends effective context window — the single biggest "more brain per token" win.

### #3 — Curated layered memory (USER.md-style directives vs daily notes)
Split the flat note store into a **stable preferences layer** (directive-style, inlined always) and a **working/daily layer** (indexed, on-demand), plus an index like Claude's `MEMORY.md`. Adds durability + reduces prompt noise.
- **Files:** `src/kova/memory.py` (note categories/priority + index file), `src/kova/tools/memory.py` (`memory_save` writes category; new `memory_list`), `src/kova/agent.py` (`build_system_prompt` inlines only the stable layer).

### #4 — Background consolidation / reflection (dreaming)
Add an occasional pass that promotes stale/frequent notes and merges duplicates into a curated summary (OpenClaw dreaming; vault memory-decay). Keeps memory quality without per-turn cost.
- **Files:** `src/kova/memory.py` (merge/dedup/promote logic), `src/kova/tools/memory.py` (new `memory_reflect` or a scheduled tool), `src/kova/agent.py` or `store.py` (schedule the sweep).
- **Why:** cheap, compounding quality win; mirrors proven "dreaming."

### #5 — Provenance, trust & privacy controls
Add file-trust/privacy tags and an explicit forget/delete path (OpenClaw provenance; vault forgetting policy). Currently memory overwrite is the only "forget."
- **Files:** `src/kova/memory.py` (privacy flag on notes; `delete` already exists — expose it), `src/kova/tools/memory.py` (`memory_delete`, `memory_list`), `src/kova/agent.py` (exclude untrusted notes from auto-inject).
- **Why:** correctness/trust; cheap since `delete` exists.

### #6 — On-demand subdirectory / scoped instructions
`project_instructions` only reads cwd `KOVA.md`/`AGENTS.md` (≤4KB). Add Claude-style on-demand loading of nested instructions and path-scoped rules when the agent touches matching files.
- **Files:** `src/kova/project_instructions.py` (discovery + on-demand trigger), `src/kova/agent.py` (inject when a matching file is read), `src/kova/tools/code_files.py` (resolve scoped paths).
- **Why:** lets Kova scale to large repos without bloating every prompt.

### #7 — Editable/inspectable memory surface (human-in-the-loop)
Add `memory_list`/`memory_get` exposed to the user (not just the model), plus a way to review/edit notes — the vault's Obsidian + OpenClaw `/context` model. Bridges the gap that the vault closes manually.
- **Files:** `src/kova/tools/memory.py` (`memory_list`, `memory_get`), `src/kova/cli.py` / `src/kova/tui/*` (a memory review command), `src/kova/store.py` (if memory is later moved to SQLite).

### #8 — Session/project scoping + cross-session identity
Kova keys memory by nothing project-specific today; notes are global. Add per-project memory roots (Claude's `~/.claude/projects/<project>/memory` model) so facts don't bleed across unrelated repos, and persist a lightweight cross-session identity.
- **Files:** `src/kova/memory.py` (per-project `root`), `src/kova/config.py` (project dir resolution), `src/kova/agent.py` (pass project id to memory search).
- **Why:** correctness at multi-repo scale; moderate effort, lower user-visible impact than 1–4.

**Ordering rationale:** 1 and 2 change how much *useful* context Kova has (largest UX lift). 3 and 4 keep that context *high-quality over time*. 5 is trust/correctness. 6–8 are scale/robustness that matter once recall + compaction exist.

---

## 6. Open uncertainties (UNSURE)
- **Codex internal memory mechanics**: only its on-disk shape (`MEMORY.md`/`memory_summary.md`) is documented via OpenClaw's importer; how Codex *recalls* is UNSURE.
- **Hermos recall algorithm**: config shows persistence + `USER.md`/profile + session search exist, but whether it is semantic vs keyword is UNSURE.
- **OMP local memory retrieval specifics**: retain/recall/reflect are named, but the exact local recall method (keyword vs vector) is UNSURE from the Chinese docs.
- **Pi native (without Honcho)**: base pi has no memory by design (`pi.dev` "starts every session from scratch"); memory is the Honcho extension. UNSURE whether pi has any built-in memory beyond that.
