/* A live preview card for one theme: a miniature sample of the app painted with
   the theme's own semantic tokens, not the theme currently active on the page.

   This is the "preview card" half of #175. It renders from resolved token data
   (the same flattened `group.token` shape the theme API returns), so the gallery
   can show many themes at once and the Settings picker can preview the chosen
   one without those themes being active. It is pure presentation: aria-hidden,
   no text, no controls. Every colour comes from the theme's tokens, written as
   --preview-* custom properties that theme-preview.css reads with a fallback to
   the active theme's semantic token. Nothing here invents a colour. */
(() => {
  'use strict';

  // semantic token -> the --preview-* custom property theme-preview.css reads.
  const TOKENS = [
    ['bg.base', 'bg-base'], ['bg.surface', 'bg-surface'], ['bg.raised', 'bg-raised'],
    ['fg.default', 'fg-default'], ['fg.muted', 'fg-muted'], ['fg.faint', 'fg-faint'],
    ['accent.default', 'accent-default'], ['accent.on', 'accent-on'],
    ['border.subtle', 'border-subtle'],
    ['status.danger', 'status-danger'], ['status.success', 'status-success'],
    ['status.warning', 'status-warning'],
  ];

  function el(tag, className) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    return node;
  }

  function paint(node, name, value) {
    if (!value) return;
    if (typeof node.style.setProperty === 'function') node.style.setProperty(name, value);
    else node.style[name] = value;
  }

  /** A preview card element for the given flattened theme tokens. */
  function card(tokens) {
    const preview = el('div', 'theme-preview');
    preview.setAttribute('aria-hidden', 'true');
    for (const [token, css] of TOKENS) paint(preview, `--preview-${css}`, tokens && tokens[token]);

    const side = el('div', 'theme-preview-side');
    side.append(
      el('span', 'theme-preview-mark'),
      el('span', 'theme-preview-nav'),
      el('span', 'theme-preview-nav short'),
      el('span', 'theme-preview-nav'),
    );

    const body = el('div', 'theme-preview-body');
    const bubble = el('div', 'theme-preview-bubble');
    bubble.append(el('span', 'theme-preview-bubble-line'), el('span', 'theme-preview-bubble-line short'));
    const text = el('div', 'theme-preview-text');
    text.append(
      el('span', 'theme-preview-text-line'),
      el('span', 'theme-preview-text-line muted'),
      el('span', 'theme-preview-text-line short'),
    );
    const accent = el('div', 'theme-preview-accent');
    const status = el('div', 'theme-preview-status');
    status.append(el('span', 'ok'), el('span', 'warn'), el('span', 'err'));
    body.append(bubble, text, accent, status);

    preview.append(side, body);
    return preview;
  }

  window.KovaThemePreview = {card};
})();
