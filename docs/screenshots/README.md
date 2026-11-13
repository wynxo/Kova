# Web app screenshots

Before/after pairs for the web app visual pass. Both sides were captured the
same way: `uv run kova serve` on an isolated `KOVA_HOME` with port 7813, a
local OpenAI-compatible stub model, and a real WebKit browser engine with a
1440x900 or 360x740 CSS viewport, the same seeded conversation in each pair.
The files are saved at 1.25x the CSS viewport (1800x1125 and 450x925).

- `before-*`: the working tree at `main` (7134c54).
- `after-*`: the same views with this change.

`docs/screenshots/after-mobile-sidebar-360x740.png` is the drawer after
tapping the menu button.
