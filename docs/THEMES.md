# Themes

Kova ships two themes, `kova-dark` and `kova-light`, both generated from
[`design/tokens.json`](../design/tokens.json). Everything else is a *theme file*: a
small JSON document a user can drop into `~/.kova/themes/`, share, or install from the
gallery. Theme files are data. They never contain CSS, JavaScript, URLs or code of any
kind.

## The two rules

1. **Override semantic tokens only.** A theme says `accent.default`, not
   `primitive.orange.500`, and never a component's colour. Components read
   `var(--accent-default)`; the theme changes what that resolves to.
2. **Pass the contrast gate.** A theme is merged over its base and then measured. If it
drops body text below 4.5:1 or a control boundary below 3:1, it is rejected - the same
maths that guards the built-in themes (`scripts/build_tokens.py --check`).

The reason for rule 1 is rule 2. Contrast can only be checked if a theme speaks in
semantic names: the checker knows that `fg.default` sits on `bg.base`, so it can measure
it. A theme that redefined raw hex values on individual components would be
unverifiable, and would break on the next palette change.

## A minimal theme

```json
{
  "id": "midnight-teal",
  "name": "Midnight Teal",
  "version": "1.0.0",
  "appearance": "dark",
  "extends": "kova-dark",
  "tokens": {
    "accent": {
      "default": "#2dd4bf",
      "hover": "#5eead4",
      "pressed": "#14b8a6",
      "on": "#04201c"
    }
  }
}
```

`extends` is what makes themes small: everything not listed is inherited. Set
`accent.on` whenever you change `accent.default`, because that is the text colour drawn
*on top of* a solid accent chip, and the pairing is the one most likely to fail.

## Fields

| Field | Required | Notes |
| --- | --- | --- |
| `id` | yes | `^[a-z0-9][a-z0-9-]{1,39}$`. Stable; used by settings and exports. |
| `name` | yes | Shown in the theme picker. |
| `version` | yes | Semver. Bump it when the values change. |
| `appearance` | yes | `light` or `dark`. Decides which base is used when `extends` is absent, and tells the browser how to render native controls. |
| `extends` | no | `kova-dark` or `kova-light`. Defaults from `appearance`. |
| `tokens` | no | Groups of semantic overrides: `bg`, `fg`, `border`, `accent`, `status`, `syntax`. |
| `terminal` | no | Exactly 16 colours, ANSI order 0-15, for the TUI. Inherited when absent. |
| `author`, `description` | no | Free text, length-limited. |

Colour values are hex (`#rgb`, `#rrggbb`, `#rrggbbaa`) or `rgb()` / `rgba()`. Named
colours are rejected: `red` is not a value a contrast checker can read without shipping
a colour table, and it is not a value a theme author can predict across browsers.

## Validating

```bash
python scripts/validate_theme.py themes/*.json          # exit 1 on any failure
python scripts/validate_theme.py --explain my-theme.json  # print the merged result
```

Two layers run in order. The **schema** (`design/theme.schema.json`) rejects unknown
keys, malformed ids and anything that is not a colour - a theme file is untrusted input,
so `additionalProperties: false` everywhere is deliberate. The **semantic checks** then
merge the theme over its base and measure the WCAG pairs, reporting the ratio it got and
the ratio it needed.

If you have a theme file but not a checkout - one you downloaded, say - `kova themes check
path/to/theme.json` runs the structural checks on it and prints the tokens it resolves to,
without starting the server. Contrast stays the build-time gate in `build_tokens.py
--check` and `validate_theme.py`.

> There is no browser and no JavaScript in this path. A theme is a JSON document,
> validated on the server before it is ever offered to the frontend, so a broken theme
> cannot reach `app.css`.

## Listing what Kova found

```bash
kova themes list          # the two built-ins, then the theme files in Kova's home
kova themes show midnight-teal   # its tokens, resolved over the base it extends
```

The server resolves a theme; the browser never does. `GET /themes` lists every theme Kova can
offer - a file that does not validate is listed with its error rather than hidden - and
`GET /themes/<id>` returns the merged tokens. The built-in palettes come from
`src/kova/tokens.json`, which `scripts/build_tokens.py` generates from `design/tokens.json`
next to `tokens.css`.

## Choosing a theme

Settings > General > Theme offers **System**, **Light** and **Dark**, then every
theme file Kova found that validates. The command palette lists the same choices
("Theme: Midnight Teal"), and the light/dark shortcut always lands on the built-in
opposite of what is on screen.

The choice is one value in the shared UI preferences (`theme`: `system`, `light`,
`dark` or a theme id), so a phone paired with the same Kova shows the same theme, and
`kova prefs export` carries it. `system` is the default and follows the device's
light/dark setting live. A preference naming a theme that is not installed on this
machine is kept; until the file appears the page follows the device.

In the browser, `web_assets/theme.js` applies it. A built-in only sets
`data-theme`, and `tokens.css` does the rest. A theme file is fetched resolved from
`GET /themes/<id>`, and each of its semantic tokens becomes the matching custom
property on `<html>` (`accent.default` -> `--accent-default`); anything that is not a
semantic token with a plain colour value is dropped even though the server already
checked it. The browser's `theme-color`, the desktop window and the installed app's
manifest (`theme_color`, `background_color`) all take the theme's `bg.base`; the
manifest uses the dark base for `system`, since a manifest cannot follow the device.

## Adding a built-in theme

A built-in theme is different work: it goes into `design/tokens.json` under `semantic`,
needs a full token set (not a diff), and must pass `build_tokens.py --check` in CI. See
[DESIGN.md](DESIGN.md) for the checklist. If you only want your own colours, write a
theme file - that path needs no code change and no release.
