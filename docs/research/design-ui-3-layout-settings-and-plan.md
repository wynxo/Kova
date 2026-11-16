# Part 3 — streaming, settings, layout, agents, build order

Covers #189, #184, #212, #200, #182, #183, #195, #204, #205, #193, #192, #191, #202, #203.

---

## 13. Streaming, scroll anchoring and markdown (#189)

**Numbers to design against.** Streamdown splits a response into blocks, memoizes each, and re-parses only the
last, "because once a model finishes a paragraph, that paragraph never changes again"; its author measured
react-markdown at ~10 ms per update by the end of a 10 KB streamed response versus ~0.6 ms
([Building Streamdown](https://haydenbleasel.com/writing/streamdown),
[memoization](https://streamdown.ai/docs/memoization)). Orbit measured ~1.5 ms per segment and 3–8 segments per
reply — but 20+ segments means 30 ms of main-thread work inside an 8.3 ms frame budget at 120 Hz
([Orbit](https://www.orbit.build/blog/measure-once-ai-chat-virtualization)). Orbit also names the killer
detail: async syntax loading makes a message pass through three size states (mounted → markdown → highlighted),
"every state produces a ResizeObserver fire. A virtualization library that trusts the first measurement has
already lost."

For Kova, in vanilla JS:

- Render markdown **per block**, memoized on `(blockText, isLastBlock)`. Never re-parse a whole message per
  token. `app.js` has no framework to do this for you — a `Map<blockKey, Node>` cache plus `replaceChild` is
  the whole mechanism.
- Batch tokens with `requestAnimationFrame` (one flush per frame).
- **Three-state scroll mode**: `pinned` (follow) / `free` (anchored + "Jump to latest ↓" with a new-step
  count) / auto-reset to `pinned` when the user sends. Persist per conversation.
- **The bug nobody expects**: if the reader scrolled up but the *streaming tail row* is taller than the
  viewport, that row starts above the viewport, so size-delta compensation drags the reader along even though
  the UI says "not following"
  ([Chabot](https://chabot.dev/blog/following-a-streaming-transcript-without-fighting-the-reader)). Fix: record
  the first message whose bottom edge is below the viewport top, store its `offsetTop`, and in
  `requestAnimationFrame` do `scrollTop += (current - saved)` — "only movement of the anchored message affects
  that correction. Text arriving below it doesn't"
  ([Devin Schulz](https://devinschulz.com/blog/building-a-chat-interface-that-doesnt-fight-your-scroll-position)).
- Set `overflow-anchor: none` on the scroller and own the anchoring — mixing native anchoring with manual
  correction double-corrects.
- Defer highlighting until a block is complete; render the streaming fence as plain `<pre>`, then swap.

**Markdown specifics:**

- **Math**: vendor KaTeX's whole `dist/` — "the default build expects the KaTeX fonts to be located in a
  directory called `fonts` which is a sibling of `katex.min.css`"
  ([browser](https://katex.org/docs/browser), [font](https://katex.org/docs/font)). Kova already has a
  `fonts/` sibling, so the layout fits. Render math at the AST/markdown level, **not** with auto-render's DOM
  scan (which deliberately skips `<pre>`) ([auto-render](https://katex.org/docs/autorender)).
- **Mermaid**: must init after fonts load or "mermaid will most likely result in rendering graphs that have
  labels out of bounds"; keep `securityLevel: 'strict'` and lazy `import()` on first mermaid fence
  ([usage](https://mermaid.js.org/config/usage)). Always keep the source visible as a code block.
- **Code blocks**: header with language + filename, copy (disabled while streaming — Streamdown does this),
  wrap toggle (persist it), line numbers, and "open in panel" above 100 lines
  ([Streamdown config](https://vercel-streamdown.mintlify.app/customization/configuration)).
- **Tables**: `overflow-x:auto` wrapper plus `th{position:sticky;top:0}`; copy-as-CSV.
- **Footnotes/details**: `<details>/<summary>` is natively accessible with no ARIA
  ([WebAIM](https://webaim.org/techniques/disclosures)). Prefix footnote ids per message or they collide
  across a transcript.
- **Syntax colours (the stack-specific bit):** you already render code server-side with **Pygments**
  (`CODE_THEME = "monokai"`). Keep it — its output is *classes* (`.k`, `.s`, `.c`, `.nf`), exactly what
  token-driven themes want. Write one `.codehilite` stylesheet mapping each class to a `var(--syntax-*)`
  token, and let the theme supply `syntax`. That gives per-theme highlighting with zero JS and zero bundler.
  (Shiki can take CSS variables too, with a documented TextMate-compatibility caveat
  [Shiki theme colors](https://shiki.style/guide/theme-colors) — but it needs a bundler and buys little when
  Python already has the AST.)

---

## 14. Settings (#184), preferences sync (#212), shortcuts (#200)

Precedents, all three converging:

- **VS Code**: search over all settings, "Commonly Used" group at top, `@modified` filter, **changes apply
  instantly** (no Save button), per-setting gear with *Reset Setting* + copy-as-JSON + a
  `vscode://settings/<id>` deep link, and a read-only `defaultSettings.json` listing every default
  ([settings](https://code.visualstudio.com/docs/configure/settings),
  [default settings](https://code.visualstudio.com/docs/reference/default-settings)).
- **Zed** rebuilt settings around one strongly-typed model because "it's impossible to make new settings
  discoverable with a simple JSON file"; changes save automatically and the editor shows real defaults
  (verifiable via `zed: open default settings`) ([blog](https://zed.dev/blog/settings-ui)). The blog is also
  a cautionary tale: distributed settings definitions with no single typed source of truth broke
  auto-updating mid-refactor. **Centralise the registry early.**
- **Raycast**: one searchable surface — "search for anything: a setting, a command, an extension… jump
  straight to the right place" — plus a Shortcuts tab giving "a bird's-eye view of every shortcut"
  ([settings](https://manual.raycast.com/settings)).

**Build for Kova:** a settings **registry** as the single source of truth —
`{ id: 'appearance.density', type: 'enum', default: 'comfortable', scope: 'user', section, label,
description, keywords[], options[], requiresRestart, advanced }` — and render the page from it. This is what
stops `docs/CONFIG.md`, `config.py` (25 KB) and the UI drifting into three ways to say "theme".

- Search over label + id + description + section + keywords, with a "Modified (n)" filter; results jump to
  and highlight the control.
- **Instant apply** for reversible local settings (appearance, density, transcript detail) with an undo toast;
  confirm only for destructive ones (clear data, change DB path, disable approvals).
- **Reset removes the key**, doesn't write the default — that is how you know whether a value is
  intentionally set, and what makes layering work.
- Config stays **file-as-truth**: the UI writes TOML atomically via `tomlkit`; document precedence
  `defaults < user config < project config < env < CLI flag`, and offer a read-only "raw config" view.
- **#212 is a schema problem**: version the preferences blob (`v1`, `v2`) and migrate on import, or the first
  format change orphans everyone's export. Export should include the theme files, not just settings — a theme
  id alone breaks on another machine.
- **#200**: a searchable table listing every command with and without a binding, filterable by keystroke,
  detecting and recording conflicts — that is VS Code's Keyboard Shortcuts editor
  ([keybindings](https://code.visualstudio.com/docs/configure/keybindings)). `docs/SHORTCUTS.md` already
  exists; generate it from the same registry so it cannot lie.
- **Do not** put layout ephemera (pane widths, open tabs, per-turn collapse state) in settings — separate
  state store with a Reset command (§15).

---

## 15. Layout, panels and the command palette (#182, #183, #195, #204, #205)

**Three panes, remembered as state.** VS Code's model: primary side bar / editor / secondary side bar plus a
movable panel; "VS Code will remember the layout of views and panels across your sessions" with a
`View: Reset View Locations` command — layout is *state with a reset command*, not a settings tree
([custom layout](https://code.visualstudio.com/docs/configure/custom-layout)). Zed exposes `agent.default_width`
(640) with proportional `agent.flexible`, and **double-clicking the resize handle restores the default
proportion** ([all settings](https://zed.dev/docs/reference/all-settings)).

**Surface-choice rule #184 should adopt** (consistent across platform guidance):

- Persistent **side panel** = complementary content viewed *alongside* work
  ([M3 standard side sheet](https://m3.material.io/components/side-sheets/guidelines)).
- **Sheet/modal** at compact widths only, where size forces it; Apple: "for compact views… present information
  in a full-screen modal view like a sheet instead"
  ([popovers](https://developer.apple.com/design/human-interface-guidelines/popovers)).
- **Dialog** = blocks and demands resolution ("dialogs disable all app functionality",
  [M3](https://m3.material.io/components/dialogs/guidelines)); reserve it for approvals, which genuinely
  ought to block.
- **Popover** = a few related, transient tasks
  ([Apple](https://developer.apple.com/design/human-interface-guidelines/popovers)).

Concretely: artifacts, the diff for the selected file, the subagent tree and the plan card → side panel on wide
screens; tool-step detail → dialog only when it must block; all of those → full-height sheet on the phone (#202).

**One concrete trap:** an element that is a drawer on mobile and a sidebar on desktop must **not** be a
`<dialog>` — a `<dialog>` is always exposed as a dialog. Use a plain element with `role="dialog"` +
`popover="manual"`, and remove the role when it acts as a sidebar
([Chrome navigation drawer](https://developer.chrome.com/docs/modern-web-guidance/guides/ui-components/navigation-drawer)).

**Command palette (#183).** VS Code's model: one input, modes by prefix (`>` commands, `@` symbols, `#`
workspace symbols), and Cmd+P / Cmd+Shift+P open the same widget with `>` prefilled, so `?` can list the
prefixes and nothing needs memorising. Zed's key idea: **"Available commands depend on what is focused"**
([Zed features](https://zed.dev/features),
[prefix table](https://www.urhoba.net/en/post/the-vs-code-command-palette)).

Commands to register (namespaced `category: action` like VS Code, whose guidance stresses findability
[UX guidelines](https://code.visualstudio.com/api/ux-guidelines/command-palette)):

- *Conversation*: New chat · Search chats · Rename · Export MD/JSON · Delete
- *Turn*: Stop · Retry · Retry with model… · Edit last message · Branch from here · Copy response
- *View*: Toggle sidebar · Toggle side panel · Focus composer · Scroll to latest · Detail:
  Normal/Thinking/Verbose · Expand/collapse all steps
- *Agent*: Switch model · Switch mode (Plan/Agent) · Approve/deny pending · Inspect subagents · Cancel subagent ·
  Compact context · Set permission mode
- *Files*: Open changed file… · Review changes… · Accept/Reject all in file
- *App*: Settings · Search settings… · Toggle theme · Keyboard shortcuts · Reset layout

Fuzzy match is subsequence-based (`uslst` → `UrunListesiSayfasi.dart`); rank
`recents × context-relevance × score`, cap visible rows at 8–10, and show the keybinding right-aligned on every
row — that is how a palette teaches shortcuts. Keep it virtualized; it will eventually hold hundreds of
"switch to <chat>" entries. **Discoverability without palette-only UI**: every command also exists as a visible
menu item with its shortcut, an overflow menu on each message and tool group, a `Cmd+/` cheat sheet (Claude
Desktop does this — [docs](https://code.claude.com/docs/en/desktop)), and a first-run hint. Don't bind bare
`Ctrl/Cmd+K` both globally and for a composer slash-menu — one owner per context.

---

## 16. Plan cards, subagent trees, activity centre (#193, #192, #191)

**Two objects — don't merge them.**

1. **Plan/todo** (#193): 3–8 items, `○ pending / ◐ in progress (with elapsed) / ● done / ✕ failed`, pinned to
   the top of the current turn — a *sticky child* of the turn, not a floating overlay, so it scrolls away when
   reading history. Collapse to `Plan · 5/7` on completion. Claude Code keeps this explicitly separate from
   background tasks ([interactive mode](https://code.claude.com/docs/en/interactive-mode)).
2. **Agent tree** (#192): indented rows, agent label, status dot, live elapsed, tokens; completed agents
   collapse into a `Done · 3` group with one-line result summaries — Codex's exact shape, "Model api audit 2m —
   Audit complete; no endpoint compatibility issues were found"
   ([multi-agent](https://developers.openai.com/codex/multi-agent)). Expanding a row opens *that agent's
   transcript in the side panel*, not inline; inline recursion destroys the layout. Claude Desktop's tasks pane
   shows "subagents, background shell commands, and dynamic workflows" with click-to-inspect-or-stop
   ([desktop](https://code.claude.com/docs/en/desktop)).

Data: `plan { turnId, revision, items[{itemId, text, status, startedAt, endedAt, agentId?}] }` — keep revisions
so you can show what the plan *was*. `agentRun { agentId, parentAgentId, rootTurnId, label, role, model,
status, startedAt, endedAt, summary, transcriptRef, tokenUsage, spawnedByStepId }` — `spawnedByStepId` lets the
tree also render in place at the tool call that spawned it.

Activity centre (#191) is the union of running replies, jobs/reminders (`reminders.py`, `scheduler.py`) and
pending approvals in one list — which means it needs an *event* stream, not polling. `events.py` and
`scheduler.py` already exist, so this is wiring rather than invention.

Pitfalls: status is the noisiest live region — announce terminal transitions only; never let the pinned card
change height each second (reserve rows, fixed-width timer); a failed subagent must appear in the parent header
*and* keep its child row, since users need the reason, not a count.

---

## 17. Suggested build order

The epic's own ordering is right; this is the same order with dependencies made explicit and the stack-specific
first step inserted.

1. **Resolve the accent conflict** (orange vs the current `theme.py` violet) — blocks #171, #173, #175.
2. **`scripts/build_tokens.py`** plus a DTCG `tokens.json` → `tokens.css`, `theme.py`, `tui_palette.json`;
   lint that components never alias primitives; the contrast gate in `pytest`. *(was #173)*
3. **Theme engine, data-only** + `system|light|dark` + `syntax`/`terminal` per theme (§2). *(#174)*
4. **Split the token layer out of `app.css`** — 71 KB is already past the point where "every value is a token"
   can be eyeballed; do this before the theme editor, not after.
5. **Token-sheet page + Playwright-python visual tests** per theme (part 2 §6) — land it the moment #173 lands,
   so you have a baseline *before* the redesign churns every screen. *(#209)*
6. **`docs/DESIGN.md`** (#210) written from the generated token reference, linked from `AGENTS.md` and
   `CONTRIBUTING.md`.
7. Then the epic's sections 3–5 in its stated order: layout → conversation → states. Tool groups (part 2 §9)
   and streaming/anchor (part 3 §13) are the two highest-risk pieces and both need the data-model changes
   above (per-step rows, message tree) before their UI.

Deliberately last: theme gallery (#177), custom CSS (#178), sounds (#207), backgrounds (#206) — each multiplies
whatever the token layer and the gallery's trust model are, so they are cheap after step 3 and expensive before
it.

---

## 18. What this research could not establish

Stated plainly so the doc doesn't overclaim, matching `docs/research/README.md`'s VERIFIED/UNSURE convention:

- **No vendor documents the "Worked for 34s · 7 steps" row format.** OpenAI's changelog confirms a "collapse
  turn details" feature and issues describe a single expandable "Worked for …" row; Anthropic documents
  per-call summaries and Normal/Thinking/Verbose modes, but the collapsed turn-group row is still an open
  request. The format in §9 is a synthesis, not a spec.
- **Cursor's tool-grouping UI is not first-party documented**; only forum answers describe the review panel
  behaviour.
- **ChatGPT's current branch UX** is documented only by the branching release notes plus community threads
  about the arrows being removed; there is no official "version history" help article.
- **LibreChat's `‹2/3›` visuals** are unverified; the sibling-index *model* was read from source, not from a
  design description.
- **Icon counts and bundle sizes** differ between vendor sites and third-party counts; Phosphor's ~4–5 kB per
  icon is a maintainer estimate, not a benchmark. None of it is load-bearing for choosing Lucide.
- **Motion durations/easings** are conventions; only the reduced-motion *requirement* (WCAG 2.3.3) is normative.
- **Nothing here was verified by running Kova.** Stack facts come from the repository tree and
  `pyproject.toml`; no code was executed, no screen was rendered, and none of the UI claims were checked
  against the current build.
