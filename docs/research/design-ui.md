# Design research for the redesign epic (#214)

Research supporting [#214](https://github.com/wynxo/Kova/issues/214) and its child issues
#171–#213. Two independent web-research passes: one on design tokens / theming / visual testing,
one on agent-chat UI patterns. Every claim carries a source link; conventions are labelled as such.

Split into three parts because a single file would be unreadable at this length:

1. [Part 1 — tokens, themes, TUI, motion, typography](design-ui-1-tokens-and-themes.md)
2. [Part 2 — visual/a11y testing, icons, conversation UI](design-ui-2-testing-and-conversation.md)
3. [Part 3 — streaming, settings, layout, agents, build order](design-ui-3-layout-settings-and-plan.md)

**Status:** research only. Nothing here was verified by running Kova — no code executed, no screen
rendered. Stack facts are read from the repository tree and `pyproject.toml`.

## The one thing to settle first

#214 says *"Monochrome UI. Orange is the only accent and means 'Kova is working'"*, and #171 is a
new logo around that. But `src/kova/theme.py` today defines `ACCENT = "#8b7cff"` — *"Kova violet"* —
plus a violet→pink `GRADIENT`. #171, #173 and #175 are all blocked behind picking one accent story.

## The stack constraint that reshapes the recommendations

| Thing | Reality in this repo |
|---|---|
| Runtime | Python 3.12, FastAPI (`pyproject.toml`, `uv.lock`) |
| Web UI | `kova/web_assets/`: `index.html` 17.5 KB, `app.css` 71 KB (one file), `app.js` 150 KB (one file) |
| Frontend deps | `web_assets/vendor/`, pre-vendored — no npm, no bundler, no framework |
| Fonts | `web_assets/fonts/`, already bundled |
| PWA | `sw.js` + `manifest.webmanifest` |
| Colours | `kova/theme.py`, 20 constants, single source of truth |
| Code blocks | Pygments server-side (`CODE_THEME = "monokai"`) |
| TUI | `kova/tui/`, `kova.tui.style` converts to 256/16 colours already |
| Config | TOML via `tomlkit`, documented in `docs/CONFIG.md` |

Any recommendation that assumes a package manager is wrong here: prefer a vendored single file under
`web_assets/vendor/`, or push the work server-side into Python.
