# Vendored fonts

- `InterVariable.woff2`, `InterVariable-Italic.woff2`: Inter 4.1 (variable, 100–900),
  https://github.com/rsms/inter/releases/tag/v4.1 (`web/InterVariable.woff2`).
- `JetBrainsMonoVariable.woff2`: JetBrains Mono 2.304 (variable, 100–800),
  https://github.com/JetBrains/JetBrainsMono/releases/tag/v2.304
  (`fonts/variable/JetBrainsMono[wght].ttf`, converted to woff2 with fontTools).

Both families are licensed under the SIL Open Font License 1.1; the license texts
are `Inter-LICENSE.txt` and `JetBrainsMono-OFL.txt`. Nothing is fetched at runtime:
the app is served from `127.0.0.1` and its CSP allows fonts from `'self'` only.
