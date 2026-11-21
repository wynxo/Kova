# Kova design system

Read this before changing anything users look at. It is short on purpose: one page that a
human or an agent can actually follow, rather than a spec nobody opens.

Background research, with sources, lives in [docs/research/design-ui.md](research/design-ui.md).

## What Kova is, and what the design is for

Kova is a private AI that runs on your own computer. Its value is not the answer box; it is
that **you can see and steer every step**: what it thought, every tool call with its name,
arguments and output, what it is waiting for you to allow, which sub-agents it started, and
a Stop that always works. Group chats with several bots come next, so every speaker needs
its own mark and colour.

So the agent's *work* is the hero of the screen, not an empty greeting. The interface is
closer to an instrument than to a messenger: a quiet panel, and a live signal on it.

## Principles

1. **Colour is data.** The chrome (sidebar, header, message box, dialogs) is monochrome.
   Colour appears only where the agent did something, and each colour names a kind of
   step. If a colour is on screen, you can say what happened.
2. **The trace is the signature.** Every reply hangs from a vertical spine. Thinking, tool
   calls, approvals, plans, sub-agents and the answer are nodes on it, in order. The node
   that is running now is the only thing on screen that moves.
3. **Your words are instructions.** What you typed is set larger than the reply and flush
   with the spine, so a long conversation reads as a list of jobs and what was done for each.
4. **Every value is a token.** No hex codes, no magic pixel numbers. If a value is missing,
   add a token in `design/tokens.json`; don't hard-code it.
5. **Every opinion is a setting with a sensible default.** Density, motion, transcript detail
   and theme are preferences, not opinions baked into a component.
6. **Motion explains change.** The live node breathes while it runs; everything else moves
   only in answer to what you did. Motion can always be turned off.
7. **One product, four surfaces.** Web, desktop, phone and the TUI share the same semantic
   tokens. The terminal is a renderer, not a separate design.
8. **Accessible in every theme.** Contrast is measured, not eyeballed. A theme that fails is
   not shipped, including themes from the gallery.

## Design plan (2026 redesign)

### Palette

Two themes, each a monochrome panel plus four *step channels*. The channels borrow from the
probe colours of a bench oscilloscope: each kind of step is a channel with its own trace colour.

| Role | Token | Dark ("Scope") | Light ("Bench") |
|---|---|---|---|
| Panel: page | `bg.base` | Graphite `#17191C` | Chalk `#F7F8F9` |
| Panel: raised | `bg.raised` | Slate `#282C30` | Steel `#E4E7EA` |
| Ink | `fg.default` | Signal white `#E6E9EC` | Ink `#15181B` |
| Channel: thinking | `step.think` | Lilac `#B8A6FF` | Violet `#5B41CF` |
| Channel: tool call | `step.tool` | Probe cyan `#4CCFDF` | Teal `#00707E` |
| Channel: needs you | `step.ask` | Amber `#F3C14B` | Ochre `#875A00` |
| Channel: sub-agent | `step.agent` | Magenta `#F27FC2` | Plum `#AD2370` |

The accent (`accent.*`) stays monochrome — signal white on dark, ink on light — and means
focus, selection and the one primary button. Status colours (`status.*`) mean outcome
(done, failed, warning) and never stand in for a channel.

### Type

- **Geologica** (OFL, variable 100–900, Latin + Cyrillic) for everything people read: the
  interface, replies and the start screen. A grotesk with sharp terminals and an open
  aperture; it holds up at 12px in the trace and has character at 36px.
- **Martian Mono** (OFL, variable width and weight, Latin + Cyrillic) only for literal
  machine text: commands, paths, tool arguments and output, code. Set semi-condensed
  (`font-stretch: 87.5%`) so a command line fits the trace. Never for timestamps, labels or
  decoration.
- Both are bundled under `web_assets/fonts/`; nothing is fetched (Kova works offline).

