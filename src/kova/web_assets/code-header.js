// Quiet header row for rendered code blocks (#189): the language name, a Wrap
// toggle and a Copy button. Self-contained classic script (no module scope): it
// loads after highlight.js, so `hljs` exists, but before app.js, so it does not
// see app.js's local `hljs` or `toast`. It uses the page's toast component when
// there is one and keeps a small fallback for tests and older shells.
(function () {
  const COPIED_MS = 2000;
  // The wrap choice is a reading preference, per browser, like the theme.
  const WRAP_KEY = 'kova-code-wrap';

  // Build an element with optional class list and text content.
  function el(tag, cls, text) {
    const node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text !== undefined) node.textContent = text;
    return node;
  }

  function storage() {
    try {
      return typeof localStorage !== 'undefined' ? localStorage : null;
    } catch (_) {
      return null;
    }
  }

  function wrapEnabled() {
    try {
      return storage()?.getItem(WRAP_KEY) === '1';
    } catch (_) {
      return false;
    }
  }

  function storeWrap(on) {
    try {
      storage()?.setItem(WRAP_KEY, on ? '1' : '0');
    } catch (_) { /* a private window may refuse storage; the toggle still works */ }
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

  // Wrapping is one preference for every block, so toggling one row updates the
  // blocks already on screen and every other header's pressed state.
  function applyWrap(on, local) {
    storeWrap(on);
    if (local) {
      if (on) local.classList.add('wrap-lines');
      else local.classList.remove('wrap-lines');
    }
    document.querySelectorAll?.('.markdown pre').forEach((pre) => {
      if (on) pre.classList.add('wrap-lines');
      else pre.classList.remove('wrap-lines');
    });
    document.querySelectorAll?.('.wrap-code').forEach((button) => {
      button.setAttribute('aria-pressed', String(on));
    });
  }

  function wrapButton(pre) {
    const button = el('button', 'code-action wrap-code');
    button.type = 'button';
    button.setAttribute('aria-label', 'Wrap long lines');
    button.setAttribute('aria-pressed', String(wrapEnabled()));
    button.append(el('span', '', 'Wrap'));
    button.onclick = () => {
      const next = button.getAttribute('aria-pressed') !== 'true';
      button.setAttribute('aria-pressed', String(next));
      applyWrap(next, pre);
    };
    return button;
  }

  // Attach (or re-attach) a header row to every `pre code` inside `root`.
  // `final` says the reply has finished; `openFence` says the last block is a
  // code fence that is still being written, so it stays plain text and its Copy
  // button waits. The marker on the root keeps a double attach from duplicating
  // the row; a caller that rebuilds the root's subtree (the streaming
  // transcript does) clears it first.
  function attach(root, final = false, openFence = false) {
    if (!root || root.classList.contains('code-header-ready')) return;
    root.classList.add('code-header-ready');
    const blocks = [...root.querySelectorAll('pre code')];
    const streamingIndex = !final && openFence ? blocks.length - 1 : -1;
    const wrap = wrapEnabled();
    blocks.forEach((code, index) => {
      const streaming = index === streamingIndex;
      // Highlight only once a fence is closed: a block still arriving is plain
      // text, and re-highlighting it on every frame would flash.
      if (!streaming) {
        try {
          if (code.textContent.length < (final ? 20000 : 4000)) hljs.highlightElement(code);
        } catch (_) {}
      }
      const pre = code.parentNode;
      if (pre) {
        if (wrap) pre.classList.add('wrap-lines');
        if (streaming) pre.setAttribute('data-streaming', '');
      }
      const language = /language-([\w+#.-]+)/.exec(code.className)?.[1] || 'text';
      const bar = el('div', 'code-bar');
      const actions = el('div', 'code-actions');
      const copy = el('button', 'code-action copy-code');
      copy.type = 'button';
      copy.setAttribute('aria-label', 'Copy code');
      const label = el('span', '', 'Copy');
      copy.append(icon('copy'), label);
      if (streaming) {
        copy.disabled = true;
        copy.classList.add('waiting');
        copy.title = 'Copy is available when the block finishes';
        copy.setAttribute('aria-label', 'Copy code, available when the block finishes');
      } else {
        copy.onclick = () => {
          copyText(code.textContent, () => {
            label._timer && clearTimeout(label._timer);
            label.textContent = 'Copied';
            label._timer = setTimeout(() => { label.textContent = 'Copy'; }, COPIED_MS);
          });
        };
      }
      actions.append(wrapButton(pre), copy);
      bar.append(el('span', 'code-lang', language), actions);
      code.parentNode.insertBefore(bar, code);
    });
  }

  window.CodeHeader = {attach, wrapEnabled, applyWrap};
})();
