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

> There is no browser and no JavaScript in this path. A theme is a JSON document,
> validated on the server before it is ever offered to the frontend, so a broken theme
> cannot reach `app.css`.

## Adding a built-in theme

A built-in theme is different work: it goes into `design/tokens.json` under `semantic`,
needs a full token set (not a diff), and must pass `build_tokens.py --check` in CI. See
[DESIGN.md](DESIGN.md) for the checklist. If you only want your own colours, write a
theme file - that path needs no code change and no release.
