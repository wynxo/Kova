# Vendored fonts

- `Geologica-*.woff2`: Geologica (variable, weight 100–900 and sharpness 0–100) by
  Monokrom, https://github.com/monokromskriftforlag/geologisk. The interface and reply face.
- `MartianMono-*.woff2`: Martian Mono (variable, width 75–112.5 and weight 100–800) by
  Evil Martians, https://github.com/evilmartians/mono. Only for literal machine text:
  commands, paths, tool arguments and output, code.

Each family is split into four subsets (`latin`, `latin-ext`, `cyrillic`,
`cyrillic-ext`), taken from the Google Fonts builds, and `app.css` loads them with
`unicode-range`, so a page only downloads the scripts it shows.

Both families are licensed under the SIL Open Font License 1.1; the license texts
are `Geologica-OFL.txt` and `MartianMono-OFL.txt`. Nothing is fetched at runtime:
the app is served from `127.0.0.1` and its CSP allows fonts from `'self'` only.
