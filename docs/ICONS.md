# Icons

Kova uses [Lucide](https://lucide.dev) icons, vendored into one sprite:
[`web_assets/vendor/icons.svg`](../web_assets/vendor/icons.svg). Ten symbols, three
kilobytes, no dependency, no build step.

## Why vendored rather than a package

There is no bundler here. The frontend is `index.html`, `app.css`, `app.js` and a
`vendor/` directory served as-is, so an icon has to be a file that exists at request
time. Adding a Node toolchain for ten paths would be the largest dependency in the
project, and it would still need a build step before the app could start. A checked-in
sprite needs neither.

The trade-off is that icons do not update themselves, which is why the version is
pinned: `LUCIDE_VERSION` in `scripts/vendor_icons.py`. An icon cannot change under the
UI without a commit that says so.

## Using one

```html
<svg class="icon" aria-hidden="true"><use href="#icon-check"></use></svg>
```

- The sprite is `display:none` and loaded once, so `#icon-check` resolves anywhere in
  the document. There is no `<img>`, no network request per icon and nothing to prefix.
- Icons inherit `currentColor`. Do not set `stroke` or `fill` on the `<svg>`: the colour
  is the token's job (`--fg-muted`, `--accent-default`), not the icon's.
- Give the `<svg>` an accessible name when the icon carries meaning on its own
  (`role="img"` plus a `<title>`, or a visually hidden label). When it sits next to a
  text label, `aria-hidden="true"` is correct, as above.
- Size with `width`/`height` or the `.icon` class. The `viewBox` is `0 0 24 24` for all
  of them, so a single rule covers every icon.

## Adding an icon

1. Add the Lucide name to `ICONS` in `scripts/vendor_icons.py` (keep the list sorted).
2. Run `python scripts/vendor_icons.py`.
3. Commit the script change **and** the regenerated sprite together.

The script fetches from a fixed tag, so this is reproducible at any time. Do not paste an
icon into the sprite by hand: `--check` compares the file against a fresh render and will
fail.

## What the script guards

An icon is inlined into the page, which makes it markup the app trusts. The vendoring
step therefore refuses any file containing:

- `<script>` or `<foreignObject>`,
- an `on*` event handler attribute,
- a `javascript:` URL,
- an external `href` or `url()` reference.

A compromised or mistyped upstream file fails the build rather than becoming stored XSS
in a self-hosted app. Every symbol is also normalised to
`fill="none" stroke="currentColor" stroke-width="1.75"`, so a caller cannot forget the
attributes and accidentally get a filled black blob.

## Licence

Lucide is ISC licensed. The sprite carries the notice in its header, and the full text is
in the Lucide repository. Copyright for portions of Lucide is held by Cole Bemis
(2013-2022) and the Lucide Contributors (2022).

## Checking

```bash
python scripts/vendor_icons.py --list    # pinned set and version
python scripts/vendor_icons.py --check    # fails if the sprite is stale
```

`tests/test_icons_sprite.py` asserts the committed sprite matches the icon list, carries
its licence header and contains nothing executable. That test runs offline; only
regenerating the sprite needs the network.
