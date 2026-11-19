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

## Install a theme

A theme file becomes a real theme the moment it lands in Kova's home.

```bash
cp path/to/theme.json ~/.kova/themes/          # or $KOVA_HOME/themes/
kova themes check ~/.kova/themes/theme.json     # validate; prints the tokens it resolves to
kova themes list                                # now lists it alongside the two built-ins
```

`check` is the structural gate: it rejects unknown keys, malformed ids and non-colour
values, then prints the tokens the theme resolves to. `list` is how you confirm Kova has
seen it. A file that fails `check` is simply not offered - it does not affect the two
built-ins - so fix the file and re-run it until it passes.

Then pick it in the web app under **Settings > General > Theme**. It appears with its
`name`, alongside **System**, **Light** and **Dark**. Choosing it sets the `theme`
preference, so a paired device with the same Kova gets the same choice.

Until the file is present on a device, the choice falls back to the device's own
light/dark setting: the preference is remembered, but nothing is applied until the file
arrives. Installing the file is the step that makes the theme real.

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

## In the terminal

The terminal chat and every `kova` command paint with the same themes. Each terminal
role follows a semantic token (`accent.default` is the accent, `fg.muted` the quiet
text, `status.danger` errors, and so on; the shell and "you" colours follow the theme's
ANSI magenta and cyan), so a theme file needs nothing extra to work there.

```sh
kova themes use midnight-teal         # the terminal's theme, saved as [tui] theme
kova themes use light --app           # the terminal and the app, on every paired device
KOVA_THEME=forest kova                # one run only
```

`kova themes list` marks the terminal's theme with ●. Colours follow the terminal's
depth: exact in 24-bit colour, the nearest match in 256 colours, and at 16 colours each
role keeps a fixed meaning (red errors, green success) while the accent takes the basic
colour nearest to the theme's.

## Make your own

In **Settings > General > Theme**, choose **Customize…** to start from the current
palette, duplicate another installed theme, or **Import** a theme JSON file (up
to 32 KiB). Edit a swatch or its hex value to preview the change on the app.
Kova checks contrast as you edit and lists any failing token pairs. **Save** is
available once the checks pass; it installs and selects the new theme without
overwriting any existing theme ID. **Cancel** restores the previous appearance.
**Export** downloads the validated draft as JSON without installing it.

To edit by hand, start from an example in `design/themes/`: copy one into
`~/.kova/themes/`, change only semantic tokens, and re-run `kova themes check`
until it passes. That command checks the file's structure, not its contrast. To measure contrast too, run
`uv run python scripts/validate_theme.py <file>` from a Kova checkout; it fails on any text
or control pair below the WCAG ratios the built-in themes meet.

## Adding a built-in theme

A built-in theme is different work: it goes into `design/tokens.json` under `semantic`,
needs a full token set (not a diff), and must pass `build_tokens.py --check` in CI. See
[DESIGN.md](DESIGN.md) for the checklist. If you only want your own colours, write a
theme file - that path needs no code change and no release.
