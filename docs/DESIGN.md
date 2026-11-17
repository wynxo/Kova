# Kova design system

Read this before changing anything users look at. It is short on purpose: one page that a
human or an agent can actually follow, rather than a spec nobody opens.

Background research, with sources, lives in [docs/research/design-ui.md](research/design-ui.md).

## Principles

1. **Calm and monochrome.** Surfaces, text and borders are neutral. There is exactly one accent.
2. **The accent means activity.** Orange is not decoration: it marks what Kova is doing right now —
   the running spinner, the streaming caret, the active step, an unread result. If nothing is
   happening, no accent is on screen.
3. **Every value is a token.** No hex codes, no magic pixel numbers, no `12px` written inline. If a
   value is missing, add a token in `design/tokens.json`; don't hard-code it.
4. **Every opinion is a setting with a sensible default.** Density, motion, transcript detail and
   theme are preferences, not opinions baked into a component.
5. **Motion explains change.** Animation shows where something came from or went. It never
   decorates, never loops for attention, and can always be turned off.
6. **One product, four surfaces.** Web, desktop, phone and the TUI share one look and the same
   semantic tokens. The terminal is a renderer, not a separate design.
7. **Accessible in every theme.** Contrast is measured, not eyeballed. A theme that fails is not
   shipped — including themes from the gallery.

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

`--border-subtle` is decorative and deliberately low-contrast. Anything a user must be able to
*find* (an input, a focus ring, a selected control) uses `--border-strong`, which is held to the
3:1 non-text contrast minimum.

### Type, space, shape, motion

- Type scale: 12 / 14 / 16 / 18 / 24px. Body is 14px compact, 16px comfortable. Never below 12px.
- Reading measure: `--type-measure-prose` (66ch) for prose, `--type-measure-chat` (80ch) for chat.
- Spacing is a 4px grid: `--space-1` … `--space-12`. Use `--space-0-5` for hairlines.
- Radii: `--radius-sm` (controls), `--radius-md` (panels), `--radius-lg` (sheets), `--radius-pill`.
- Motion: `--motion-fast` (120ms) for hover/press, `--motion-base` (180ms) for popovers,
  `--motion-slow` (280ms) for view changes. `--motion-instant` for state flips with no travel.

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

**Copy.** Sentence case. Verbs for actions. No exclamation marks. Errors say what happened and
what to do next, in that order, without apologising: *The model server stopped responding. Start it
again, or switch to a local model.*

**States.** Every list needs empty, loading and error states that look designed. An empty state
offers the action that fills it.

## Looks vibe-coded — checklist

Before you call a screen done, check it is not doing any of these:

- [ ] Random gradients, glows or coloured shadows
- [ ] More than one accent colour on screen, or accent used for decoration
- [ ] Mixed corner radii (a 4px chip next to a 16px card with no rule behind it)
- [ ] Emoji standing in for an icon
- [ ] Everything centred; nothing with a clear reading order
- [ ] Shadows on every surface
- [ ] Uneven spacing that is not on the 4px grid
- [ ] Hard-coded hex, `px` font sizes, or a colour that exists in no token
- [ ] Text below 12px, or a line length past 80ch in prose
- [ ] Motion that plays on load, loops, or cannot be turned off
- [ ] A hover state that only changes colour and nothing else

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
- Reduced motion is honoured, and `data-motion="reduced"` overrides it in both directions.
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
