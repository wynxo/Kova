# Part 2 — visual testing, accessibility, icons, conversation UI

Covers #209, #201, #180, #186, #187, #190, #188, #192.

---

## 6. Visual regression per theme (#209)

Playwright is the right engine, and **use its Python bindings** — `pytest-playwright`, not a Node toolchain,
so it slots into the existing `pytest` setup (`pyproject.toml` already sets asyncio mode and
`testpaths = ["tests"]`). `toHaveScreenshot()` ships capture, diff, retry-until-stable and an HTML report,
and "will wait until two consecutive page screenshots yield the same result"
([PageAssertions](https://playwright.dev/docs/api/class-pageassertions)).

The stability rules are not optional (Playwright documents each):

- Rendering varies by host OS, browser version, hardware and headless mode — **baselines must be produced in
  the same environment** as CI, and snapshot names are namespaced by browser+platform
  ([visual comparisons](https://playwright.dev/docs/test-snapshots)). Pin a container.
- Bundle fonts; `await document.fonts.ready` before capture; no network fonts.
- `animations: 'disabled'`, `caret: 'hide'`, move the mouse off-canvas to kill hover states.
- `stylePath=` to blank volatile regions (timestamps, spinners, token counters).
- **Thresholds that don't flake**: keep `threshold` at the `0.2` default and gate on *pixel count* —
  `maxDiffPixels: 0` for the token sheet (you *want* a hard fail when a token moves), 20–50 for composite
  screens. Avoid `maxDiffPixelRatio` on small elements: 0.1% of a 40px icon is zero pixels.

Matrix: one project per theme (`dark`, `light`, `contrast`) setting `data-theme` plus a `localStorage` seed,
at 3 widths — exactly what #209 asks for — against a stub model with fixed data and a throwaway
`KOVA_HOME`, which also satisfies the repo's privacy rules (`.privacy-allow` exists for a reason).

**Add one screen the issue doesn't mention: a token sheet.** A page rendering *every* semantic token as a
swatch + name + computed contrast ratio, per theme. It catches a bad colour the day it is introduced,
before any component happens to use it.

Alternatives, briefly: Chromatic has the best review UX and theme modes but is cloud-only
([visual tests](https://www.chromatic.com/docs/visual/)) — wrong for a privacy-first local app. Loki is
Storybook-specific ([loki.js.org](https://loki.js.org/)) and Kova has no Storybook. BackstopJS is MIT but its
README is asking for a new maintainer; don't build a programme on it.

**Browser tests vs the service worker:** `sw.js` will happily serve stale CSS to a screenshot run. Use a
fresh context per project, or bypass the SW in tests. Not documented anywhere — it is a consequence of being
a PWA.

---

## 7. Accessibility per theme (#201, and the epic's "accessible in every theme" principle)

Two layers, because they catch different bugs.

**Layer 1 — token-pair arithmetic in `pytest`. Sub-second, no browser, and it is the real gate.**
WCAG 2.2 SC 1.4.3 wants **4.5:1** for normal text and **3:1** for large text (≥24px, or ≥18.5px bold);
SC 1.4.11 wants **3:1** for UI boundaries and meaningful graphics; the ratio is `(L1+0.05)/(L2+0.05)`
([WCAG 2.2](https://www.w3.org/TR/WCAG22/),
[Understanding 1.4.3](https://www.w3.org/WAI/WCAG22/Understanding/contrast-minimum),
[1.4.11](https://www.w3.org/WAI/WCAG22/Understanding/non-text-contrast)).

Implement it against an **explicit pair manifest** (`fg-default on bg-surface: 4.5`,
`border-subtle on bg-panel: 3`), not a brute-force cross product — the cross product drowns you in pairs
that never co-occur. ~40 lines of pure Python (sRGB → linear → relative luminance), no dependency. Run it
over every shipped theme *and* every gallery submission; a theme that fails contrast does not get published.
Allow `# allow-fail: <reason>` so audits stay honest instead of being deleted.

**Layer 2 — axe-core in a real browser, per theme.** Vendor `axe.min.js` into `tests/` and inject it via
Playwright. Assert `violations` is empty, but also **surface `incomplete`**: axe reports gradients,
background images, semi-transparent and 1:1 cases as *incomplete*, not violations, so a violations-only
check silently passes on exactly the pretty background work #206 adds
([Deque color-contrast rule](https://dequeuniversity.com/rules/axe/4.6/color-contrast)). Run against a
production-ish static serve rather than the dev server, and against representative screens only — Deque's own
advice, since styles are shared ([axe API docs](https://www.deque.com/axe/core-documentation/api-documentation)).
Neither layer is a conformance claim.

**APCA vs WCAG — the correct split.** Gate the build on **WCAG 2.x** (the normative, legally-referenced
standard); *report* APCA alongside as advisory, because APCA's own critique is that WCAG 2.x overstates
contrast for dark colours ([APCA in a Nutshell](https://git.apcacontrast.com/documentation/APCA_in_a_Nutshell.html)).
WCAG 3's contrast algorithm is still explicitly undetermined ([WCAG 3 draft](https://www.w3.org/TR/wcag-3.0/)),
so don't gate on it. APCA is directional and size/weight dependent (≈Lc 75 body, 60 large, 45 UI, 30
non-text as practical defaults) — never one global number.

**Also test forced-colors / high-contrast mode** (`@media (forced-colors: active)` plus `<system-color>`
keywords), or the theme engine will paint over Windows HCM
([MDN](https://developer.mozilla.org/en-US/docs/Web/CSS/Reference/At-rules/@media/forced-colors)).

---

## 8. Icons (#180): Lucide, vendored as one sprite

| Library | License | Grid/stroke | Notes |
|---|---|---|---|
| **Lucide** | **ISC** ([license](https://lucide.dev/license)) | 24×24, 2px | Ecosystem default (shadcn/ui); one icon = one module; tree-shakeable ([lucide.dev](https://lucide.dev/)) |
| **Tabler** | **MIT**, no attribution | 24×24, 2px | 6,000+ icons, outline + filled, SVG sprite output ([tabler.io](https://tabler.io/icons)) |
| **Phosphor** | MIT | 256 viewBox, 6 weights | Duotone/filled in one family, but each import bundles all six weights (~4–5 kB/icon, maintainer estimate) ([discussion](https://github.com/orgs/phosphor-icons/discussions/581)) |
| **Iconoir** | MIT | 24×24, **1.5px** | 1,600+; lighter aesthetic; clashes with Lucide's 2px unless normalised |
| **Remix** | ⚠️ custom license since Jan 2026 | 24×24 | Forbids competing icon packs; npm metadata still says Apache-2.0 ([license](https://github.com/Remix-Design/RemixIcon/blob/master/License)). Avoid for a gallery/marketplace product |

**Recommendation: Lucide**, pulled once and committed as a single `<symbol>` sprite at
`web_assets/vendor/icons.svg` — no bundler needed, one HTTP request, `currentColor` so icons inherit theme
tokens for free, and `--icon-stroke` as a token so 16px and 24px stay optically consistent. Mix Tabler in only
for filled/active states (MIT, same 24/2px geometry). Cheap to add icons to a sprite; expensive to change
family later.

---

## 9. Grouped tool calls (#186): what the leaders actually ship

Ground truth, because the exact vocabulary in #186 is not documented by any vendor:

- **Claude Code** collapses each tool call to a summary (command + output line count); `Ctrl+O` opens a
  transcript view with timestamps and model per message, and expands things collapsed by default such as MCP
  calls shown as one `Called slack 3 times` line
  ([interactive mode](https://code.claude.com/docs/en/interactive-mode),
  [settings](https://code.claude.com/docs/en/settings-reference)).
- **Claude Desktop** ships **Normal / Thinking / Verbose** modes: Normal = "Tool calls collapsed into
  summaries, with full text responses" ([desktop](https://code.claude.com/docs/en/desktop)).
- **Codex Desktop** groups activity behind one expandable **"Worked for …"** row; the CLI prints each entry
  inline — a divergence users report as a bug ([#18396](https://github.com/openai/codex/issues/18396)). Codex
  CLI auto-groups consecutive commands into `Ran N commands · ctrl+t to view transcript`
  ([#39903](https://github.com/openai/codex/issues/39903)) and later added an opt-in "Collapse turn details"
  ([changelog](https://developers.openai.com/codex/changelog)).
- The literal format appears first in *feature requests*: `Worked for 4m 12s · 23 tool calls`, "only the final
  reply should stay expanded" ([claude-code #96497](https://github.com/anthropics/claude-code/issues/96497)),
  preserving `User request > Worked for 2m 15s > Final answer` in order
  ([JetBrains LLM-30970](https://youtrack.jetbrains.com/issue/LLM-30970)).

**So #186's design is yours to define.** The shape the evidence supports: a turn group above the final answer —
header = status glyph + `Worked for {dur} · {n} steps` + chevron; expanded = ordered rows
`[icon] Tool  summary…  dur`, monospace for commands, truncated to 2–3 lines with "Show all (412 lines)" and
copy. Auto-expand while running; auto-collapse on completion **only if the user never opened it**. Keep
running, awaiting-approval and failed steps visible even when collapsed — Codex's explicit lesson.

**Store per step, not per frame:** `stepId, turnId, parentStepId,
kind(tool_call|tool_result|reasoning|assistant_text|approval|error|subagent), toolName,
titleSummary(≤120ch, pre-rendered), argsJson,
status(queued|running|ok|error|cancelled|awaiting_approval|skipped), startedAt, endedAt, durationMs,
outputRef(blob+bytes+lineCount+truncatedAt), exitCode, error, model, tokensIn/out, retryOfStepId`.
Derive turn aggregates (`stepCount`, `totalDurationMs`, worst status) — never store them, they drift. Store
`turnStartedAt/turnFinishedAt` because wall-clock duration must survive reload and compaction. `store.py` is
SQLite; a content-addressed `outputRef` with a byte cap is what keeps a 4,000-line `pytest` run out of the
transcript row.

**Accessibility:** disclosure pattern for one step (`aria-expanded` on a real `<button>`), accordion semantics
across siblings ([APG disclosure](https://www.w3.org/WAI/ARIA/apg/patterns/disclosure/),
[accordion](https://www.w3.org/WAI/ARIA/apg/patterns/accordion)). **Never** put `aria-live` on the step list
while it streams; drive one separate `role="status"` node with phase changes only ("Running tests…",
"Finished: 7 steps, 1 failed") ([ARIA22](https://www.w3.org/WAI/WCAG22/Techniques/aria/ARIA22),
[WCAG 4.1.3](https://www.w3.org/WAI/WCAG22/Understanding/status-messages.html)) — the spec itself warns live
regions make an app "too chatty".

**Known bug to design out:** a block the user expanded re-collapsing on re-render. Filed against Claude Desktop
([#105282](https://github.com/anthropics/claude-code/issues/105282)); it is entirely a client state-persistence
issue. Key expansion state by `stepId` and persist it per conversation.

---

## 10. Reasoning display (#187): three capabilities, one component

Providers differ fundamentally, so branch on capability, not provider name:

- **Anthropic** streams `thinking_delta` per block, then a `signature_delta`; with `display: "omitted"` no
  thinking deltas are sent at all, which they recommend when you don't surface reasoning (faster
  time-to-first-text-token). Thinking blocks must be passed back unmodified or the API 400s
  ([streaming](https://docs.anthropic.com/en/docs/build-with-claude/streaming),
  [thinking](https://platform.claude.com/docs/en/build-with-claude/thinking)).
- **OpenAI Responses** exposes reasoning as *summary parts*
  (`response.reasoning_summary_part.added/.done`)
  ([streaming events](https://developers.openai.com/api/reference/resources/responses/streaming-events)).
- **Local models** (llama.cpp, Ollama — `local_models.py`) usually expose nothing. Codex App shows only a
  blinking "Thinking" state and no summary, filed as a gap
  ([#27197](https://github.com/openai/codex/issues/27197)). Don't do that: render elapsed time, and never
  render an empty expander.

States: `none` → no row (or elapsed-only, non-interactive); `pending` → "Thinking…" + elapsed; `streaming` →
collapse to "Thought for 12s" once the first *answer* token arrives; `done` → static label, expandable.
Claude Code's collapsed preview stays readable ≥3s, renders as markdown, caps at 10 lines
([#62672](https://github.com/anthropics/claude-code/issues/62672)).

Pitfalls with concrete fixes:

- **Jitter**: drive the timer from a 1 Hz interval and *freeze the string* at completion, or it oscillates
  11s/12s between renders.
- **Screen-reader spam**: timer and streaming reasoning must sit outside any live region — an `aria-live` on
  the token node "makes the screen reader stutter through every partial word"
  ([accessibility.build](https://accessibility.build/guides/accessible-ai-chat),
  [azukiazusa](https://azukiazusa.dev/en/blog/accessible-streaming-chat-ui)).
- **Scroll jump**: reasoning above the answer pushes the answer down — cap its height or collapse before the
  answer grows (part 3 §13).
- **Honesty**: Claude Desktop only lists "Thinking" mode "after Claude has produced thinking in the session
  you're viewing". Do the same rather than showing a dead toggle.

Store `reasoning { blockId, kind: summary|full|redacted, textRef, signature?, startedAt, endedAt, displayable }`
separately from answer text — so you can replay the signature, switch views without refetching, and strip it
from exports.

---

## 11. Diffs (#190): generate them in Python, render hunks in JS

This is where the stack changes the recommendation. Every JS diff library wants either a bundler or a big
runtime, and **Kova already has the best diff engine available: `difflib` / `git diff` on the server**.

- Generate the patch server-side (`difflib.unified_diff`, or `git diff` for tracked files), and store
  `editOp { id, turnId, stepId, path, baseHash, resultHash, baseContentRef, resultContentRef, patch,
  hunks: [{hunkId, header, oldStart, newStart, decision: pending|accepted|rejected, appliedAt}],
  appliedState: pending|partial|applied|reverted }`.
- Render **unified in the chat column**, and switch to split only when the side panel is wide enough. One
  summary row per file (`path +12 −3` + status) that lazily renders on expand.
- **Per-hunk accept/reject is a data problem before it is a UI problem.** Hunks need IDs independent of line
  numbers, and accept must apply to the *current* file, not line numbers captured 30s ago — partial
  application changes every subsequent offset. Recompute the remaining hunks after each accept; never cache
  positions.
- **Staleness check**: `baseHash !== currentHash` → disable accept and offer re-read. Keep `baseContentRef`
  after apply — it is the only way to implement "undo this edit" and #52 (checkpoints).
- Virtualize *inside* a very long hunk, never by rendering 4,000 rows.

Prior art if you would rather buy than build: `@codemirror/merge` has `mergeControls` for per-chunk
accept/reject ([ref](https://codemirror.net/docs/ref)); Pierre `@pierre/diffs` is virtualization-first with a
`diffAcceptRejectHunk` utility ([docs](https://diffs.com/docs)) — both need a bundler, which is why the
server-side path wins here. Its author's writeup on why browser scroll anchoring breaks under virtualization
is worth reading regardless ([On Rendering Diffs](https://pierre.computer/writing/on-rendering-diffs)).

---

## 12. Message actions, branching, retry (#188): a parent-pointer tree

**The consensus model is a tree, not an array.** Every message has `parentId`; the visible conversation is the
path from the active leaf to the root; switching branches means switching the active leaf
([Trigger.dev](https://www.trigger.dev/docs/ai-chat/patterns/branching-conversations),
[Ably](https://ably.com/docs/ai-transport/features/branching)).

Schemas worth copying:

- **Open WebUI**: `{id, parentId, childrenIds, role, content, model, done, timestamp}` with
  `history.currentId` pointing at the active branch
  ([import/export](https://docs.openwebui.com/features/chat-conversations/data-controls/import-export)).
- **LibreChat**: flat rows with `parentMessageId`; `buildTree` computes `depth` and **`siblingIndex`** per
  parent, and the UI resolves the displayed sibling positionally — with a subtle rule: an **appended** newest
  child is followed, but a re-keyed row (streaming id hydrating to a durable id) must **not** be followed,
  "or it would yank a user who paged away mid-stream"
  ([messages.ts](https://raw.githubusercontent.com/danny-avila/LibreChat/6d499ba3ce17f906a7762429c61018f230ecd64e/packages/data-provider/src/messages.ts),
  [MultiMessage.tsx](https://raw.githubusercontent.com/danny-avila/LibreChat/6d499ba3ce17f906a7762429c61018f230ecd64e/client/src/components/Chat/Messages/MultiMessage.tsx)).

**UX mapping for #188:** "edit and resend" creates a sibling **user** node; "retry" adds a sibling
**assistant** node; "branch from here" copies the path into a new chat and leaves the original untouched. The
`‹ 2 / 3 ›` switcher operates on a *sibling set*, with ←/→ when focused. Store
`branchMemory: Record<parentId, siblingIndex>` — a per-fork selection, not one global "current branch", which
is the fix LibreChat needed.

**The strongest argument for a DB constraint:** LibreChat shipped a bug where a `_`-suffixed streaming id
became an orphaned `parentId`, permanently hiding 14 subsequent messages
([#13611](https://github.com/danny-avila/LibreChat/issues/13611)). Kova is SQLite — put
`FOREIGN KEY(parentId) REFERENCES messages(id)` on it and hydrate streaming ids transactionally.

**Don't copy ChatGPT's regression.** They replaced in-place version arrows with "version history" +
branch-in-new-chat in Sept 2025 ([release notes](https://help.openai.com/en/articles/6825453-chatgpt-how-to-use-chatgpt));
users immediately asked for the arrows back, noting temporary chats lost the ability entirely and branches
became "a nightmare to organize"
([thread](https://community.openai.com/t/restore-the-ability-to-use-version-arrows-in-chatgpt/1394714)). Ship
both, and keep in-place switching as the default.
