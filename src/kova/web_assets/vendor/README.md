# Vendored browser dependencies

- marked.js: marked 18.2.0, https://unpkg.com/marked@18.2.0/lib/marked.umd.js
- purify.js: dompurify 3.4.17, https://unpkg.com/dompurify@3.4.17/dist/purify.min.js
- highlight.js: @highlightjs/cdn-assets 11.12.0, https://unpkg.com/@highlightjs/cdn-assets@11.12.0/highlight.min.js
- katex.js: KaTeX 0.19.1, npm package `katex`, `dist/katex.min.js` (native MathML output, no fonts needed).
- mermaid.js: Mermaid Tiny 12.3.0, npm package `@mermaid-js/tiny`, `dist/mermaid.tiny.js` (common diagrams without ELK, mindmaps, architecture diagrams or inline KaTeX).

Shipped locally for offline use. Licenses are alongside each file. No CDN is contacted at runtime.

Icons in `app.css` are Lucide (lucide-static 1.54.0, https://lucide.dev), embedded as CSS masks.
License: `lucide.LICENSE`.
