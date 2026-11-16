# Part 1 — tokens, themes, TUI parity, motion, typography

Covers #173, #174, #175, #176, #177, #178, #179, #180, #181, #211.

---

## 1. Tokens: one JSON file, four generated outputs, no build system

**Do not add Style Dictionary or Tokens Studio.** You generate *four* outputs from one token set —
web CSS vars, `theme.py`, TUI ANSI palettes, gallery export files — and the reference-resolution
logic you need is ~150 lines of Python. Style Dictionary's docs say the current spec is not fully
supported yet ([DTCG status](https://styledictionary.com/info/dtcg)), and Tokens Studio's theme and
colour-modifier features are paid ([pricing](https://tokens.studio/pricing)). A `scripts/build_tokens.py`
you own beats a version matrix.

**Use the W3C DTCG format as the on-disk shape** — it reached its first stable version, **2025.10**
([announcement](https://www.w3.org/community/design-tokens/2025/10/28/design-tokens-specification-reaches-first-stable-version),
[Format Module](https://www.w3.org/community/reports/design-tokens/CG-FINAL-format-20251028)). What matters:

- Tokens are `$value` / `$type` / `$description`; groups are plain objects ([Format §5–6](https://w3c.github.io/cg-reports/design-tokens/CG-FINAL-format-20251028)).
- Aliases are whole-token only: `"$value": "{color.palette.black}"` ([aliases](https://github.com/design-tokens/community-group/blob/main/technical-reports/format/aliases.md)).
- `$extensions` is the sanctioned place for vendor data and tools **must preserve** it ([§5.2.3](https://www.w3.org/community/reports/design-tokens/CG-FINAL-format-20251028)) — your hook for
  `"com.kova.terminalRole": "accent"` or a TUI glyph name.
- Composition without file duplication is in the stable spec: the **Resolver** module joins *sets* +
  *modifiers*, last declaration wins ([Resolver §4–6](https://www.designtokens.org/TR/2025.10/resolver)).

**Three layers; components may only reference the semantic layer:**

```
primitive.orange.600     # raw ramp — never referenced by a component
semantic.fg.default      # alias → primitive
semantic.accent          # the ONE accent (#214 principle 1)
component.chip.accent.bg # alias → semantic only
```

Naming rules that actually bite: tokens are **case sensitive**, `.` is reserved for grouping, `{ } $` are
illegal in names ([specs](https://docs.tokens.studio/manage-tokens/token-names/technical-specs)). Add a lint
to the build script that fails when `component.*` aliases a `primitive.*` — that one check is what keeps
"every value is a token" honest, and it is cheap.

**Generate CSS custom properties, but only *semantic* ones.** web.dev is explicit: components say
`var(--text-primary)`, never `var(--text-primary-light)` — all theme pivoting happens one level up
([web.dev colour scheme](https://web.dev/articles/building/a-color-scheme)).

```css
:root { color-scheme: dark light; }               /* themes native controls too */
[data-theme="kova-dark"]  { --fg-default: #e8e6f0; --accent: #ff7a1a; }
[data-theme="kova-light"] { --fg-default: #1c1b22; --accent: #d45a00; }
```

Set `:root { color-scheme: dark light }` and the matching `<meta name="color-scheme">`, or you get the
white flash and unthemed form controls on load
([Chrome](https://developer.chrome.com/docs/modern-web-guidance/guides/visual-design/dark-mode),
[MDN](https://developer.mozilla.org/en-US/docs/Web/CSS/Reference/Properties/color-scheme)).

**Generate ramps in OKLCH; ship sRGB hex.** `oklch()` is CSS Color 4
([spec](https://www.w3.org/TR/2026/CRD-css-color-4-20261022),
[MDN](https://developer.mozilla.org/en-US/docs/Web/CSS/Reference/Values/color_value/oklch)). A fixed `L`
gives equal *perceived* lightness across hues, which HSL does not — the reason hand-picked HSL palettes
break contrast in half their themes. Evil Martians' writeup has the gamut-clamp recipe
(`colorjs.io.toGamut({space:'srgb'})`) and relative-colour derivations like
`oklch(from var(--accent) calc(l - 0.08) c h)` for hover states
([OKLCH in CSS](https://evilmartians.com/chronicles/oklch-in-css-why-quit-rgb-hsl)). Their own caveat is
the one to keep: **OKLCH is not a contrast guarantee**. You still assert contrast — see part 2 §7.

Recipe for `scripts/build_tokens.py`:

1. Per-hue ramps as (target `L`, max `C`, `H`) with **identical `L` targets for every hue**.
2. Clamp `C` into sRGB per (`L`,`H`) before emitting hex.
3. Emit `tokens.css` (semantic only), `theme.py` (keeping the existing `ACCENT`/`MUTED`/… API that
   `cli.py` and `ui.py` import), and `tui_palette.json`.
4. Run the contrast gate and **fail the build** if a declared pair drops below threshold.

Migration note: `theme.py`'s 20 names are already the right *semantic* names. Keep them as the public
Python API, put the primitive layer underneath, and don't rename — `cli.py` (115 KB) and `ui.py` (37 KB)
import them.

---

## 2. Theme engine (#174–#178): a theme is data, never code

This is the security-relevant decision in the epic, and the prior art is unambiguous.

- **VS Code** themes are JSON extensions (`colors`, `tokenColors`, `semanticTokenColors`) — but extensions
  run **unsandboxed**: "The extension host has the same permissions as VS Code itself"
  ([colour theme guide](https://code.visualstudio.com/api/extension-guides/color-theme),
  [runtime security](https://code.visualstudio.com/docs/configure/extensions/extension-runtime-security)).
  *Theme* packages have exploited this repeatedly: a malicious-theme cluster on Marketplace and Open VSX
  ([Socket](https://socket.dev/blog/glassworm-vscode-themes)), and "Theme Darcula dark" with 45k installs
  stealing PII ([Check Point](https://blog.checkpoint.com/securing-the-cloud/malicious-vscode-extensions-with-more-than-45k-downloads-steal-pii-and-enable-backdoors)).
- **Obsidian**: `theme.css` + `manifest.json`, registered in one central `community-css-themes.json`
  ([sample](https://github.com/obsidianmd/obsidian-sample-theme),
  [registry](https://github.com/obsidianmd/obsidian-releases/blob/master/community-css-themes.json)).
  OWASP is blunt that user-supplied CSS is "a considerable risk"
  ([WSTG CLNT-05](https://owasp.boireau.io/4-web_application_security_testing/11-client-side_testing/05-testing_for_css_injection)).
- **Zed**: a JSON *Theme Family* (`name`, `themes[]` with `appearance: light|dark`, `style`, `syntax{}`)
  against a versioned schema ([theme extensions](https://zed.dev/docs/extensions/themes),
  [schema](https://zed.dev/schema/themes/v0.2.0.json)).
- **Ghostty**: a theme *is* a config file; `theme = dark:X,light:Y` auto-switches with system appearance;
  user themes shadow built-ins ([docs](https://ghostty.org/docs/features/theme)). Its ecosystem advice is
  "read a third-party theme file before you use it", because a theme can set *any* option.
- **Alacritty**: TOML merged in order via `[general] import`, later wins
  ([config](https://alacritty.org/config-alacritty.html)).
- **Warp**: YAML in `~/.warp/themes`, a gallery repo with generated previews, and "Sync with OS"
  ([custom themes](https://docs.warp.dev/terminal/appearance/custom-themes)).

**Design:**

```json
{ "$schema": "https://kova.dev/schema/theme-1.json",
  "id": "ember", "name": "Ember", "version": "1.0.0",
  "appearance": "dark",
  "extends": "kova-dark",
  "tokens": { "semantic": { "accent": "#ff7a1a", "fg.default": "#e8e6f0" } },
  "syntax": { "keyword": "…", "string": "…", "comment": "…", "number": "…", "function": "…", "type": "…", "invalid": "…" },
  "terminal": { "ansi": ["#1c1b22", "…16 entries…"] } }
```

Rules, each traceable to an incident above:

1. **JSON only. Reject `.css`.** #178's optional custom CSS stays a deliberate local-only, off-by-default
   exception — the epic already says this; keep it.
2. **Validate against a JSON Schema with an allowlisted token-path set.** Unknown key → reject the file,
   don't ignore it. Colours must parse as hex/`rgb()`/`oklch()`; no `url()`, no `var()` passthrough into
   selectors ([OWASP XSS prevention, CSS contexts](https://cheatsheetseries.owasp.org/cheatsheets/Cross_Site_Scripting_Prevention_Cheat_Sheet.html)).
3. **Never `innerHTML` a theme value.** Apply via `document.documentElement.style.setProperty()` or a
   `<style>` built from validated values only.
4. **`extends` for inheritance** — a community theme overrides ~15 colours, not 300. This is what makes a
   gallery viable, and it is what the DTCG Resolver module formalises.
5. **`syntax` is required and separate from UI colours.** VS Code keeps `colors` and `tokenColors` apart;
   Zed has a `syntax` object. Light syntax colours essentially never transfer from dark, so require the
   full role list per theme instead of deriving it.
6. **`terminal` per theme is what makes #211 possible** (see §3).
7. **Three-state appearance setting** `system | light | dark`, `system` resolved by
   `@media (prefers-color-scheme)` — matching VS Code, Zed (`mode: "system"`) and Ghostty (`dark:`/`light:`)
   ([VS Code themes](https://code.visualstudio.com/docs/configure/themes), [Zed](https://zed.dev/docs/themes)).
8. **Gallery (#177)**: a static registry JSON + theme files with SHA-256 checksums; install = download →
   validate → write to the themes dir → reload. Mirror Obsidian's stance that user assets **don't
   auto-update** — offer "check for updates" ([Obsidian themes](https://obsidian.md/help/Extending+Obsidian/Themes)).
   Render gallery previews inside a sandboxed `<iframe>` so a hostile theme cannot touch the app.
9. **Export (#212)**: the same JSON, *plus* generated Ghostty/Alacritty/Zed/VS Code snippets. That turns a
   Kova theme into something users carry to their terminal and editor — cheap, and a real differentiator
   for a local-first app.

---

## 3. Terminal parity (#211): map roles, don't nearest-match at 16 colours

`kova.tui.style` already converts to 256/16, so this is a refactor of an existing path. The degradation
policy matters more than detection:

- **24-bit** (`COLORTERM=truecolor|24bit`): emit token hex directly.
- **256**: nearest colour in OKLab against the *standardised* 6×6×6 cube (16–231) plus 24 greys (232–255),
  which have well-defined RGB ([ArchWiki](https://wiki.archlinux.org/title/Color_output_in_console)).
- **16**: **do not nearest-match.** Those entries are reprogrammed by the user's terminal theme — "your app
  has zero control over the actual colors" ([ansi.md](https://ansi.md/env)). Ship a semantic→ANSI *role map*
  per theme (`accent→4/12`, `error→1/9`, `success→2/10`, `warning→3/11`, `muted→8`), which is exactly what
  Ghostty's `palette = 0..15` block encodes ([example](https://ghostty-org-ghostty.mintlify.app/features/themes)).
- **No colour**: honour `NO_COLOR` ([no-color.org](https://no-color.org/)) and `--color=auto|always|never`,
  strip SGR, keep bold/reverse. Degrade *layout* too — borders to ASCII, icons to `*`/`>`.

Detection order worth copying: `NO_COLOR` → `FORCE_COLOR` → `COLORTERM` → `TERM` → terminfo query
(`XTGETTCAP` RGB/Tc), with the caveat that **tmux converts 24-bit for its client itself**
([OpenTUI](https://opentui.com/docs/reference/terminal-capabilities)). Node's `tty` module is a readable
reference implementation of the env chain ([tty.js](https://github.com/nodejs/node/blob/main/lib/internal/tty.js?plain=1)).

Kova owns its TUI, so an explicit three-profile generator gives you the role-map policy above instead of
`rich`'s heuristic (rich "will attempt to pick the closest color" on 256-colour terminals
[rich styles](https://rich.readthedocs.io/en/latest/style.html)). Add a `kova doctor`-style "test my terminal"
that emits a truecolor probe ([termstandard/colors](https://github.com/termstandard/colors)).

TUI glyphs should **not** share SVG icons. Ship one `glyphs.json` with `nerd-font`, `unicode` (`✓ ✗ ⚠ ›`) and
`ascii` (`[x] [!] >`) profiles, selected by a capability/override setting, keeping semantic names identical
to the GUI icon names so themes and tests stay symmetric.

---

## 4. Motion (#181)

Convention, not spec (no standards body defines these): `instant 0`, `fast 120ms` (hover/press),
`base 180ms` (popover/drawer), `slow 280ms` (view change); easings `standard cubic-bezier(.2,0,0,1)` and
`exit cubic-bezier(.4,0,1,1)`. DTCG 2025.10 has `duration`/`transition` types, so these live beside the
colours in the same JSON.

Reduced motion is a **WCAG criterion**: SC 2.3.3 requires interaction-triggered motion be disable-able
([Understanding 2.3.3](https://www.w3.org/WAI/WCAG21/Understanding/animation-from-interactions),
[C39](https://www.w3.org/WAI/WCAG22/Techniques/css/C39),
[SCR40](https://www.w3.org/WAI/WCAG22/Techniques/client-side-script/SCR40)). Two rules most teams get
wrong, both from Chrome's guidance ([motion](https://developer.chrome.com/docs/modern-web-guidance/guides/ui-behaviors/motion)):

- **Don't** ship `animation-duration: 0.01ms !important` globally — it makes motion *more* jarring.
- Keep colour/background transitions; dampen only spatial movement, scale, rotation and persistent loops.
  Prefer opting *in*: `@media (prefers-reduced-motion: no-preference) { … }`.

For Kova, the #181 setting = a `data-motion` attribute that overrides the media query in both directions (a
user may want less motion even when their OS doesn't ask). `app.css` is already 71 KB of hand-written CSS;
motion belongs in `transition:` on existing rules, not a new JS animation layer.

---

## 5. Typography and density (#179)

- **Type scale**: IBM's is the best-documented enterprise example — one equation from a 12px base, giving
  12/14/16/18/20/24/28/32 with paired line-heights ([IBM](https://www.ibm.com/design/language/typography/type-scale)).
- **Minimum sizes**: a 2025 ACM SIGACCESS review found consensus that **16px is the ideal body minimum**,
  12–13px acceptable for short labels ([ACM](https://dl.acm.org/doi/10.1145/3663547.3759692)). Never below
  12px; 14px for dense tables; 16px in the reading pane.
- **Line length**: 45–90 chars, **target 66** ([USWDS](https://designsystem.digital.gov/components/typography));
  60–80 at 1.5–1.6 line-height for desktop reading, flagged as convention rather than research
  ([Pimp My Type](https://pimpmytype.com/line-length-line-height)). Chat can run to ~80ch; prose panes 66ch.
- **Fonts**: Kova already bundles under `web_assets/fonts/` — keep it (no CDN: offline, privacy, stable
  screenshot tests). All OFL-1.1, so bundling is allowed: **Inter** (variable, tabular figures — take it
  from [rsms/inter](https://github.com/rsms/inter), Google Fonts' build is "outdated, no italics"),
  **IBM Plex Sans/Mono** for a coherent sans+mono pair ([IBM/plex](https://github.com/ibm/plex)),
  **Atkinson Hyperlegible Next** as an optional accessibility font
  ([Braille Institute](https://www.brailleinstitute.org/freefont)). Use `font-display: swap` plus a
  system-UI fallback stack.
- **Density**: 4px grid, three modes as *semantic tokens* (not media queries): compact 28px rows, default
  32px, comfortable 36–40px. Only the mode changes spacing tokens; components never hardcode px.