Scale (Bringhurst's classic steps): 12 · 14 · 16 · 18 · 21 · 24 · 36.

| Token | Size | Use |
|---|---|---|
| `--type-size-label` | 12 | durations, captions, trace metadata |
| `--type-size-body` | 14 | interface text, trace rows |
| `--type-size-body-comfortable` | 16 | replies, the message box |
| `--type-size-title` | 18 | dialog titles |
| `--type-size-lead` | 21 | your instruction at the head of a turn |
| `--type-size-heading` | 24 | settings sections |
| `--type-size-display` | 36 | the start screen statement |

Weights: 400 body, 560 labels and tool names, 680 display. Display text tracks at −0.02em.
Sentence case everywhere; no all-caps labels.

### Layout

Left-aligned throughout. The conversation is a single reading column (`--column`), and
inside it everything aligns to one vertical line: the spine.

Desktop (1440):

```
+-----------------+--------------------------------------------------------------+
| [K] Kova        |  Why are the tests slow?                           (1)   ... |
|                 |                                                              |
| [+] New chat    |  Why are the tests slow?                <- your instruction  |
| [/] Search      |  [K] Kova                                                    |
|                 |   |                                                          |
| Today           |   <> Thought for 2s                      <- lilac node       |
| | Why are the.. |   o  Read 3 files            0.2s        <- cyan node        |
| | Plan the tr.. |   #  Plan  2 of 3                                            |
|                 |   *  The slow part is one file ...       <- the answer       |
|                 |                                                              |
|                 |  Clear the cache and run them again                          |
|                 |  [K] Kova                                                    |
|                 |   o  Run command  rm -rf .pytest_cache   (running)           |
|                 |   !  Run this command?                   <- amber, waits     |
|                 |      rm -rf .pytest_cache build && ...                       |
|                 |      [Allow once] [Allow for session] [Deny]                 |
| [=] Settings    |  +--------------------------------------------------------+  |
|                 |  | +  Message Kova                       local-model v [^]|  |
+-----------------+--------------------------------------------------------------+
```

Start screen: no greeting and no centred question. It states what Kova is, then shows the
four channels as a short legend you can read once and recognise forever, then the box.

```
   [K]
   Kova runs on this computer.                          <- display, 36px
   You see every step it takes, and you can stop it
   at any point.

    |  <> Thinks out loud      Open any thought to read what it weighed up.
    |  o  Uses tools           Every file, command and search, with its output.
    |  !  Asks first           You decide which actions wait for your OK.
    |  #  Splits up big jobs   Parallel tasks show as a tree; stop any one.

   Plan a small project with me    Summarise a file I send   (your suggestions)
   +-----------------------------------------------------------+
   | +  Message Kova                          local-model v [^]|
   +-----------------------------------------------------------+
```

Phone (390): the same column, full width; the sidebar is a drawer; the spine moves to the
left edge with a 24px gutter, and approval buttons stack full width.

### The signature: the live step trace

- A 2px spine (`--border-subtle`) runs down the left of every reply, from the speaker's mark.
- Each step is a node on the spine, shaped and coloured by kind: thinking is a lilac diamond,
  a tool call a cyan ring, a request for approval an amber triangle, a sub-agent group a
  magenta square, the answer a solid ink dot, a plan a hollow ink square.
- A node that is running breathes (opacity only). Nothing else on the page animates by itself.
- An approval that waits for you turns its stretch of spine amber, so a long reply shows where
  it stopped from across the room.
- In a group chat each bot's trace hangs from that bot's own mark and colour (`--speaker`).

### Review against generic defaults

The first draft had a centred hero, one accent colour and the existing Inter/JetBrains Mono
pairing: the template every chat app ships. Changes made after review:

- *Centred greeting and suggestion chips* were replaced by a left-aligned statement and a
  legend of the four step channels, because the start screen should teach how to read the
  trace, not ask a question.
- *One accent on near-black* became a monochrome panel with four channel colours that each
  mean a kind of step; no colour is decorative.
- *Inter + JetBrains Mono* (the most common pairing) became Geologica + Martian Mono, both
  with Cyrillic, because many people write in more than one script.
- *Rounded cards for every tool call* became rows on the spine; only the approval keeps a
  card, because it is the one step that needs a decision.
- No all-caps eyebrows, no middot-joined meta strings, no arrows appended to buttons, no
  gradients or glows. Monospace is used only for text a machine would read.

## Tokens

Source of truth: [`design/tokens.json`](../design/tokens.json). Generated output:
[`src/kova/web_assets/tokens.css`](../src/kova/web_assets/tokens.css) — do not edit the generated
file. That directory is what `src/kova/web.py` mounts at `/app/assets`, and
`src/kova/web_assets/index.html` links the stylesheet before `app.css`; the order matters, because
`app.css`'s shipped palette wins the cascade for the two names they share.

```bash
python scripts/build_tokens.py           # regenerate tokens.css and tokens.json
python scripts/build_tokens.py --check   # contrast + shape + freshness, exits 1 on failure
```

Both generated files are checked for freshness: `src/kova/web_assets/tokens.css` for the
browser, and `src/kova/tokens.json` for the server, which needs the same values as data so a
user theme can be merged over its base ([`src/kova/themes.py`](../src/kova/themes.py)).

Three layers, and components may only use the third:

| Layer | Example | Rule |
|---|---|---|
| Primitive | `primitive.orange.500` | Raw ramps. Never referenced by a component. |
| Semantic | `semantic."kova-dark".accent.default` | The only layer components may use. |
| Component | `--chip-accent-bg` | Optional, may only alias a semantic token. |

In CSS the semantic layer is resolved to theme-free custom properties, so a component says
`var(--fg-default)` and never learns which theme is active:

```css
.panel {
  background: var(--bg-surface);
  color: var(--fg-default);
  border: 1px solid var(--border-subtle);
  border-radius: var(--radius-md);
  padding: var(--space-4);
  transition: background-color var(--motion-fast) var(--motion-ease-standard);
}
```

### Which token when

| Need | Token |
|---|---|
| Page background | `--bg-base` |
| Panel, sidebar, card | `--bg-surface` |
| Chip, hovered row, inset | `--bg-raised` |
| Body text | `--fg-default` |
| Timings, captions, metadata | `--fg-muted` |
| Separators, disabled text | `--fg-faint` |
| Text on a solid accent chip | `--accent-on` |
| Activity, focus, selection | `--accent-default` |
| Tinted accent background | `--accent-soft` |
| Decorative divider | `--border-subtle` |
| Input border, focus ring, control boundary | `--border-strong` |
| Error / success / warning text | `--status-danger` / `--status-success` / `--status-warning` |
| A step on the trace: thinking, tool call, needs you, sub-agent | `--step-think` / `--step-tool` / `--step-ask` / `--step-agent` |

`--border-subtle` is decorative and deliberately low-contrast. Anything a user must be able to
*find* (an input, a focus ring, a selected control) uses `--border-strong`, which is held to the
3:1 non-text contrast minimum.

### Type, space, shape, motion

- Type scale: 12 / 14 / 16 / 18 / 21 / 24 / 36px. Body is 14px, replies 16px. Never below 12px.
- Reading measure: `--type-measure-prose` (66ch) for prose, `--type-measure-chat` (80ch) for chat.
- Spacing is a 4px grid: `--space-1` … `--space-12`. Use `--space-0-5` for hairlines.
- Radii: `--radius-sm` (controls), `--radius-md` (panels), `--radius-lg` (sheets), `--radius-pill`.
- Motion: `--motion-fast` (120ms) for hover/press, `--motion-base` (180ms) for popovers,
  `--motion-slow` (280ms) for view changes. `--motion-instant` for state flips with no travel.
- Text size, density, font, content width and avatars are settings, not component opinions:
  `data-text-size`, `data-density`, `data-font`, `data-chat-width` and `data-avatars` on
  `<html>` swap these tokens, or the shared `--column`, one step up or down.
  `default` (and `inter`, `normal`, `on`) writes no override, so it is the scale above.
- Message style and timestamps are set separately via `data-message-style` and
  `data-message-timestamps`. Chat width, message style and avatars may follow the active
  theme's `tokens.chat` defaults instead of a saved choice.
- Motion is a setting too: Full, Reduced or Off overrides the duration tokens under
  `data-motion` on `<html>`.
- A theme background is drawn by one fixed layer under the shell. Panels may use an 88%
  semantic surface plus backdrop blur while it is active, and must fall back to the
  opaque `--bg-surface` / `--bg-base` under `prefers-reduced-transparency`, motion off,
  forced colours, or no `backdrop-filter`. The **Show theme backgrounds** setting turns
  the layer off without changing the theme.

## Components

**Choose the surface by what it does, not by how big it is.**

| Surface | Use for | Never for |
|---|---|---|
| Side panel | Content read *alongside* the conversation: diffs, artifacts, the subagent tree, the plan card | Anything that blocks the reply |
| Dialog | Something that must be resolved first: an approval for a destructive command, an unsaved-work warning | Settings, browsing, anything with a form inside a form |
| Sheet | The dialog's content on a phone | The desktop version of a dialog |
| Popover | A few related, transient actions on one object | Multi-step flows |
| Toast | Confirmation of something that already happened, with an undo | Errors that need a decision — those are dialogs |

**Buttons.** One primary action per view. Secondary actions are quiet: text or outline, never a
second filled accent. Destructive actions name the thing they destroy (*Delete chat*, not *OK*).

**The plan card.** While a turn runs, its latest checklist stays pinned above the message box as
one compact row: the current step, a progress bar and a disclosure for the whole list. It is fed
by the same events as the transcript plan, takes that plan's place while pinned, and hands it
back when the turn ends, so the two are never on screen together. It can be dismissed for the
turn, and on a phone the disclosure opens as a bottom sheet (#193).

**Copy.** Sentence case. Verbs for actions. No exclamation marks. Errors say what happened and
what to do next, in that order, without apologising: *The model server stopped responding. Start it
again, or switch to a local model.*

**States.** Every list needs empty, loading and error states that look designed. An empty state
offers the action that fills it.

## Looks vibe-coded — checklist

Before you call a screen done, check it is not doing any of these:

- [ ] Random gradients, glows or coloured shadows
- [ ] A colour that does not name a step channel or a status, or a channel colour used for decoration
- [ ] Mixed corner radii (a 4px chip next to a 16px card with no rule behind it)
- [ ] Emoji standing in for an icon
- [ ] Everything centred; nothing with a clear reading order
- [ ] Shadows on every surface
- [ ] Uneven spacing that is not on the 4px grid
- [ ] Hard-coded hex, `px` font sizes, or a colour that exists in no token
- [ ] Text below 12px, or a line length past 80ch in prose
- [ ] Motion that plays on load, loops, or cannot be turned off
- [ ] A hover state that only changes colour and nothing else

## The app mark

The mark is a rounded tile with a solid K. Its geometry lives in one place,
[`scripts/build_icons.py`](../scripts/build_icons.py), which writes every SVG and PNG
(favicon, PWA, Apple touch, Electron, installers). Change the shapes there and re-run it;
never edit the icon files by hand.

- **Inside the UI**, use `<span class="kova-mark" aria-hidden="true">`. It paints
  `mark.svg` (the tile with the K cut out) in `--fg-default`, so the letter shows the
  surface behind it and the mark follows every theme.
- **Outside the UI** (favicon, OS icons, notifications) the static `icon.svg` and PNGs use
  a near-black tile `#14121c` with a white K, because no theme reaches there.
- The Electron tray icon carries the same mark plus a small status dot in the lower-right:
  white while working, amber (`#ffaf5f`) while waiting for approval, none while idle. The
  dot is composited over the mark at build time
  ([`scripts/build_icons.py`](../scripts/build_icons.py); `DOT_STATUS`), so it is a fixed
  colour like the mark itself and the tray reflects state without opening Kova.
- It must read at 16px: no thin strokes, no circles, square ends. Check the 16px render
  before changing it.

## Adding a theme

A theme is **data, never code**: no CSS files, no JavaScript, no URLs. The shape is in
[`design/theme.schema.json`](../design/theme.schema.json) and the flow is documented in
[docs/THEMES.md](THEMES.md).

Minimum viable theme: `id`, `name`, `version`, `appearance`, `extends`, and overrides for the
semantic tokens you want to change. A theme that inherits `kova-dark` and changes only
`accent.default` is a valid, useful theme.

## Accessibility

- Text: 4.5:1 against its background. Large text and UI boundaries: 3:1.
- Every theme must pass `python scripts/build_tokens.py --check` before it ships.
- Reduced motion is honoured: a device that asks for it resolves Full to Reduced, and the
  Motion setting's Reduced or Off always wins. `data-motion` on `<html>` is the one switch.
- Reduced transparency is honoured: theme backgrounds stay behind panels at no more than
  12% translucency, and the solid measured surfaces return under
  `prefers-reduced-transparency` or motion off.
- Focus is always visible and never removed without a replacement.
- Streaming text must not live in an `aria-live` region; announce phase changes once instead.

## Before opening a UI pull request

- `uv run pytest -q` passes (includes the token guard-rails in `tests/test_tokens_contrast.py`).
- `python scripts/build_tokens.py --check` passes.
- `python scripts/privacy_check.py` passes.
- No hard-coded colour, font size or spacing outside `design/tokens.json`.
- New interactive elements are reachable and operable by keyboard.
- Screenshots in PRs use a throwaway `KOVA_HOME`, a neutral prompt and a stub model — see
  [AGENTS.md](../AGENTS.md).

### One colour layer

All served component stylesheets consume the semantic roles above, including
syntax highlighting. Do not redeclare these roles in component CSS or add
literal colours, legacy palette aliases, theme-specific component selectors, or
fallback palettes. Subtle state fills use `color-mix` with a semantic status
role and `transparent`; shadows derive from `--bg-overlay`. `tokens.css` is the
only generated palette. `tests/test_semantic_css.py` guards this boundary.
