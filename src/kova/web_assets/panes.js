// Resizable three-pane layout (#182): sidebar | conversation | side panel.
//
// Self-contained classic script. It builds two splitters and the side panel
// itself, so index.html and app.js need no new markup, and it exposes a small
// API other features use to show content beside the conversation:
//
//   window.KovaPanel.open({title, node})  show `node` in the panel
//   window.KovaPanel.close()              close it; focus goes back
//   window.KovaPanel.isOpen()
//
// Widths are remembered per device in localStorage. The limits come from the
// --pane-* properties in panes.css, so CSS and JS cannot disagree. On phone
// widths there are no splitters and the panel is a full-screen sheet.
(function () {
  'use strict';
  if (typeof document === 'undefined' || !document.body) return;

  const STEP = 16;          // one arrow-key press, in pixels (the 4px grid × 4)
  const BIG_STEP = 64;      // with Shift
  const KEYS = { sidebar: 'kova-pane-sidebar-w', panel: 'kova-pane-panel-w' };
  const root = document.documentElement;
  const body = document.body;
  const wide = window.matchMedia ? window.matchMedia('(min-width:721px)') : { matches: true };

  function store(key, value) {
    try {
      if (value === null) localStorage.removeItem(key);
      else localStorage.setItem(key, String(value));
    } catch (_) { /* private window or blocked storage: the width just isn't kept */ }
  }
  function stored(key) {
    try { return Number.parseInt(localStorage.getItem(key) || '', 10); } catch (_) { return NaN; }
  }
  // A pixel length from a custom property, as panes.css / app.css declare it.
  function cssPx(name, fallback) {
    const value = Number.parseFloat(getComputedStyle(root).getPropertyValue(name));
    return Number.isFinite(value) ? value : fallback;
  }

  function el(tag, cls, attrs) {
    const node = document.createElement(tag);
    if (cls) node.className = cls;
    for (const [key, value] of Object.entries(attrs || {})) node.setAttribute(key, value);
    return node;
  }

  // ---------- One draggable, keyboard-operable splitter ----------
  // `pane` describes the width it controls: its custom property, its limits,
  // and which way a rightward drag moves it (+1 sidebar, -1 right panel).
  function splitter(pane) {
    const node = el('div', 'pane-splitter', {
      id: pane.id,
      role: 'separator',
      tabindex: '0',
      'aria-orientation': 'vertical',
      'aria-controls': pane.controls,
      'aria-label': pane.label,
      title: `${pane.label}. Drag or use the arrow keys; double-click or Enter resets it.`,
    });
    // The default width as a number, read before any saved width overrides it.
    const fallback = cssPx(pane.property, pane.fallback);
    let current = fallback;

    // `wanted` is what the person chose; `current` is what fits right now. A
    // narrow window squeezes `current`, and widening it again restores `wanted`.
    let wanted = fallback;

    function limits() {
      const min = cssPx(pane.minProperty, 200);
      const cap = cssPx(pane.maxProperty, 480);
      // Leave the conversation its minimum, given whatever the other pane takes.
      const room = window.innerWidth - pane.other() - cssPx('--pane-chat-min', 360);
      return { min, cap, max: Math.max(min, Math.min(cap, room)) };
    }
    function fit() {
      const { min, max } = limits();
      current = Math.round(Math.min(max, Math.max(min, wanted)));
      root.style.setProperty(pane.property, `${current}px`);
      node.setAttribute('aria-valuemin', String(Math.round(min)));
      node.setAttribute('aria-valuemax', String(Math.round(max)));
      node.setAttribute('aria-valuenow', String(current));
      node.setAttribute('aria-valuetext', `${current} pixels wide`);
    }
    function set(width, save) {
      const { min, cap } = limits();
      wanted = Math.round(Math.min(cap, Math.max(min, width)));
      fit();
      // Save what fits: a drag past the limit should not remember an unseen width.
      if (save) { wanted = current; store(pane.key, current); }
    }
    function reset() {
      store(pane.key, null);
      wanted = fallback;
      fit();
    }

    node.addEventListener('pointerdown', (event) => {
      if (event.button !== 0) return;
      event.preventDefault();
      const startX = event.clientX;
      const startWidth = current;
      node.setPointerCapture?.(event.pointerId);
      node.classList.add('is-dragging');
      body.classList.add('panes-dragging');
      const move = (e) => set(startWidth + pane.direction * (e.clientX - startX), false);
      const end = () => {
        node.removeEventListener('pointermove', move);
        node.removeEventListener('pointerup', end);
        node.removeEventListener('pointercancel', end);
        node.classList.remove('is-dragging');
        body.classList.remove('panes-dragging');
        wanted = current;
        store(pane.key, current);
      };
      node.addEventListener('pointermove', move);
      node.addEventListener('pointerup', end);
      node.addEventListener('pointercancel', end);
    });
    node.addEventListener('dblclick', reset);
    node.addEventListener('keydown', (event) => {
      const step = event.shiftKey ? BIG_STEP : STEP;
      const { min, max } = limits();
      let next = null;
      if (event.key === 'ArrowLeft') next = current - pane.direction * step;
      else if (event.key === 'ArrowRight') next = current + pane.direction * step;
      else if (event.key === 'Home') next = min;
      else if (event.key === 'End') next = max;
      else if (event.key === 'Enter') { event.preventDefault(); reset(); return; }
      if (next === null) return;
      event.preventDefault();
      event.stopPropagation();
      set(next, true);
    });

    const saved = stored(pane.key);
    if (Number.isFinite(saved)) set(saved, false); else fit();
    window.addEventListener('resize', fit);
    return { node, width: () => current, refit: fit };
  }

  // ---------- The side panel ----------
  const panel = el('aside', 'side-panel', { id: 'side-panel', 'aria-label': 'Side panel' });
  panel.hidden = true;
  const head = el('header', 'side-panel-head');
  const title = el('h2', 'side-panel-title', { id: 'side-panel-title' });
  const close = el('button', 'icon-button side-panel-close', { type: 'button', 'aria-label': 'Close panel', title: 'Close panel (Esc)' });
  const closeIcon = el('span', 'icon i-x', { 'aria-hidden': 'true' });
  close.append(closeIcon);
  head.append(title, close);
  const content = el('div', 'side-panel-body', { tabindex: '-1' });
  panel.append(head, content);

  let returnFocus = null;
  let onClose = null;
  const sidebarEl = document.getElementById('sidebar');
  const mainEl = document.querySelector('main');

  // Wide: a complementary landmark beside the chat. Phone: a modal sheet, so
  // the rest of the page is inert until it closes. (Not a <dialog>: a dialog
  // is always announced as one, even where this acts as a side column.)
  function applyMode() {
    const sheet = !wide.matches && !panel.hidden;
    if (sheet) {
      panel.setAttribute('role', 'dialog');
      panel.setAttribute('aria-modal', 'true');
    } else {
      panel.removeAttribute('role');
      panel.removeAttribute('aria-modal');
    }
    for (const node of [sidebarEl, mainEl]) {
      if (!node) continue;
      if (sheet) node.setAttribute('inert', '');
      else node.removeAttribute('inert');
    }
  }

  function openPanel(options) {
    const opts = options || {};
    if (!panel.contains(document.activeElement)) returnFocus = opts.returnFocus || document.activeElement;
    if (typeof onClose === 'function') { try { onClose(); } catch (_) {} }
    onClose = typeof opts.onClose === 'function' ? opts.onClose : null;
    title.textContent = opts.title || 'Panel';
    panel.setAttribute('aria-label', title.textContent);
    content.replaceChildren();
    if (opts.node) content.append(opts.node);
    else if (opts.text !== undefined) content.textContent = String(opts.text);
    content.scrollTop = 0;
    panel.hidden = false;
    body.classList.add('panel-open');
    panelSplitter.node.hidden = !wide.matches;
    panelSplitter.refit();
    sidebarSplitter.refit();
    applyMode();
    content.focus({ preventScroll: true });
  }

  function closePanel() {
    if (panel.hidden) return;
    panel.hidden = true;
    body.classList.remove('panel-open');
    panelSplitter.node.hidden = true;
    content.replaceChildren();
    sidebarSplitter.refit();
    applyMode();
    const done = onClose;
    onClose = null;
    if (typeof done === 'function') { try { done(); } catch (_) {} }
    const target = returnFocus;
    returnFocus = null;
    // The opener can be gone (a streamed message re-renders); the composer is next best.
    if (target && target.isConnected && typeof target.focus === 'function') target.focus();
    else document.getElementById('prompt')?.focus();
  }

  close.addEventListener('click', closePanel);
  // Escape inside the panel closes it; it never reaches the app's Esc (stop the reply).
  panel.addEventListener('keydown', (event) => {
    if (event.key !== 'Escape' || event.isComposing) return;
    event.preventDefault();
    event.stopPropagation();
    closePanel();
  });

  // ---------- Wire up ----------
  const sidebarSplitter = splitter({
    id: 'sidebar-splitter',
    controls: 'sidebar',
    label: 'Resize sidebar',
    property: '--sidebar-w',
    minProperty: '--pane-sidebar-min',
    maxProperty: '--pane-sidebar-max',
    fallback: 272,
    key: KEYS.sidebar,
    direction: 1,
    other: () => (body.classList.contains('panel-open') ? panelSplitter.width() : 0),
  });
  const panelSplitter = splitter({
    id: 'side-panel-splitter',
    controls: 'side-panel',
    label: 'Resize side panel',
    property: '--pane-panel-w',
    minProperty: '--pane-panel-min',
    maxProperty: '--pane-panel-max',
    fallback: 480,
    key: KEYS.panel,
    direction: -1,
    other: () => (body.classList.contains('sidebar-collapsed') ? 0 : sidebarSplitter.width()),
  });
  panelSplitter.node.hidden = true;

  // The sidebar splitter only exists while the sidebar is a visible column.
  // Collapsing the sidebar also gives the panel more room to fit into.
  function syncSidebarSplitter() {
    sidebarSplitter.node.hidden = !wide.matches || body.classList.contains('sidebar-collapsed');
    if (!panel.hidden) panelSplitter.refit();
  }
  if (typeof MutationObserver === 'function') {
    new MutationObserver(syncSidebarSplitter).observe(body, { attributes: true, attributeFilter: ['class'] });
  }
  const onBreakpoint = () => {
    syncSidebarSplitter();
    panelSplitter.node.hidden = !wide.matches || panel.hidden;
    applyMode();
  };
  if (wide.addEventListener) wide.addEventListener('change', onBreakpoint);
  else if (wide.addListener) wide.addListener(onBreakpoint);

  // The sidebar is the first grid child, so the panel goes last, after <main>.
  body.append(sidebarSplitter.node, panelSplitter.node, panel);
  syncSidebarSplitter();

  window.KovaPanel = {
    open: openPanel,
    close: closePanel,
    isOpen: () => !panel.hidden,
  };
})();
