// Quiet header row for rendered code blocks (#189): the language name, a Wrap
// toggle and a Copy button. Self-contained classic script (no module scope): it
// loads after highlight.js, so `hljs` exists, but before app.js, so it does not
// see app.js's local `hljs` or `toast`. It uses the page's toast component when
// there is one and keeps a small fallback for tests and older shells.
(function () {
  const COPIED_MS = 2000;
  // The wrap choice is a reading preference, per browser, like the theme.
  const WRAP_KEY = 'kova-code-wrap';
  // Line numbers are a reading preference too, and get the same treatment.
  const LINES_KEY = 'kova-code-lines';
  // A gutter on a block this long is more work than it is worth.
  const MAX_LINES = 2000;
  // Blocks longer than this also get "Open in panel" (#182) when the side panel exists.
  const PANEL_LINES = 100;

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

  function linesEnabled() {
    try {
      return storage()?.getItem(LINES_KEY) === '1';
    } catch (_) {
      return false;
    }
  }

  function storeLines(on) {
    try {
      storage()?.setItem(LINES_KEY, on ? '1' : '0');
    } catch (_) { /* a private window may refuse storage; the toggle still works */ }
  }

  // Source lines, ignoring the single trailing newline marked puts on a fence.
  function lineCount(code) {
    const text = String(code?.textContent || '');
    const body = text.endsWith('\n') ? text.slice(0, -1) : text;
    return Math.max(1, body.split('\n').length);
  }

  // A gutter is a plain column of numbers beside the code, not a second copy of
  // it: hidden from screen readers, never selectable and never part of a copy.
  function addGutter(pre, code) {
    if (!pre || !code || pre.querySelector?.('.code-gutter')) return;
    const count = lineCount(code);
    if (count > MAX_LINES) return;
    const gutter = el('span', 'code-gutter');
    gutter.setAttribute('aria-hidden', 'true');
    gutter.textContent = Array.from({length: count}, (_, index) => index + 1).join('\n');
    pre.classList.add('has-lines');
    pre.insertBefore(gutter, code);
  }

  function removeGutter(pre) {
    pre?.classList.remove('has-lines');
    pre?.querySelector?.('.code-gutter')?.remove();
  }

  // One preference for every block, like Wrap: toggling one row updates the
  // blocks already on screen and every other header's pressed state.
  function applyLines(on, local) {
    storeLines(on);
    if (local) {
      const code = local.querySelector?.('code');
      if (on) addGutter(local, code);
      else removeGutter(local);
    }
    document.querySelectorAll?.('.markdown pre').forEach((pre) => {
      // A block still arriving has no fixed line count yet.
      if (pre.getAttribute?.('data-streaming') !== null) return;
      const code = pre.querySelector('code');
      if (!code) return;
      if (on) addGutter(pre, code);
      else removeGutter(pre);
    });
    document.querySelectorAll?.('.lines-code').forEach((button) => {
      button.setAttribute('aria-pressed', String(on));
    });
  }

  // An icon from the app's own set (app.css masks these by name).
  function icon(kind) {
    const node = el('span', `icon i-${kind}`);
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

  function linesButton(pre) {
    const button = el('button', 'code-action lines-code');
    button.type = 'button';
    button.setAttribute('aria-label', 'Show line numbers');
    button.setAttribute('aria-pressed', String(linesEnabled()));
    button.append(el('span', '', 'Lines'));
    button.onclick = () => {
      const next = button.getAttribute('aria-pressed') !== 'true';
      button.setAttribute('aria-pressed', String(next));
      applyLines(next, pre);
    };
    return button;
  }

  // Show a long block in the side panel: a copy of the highlighted code, so
  // the message keeps its own and the panel needs no clean-up.
  function panelButton(code, language) {
    const button = el('button', 'code-action open-in-panel');
    button.type = 'button';
    button.setAttribute('aria-label', 'Open code in panel');
    button.append(icon('panel-right'), el('span', '', 'Open in panel'));
    button.onclick = () => {
      const pre = el('pre');
      const copy = el('code', code.className);
      copy.append(...Array.from(code.childNodes, (node) => node.cloneNode(true)));
      pre.append(copy);
      // `.markdown` gives the copy the same block styling as in the message.
      const wrap = el('div', 'markdown side-panel-code');
      wrap.append(pre);
      const lines = code.textContent.replace(/\n$/, '').split('\n').length;
      window.KovaPanel.open({ title: `${language} · ${lines} lines`, node: wrap, returnFocus: button });
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
      const filename = code.getAttribute('data-kova-file');
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
      const count = lineCount(code);
      if (!streaming && count > PANEL_LINES && window.KovaPanel?.open) actions.append(panelButton(code, language));
      actions.append(wrapButton(pre));
      // A fence still arriving has no fixed line count yet.
      if (!streaming && count <= MAX_LINES) actions.append(linesButton(pre));
      actions.append(copy);
      const labelGroup = el('span', 'code-labels');
      const languageLabel = el('span', 'code-lang', language);
      if (filename) {
        const file = el('span', 'code-filename', filename);
        file.title = filename;
        labelGroup.append(file, languageLabel);
        bar.append(labelGroup, actions);
      } else bar.append(languageLabel, actions);
      code.parentNode.insertBefore(bar, code);
      if (linesEnabled() && !streaming && count <= MAX_LINES) addGutter(pre, code);
    });
  }

  window.CodeHeader = {attach, wrapEnabled, applyWrap, linesEnabled, applyLines};
})();
