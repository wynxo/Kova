// Quiet header row for rendered code blocks (#189): the language name and a
// Copy button. Self-contained classic script (no module scope): it loads after
// highlight.js, so `hljs` exists, but before app.js, so it does not see app.js's
// local `hljs` or `toast`. It uses the page's toast component when there is one
// and keeps a small fallback for tests and older shells.
(function () {
  const COPIED_MS = 2000;

  // Build an element with optional class list and text content.
  function el(tag, cls, text) {
    const node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text !== undefined) node.textContent = text;
    return node;
  }

  // An icon from the app's own set (app.css masks these by name).
  function icon(kind) {
    const node = el('span', `icon i-${kind === 'check' ? 'check' : 'copy'}`);
    node.setAttribute('aria-hidden', 'true');
    return node;
  }

  // A transient status message, announced to screen readers.
  function toast(message) {
    const shared = typeof window !== 'undefined' ? window.KovaToast : null;
    if (shared?.show) { shared.show(message, {severity: 'error'}); return; }
    const node = el('div', 'toast');
    node.append(el('div', 'toast-body', message));
    const root = document.querySelector?.('#toasts');
    if (root) root.append(node);
    setTimeout(() => node.remove(), 9000);
  }

  // Copy to clipboard, falling back to a toast when it is unavailable.
  async function copyText(text, copied) {
    try {
      await navigator.clipboard.writeText(text);
      copied?.();
    } catch (_) {
      toast('Clipboard unavailable. Select the text to copy it.');
    }
  }

  // Attach (or re-attach) a header row to every `pre code` inside `root`.
  // The marker on the root keeps a double attach from duplicating the row; a
  // caller that rebuilds the root's subtree (the streaming transcript does)
  // clears it first.
  function attach(root, final = false) {
    if (!root || root.classList.contains('code-header-ready')) return;
    root.classList.add('code-header-ready');
    root.querySelectorAll('pre code').forEach((code) => {
      try {
        if (code.textContent.length < (final ? 20000 : 4000)) hljs.highlightElement(code);
      } catch (_) {}
      const language = /language-([\w+#.-]+)/.exec(code.className)?.[1] || 'text';
      const bar = el('div', 'code-bar');
      const copy = el('button', 'copy-code');
      copy.type = 'button';
      copy.setAttribute('aria-label', 'Copy code');
      const label = el('span', '', 'Copy');
      copy.append(icon('copy'), label);
      copy.onclick = () => {
        copyText(code.textContent, () => {
          label._timer && clearTimeout(label._timer);
          label.textContent = 'Copied';
          label._timer = setTimeout(() => { label.textContent = 'Copy'; }, COPIED_MS);
        });
      };
      bar.append(el('span', 'code-lang', language), copy);
      code.parentNode.insertBefore(bar, code);
    });
  }

  window.CodeHeader = { attach };
})();
