# Web app screenshots

Before/after pairs for the web app visual pass. Both sides were captured the
same way: `uv run kova serve` on an isolated `KOVA_HOME` with port 7813, a
local OpenAI-compatible stub model, and a real WebKit browser engine at a
1440x900 or 360x740 viewport (device pixel ratio 1), the same seeded
conversation in each pair.

- `before-*`: the working tree at `main` (7134c54).
- `after-*`: the same views with this change.

`docs/screenshots/after-mobile-sidebar-360x740.png` is the drawer after
tapping the menu button.
