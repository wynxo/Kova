// Resizable three-pane layout (#182): sidebar | conversation | side panel.
//
// Self-contained classic script. It builds two splitters, the side panel and
// the collapsed sidebar's icon rail itself, so index.html and app.js need no
// new markup, and it exposes a small API other features use to show content
// beside the conversation:
//
//   window.KovaPanel.open({title, node, key, background, onClose, onSelect})
//                                       show `node` in a tab; the same `key`
//                                       refreshes that tab instead of adding one
//   window.KovaPanel.close()            hide the panel (its tabs stay); focus goes back
//   window.KovaPanel.toggle()           hide it, or show it again with its tabs
//   window.KovaPanel.closeTab(key)
//   window.KovaPanel.isOpen()
//   window.KovaPanes.focusNext() / focusPrevious()   move focus between panes
//
// A tab can be popped out into its own window and docked back. Widths are
// remembered per device in localStorage. The limits come from the --pane-*
// properties in panes.css, so CSS and JS cannot disagree. On phone widths
// there are no splitters and the panel is a full-screen sheet.
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

  // ---------- The side panel: one tab per thing opened in it ----------
  // Each tab keeps its own content node, so switching tabs keeps scroll
  // positions, expanded folds and anything a feature drew. Opening with a
  // `key` that is already open refreshes that tab instead of adding one.
  const MAX_TABS = 10;
  const panel = el('aside', 'side-panel', { id: 'side-panel', 'aria-label': 'Side panel' });
  panel.hidden = true;
  const head = el('header', 'side-panel-head');
  const tablist = el('div', 'side-panel-tabs', { role: 'tablist', 'aria-label': 'Open in the panel' });
  function iconButton(cls, icon, label, hint) {
    const node = el('button', `icon-button ${cls}`, { type: 'button', 'aria-label': label, title: hint || label });
    node.append(el('span', `icon ${icon}`, { 'aria-hidden': 'true' }));
    return node;
  }
  const popButton = iconButton('side-panel-popout', 'i-external-link', 'Open this tab in its own window');
  const closeTabButton = iconButton('side-panel-close-tab', 'i-x', 'Close this tab', 'Close this tab (Delete on a tab)');
  const hideButton = iconButton('side-panel-close', 'i-panel-right', 'Hide panel', 'Hide panel (Esc); its tabs stay open');
  head.append(tablist, popButton, closeTabButton, hideButton);
  const content = el('div', 'side-panel-body');
  panel.append(head, content);

  const tabs = [];        // docked tabs, in tab order
  const popped = [];      // tabs living in their own window
  let active = null;
  let serial = 0;
  let returnFocus = null;
  const sidebarEl = document.getElementById('sidebar');
  const mainEl = document.querySelector('main');

  function call(fn) { if (typeof fn === 'function') { try { fn(); } catch (_) { /* a feature's hook never breaks the panel */ } } }
  function notify(message) {
    if (window.KovaToast?.show) window.KovaToast.show(message, { severity: 'error', desktop: false });
  }

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
    for (const node of [sidebarEl, mainEl, rail]) {
      if (!node) continue;
      if (sheet) node.setAttribute('inert', '');
      else node.removeAttribute('inert');
    }
    popButton.hidden = !wide.matches || typeof window.open !== 'function';
  }

  function makeTab(key) {
    const id = ++serial;
    const tab = { id, key: key || null, title: '', onClose: null, onSelect: null, popup: null, heading: null };
    tab.button = el('button', 'side-panel-tab', {
      type: 'button', role: 'tab', id: `side-panel-tab-${id}`,
      'aria-controls': `side-panel-tabpanel-${id}`, 'aria-selected': 'false', tabindex: '-1',
    });
    tab.view = el('div', 'side-panel-tabpanel', {
      role: 'tabpanel', id: `side-panel-tabpanel-${id}`, 'aria-labelledby': `side-panel-tab-${id}`, tabindex: '0',
    });
    tab.view.hidden = true;
    tab.button.addEventListener('click', () => select(tab, true));
    // Middle-click closes a tab, as in a browser.
    tab.button.addEventListener('auxclick', (event) => {
      if (event.button !== 1) return;
      event.preventDefault();
      closeTab(tab, true);
    });
    return tab;
  }

  function fill(tab, opts) {
    call(tab.onClose);
    tab.onClose = typeof opts.onClose === 'function' ? opts.onClose : null;
    tab.onSelect = typeof opts.onSelect === 'function' ? opts.onSelect : null;
    tab.title = String(opts.title || 'Panel');
    tab.button.textContent = tab.title;
    tab.button.title = tab.title;
    tab.view.replaceChildren();
    if (opts.node) tab.view.append(opts.node);
    else if (opts.text !== undefined) tab.view.textContent = String(opts.text);
    tab.view.scrollTop = 0;
    if (tab.popup) {
      tab.view.setAttribute('aria-label', tab.title);
      tab.heading.textContent = tab.title;
      tab.popup.document.title = `${tab.title} · Kova`;
    }
  }

  function select(tab, focusTab) {
    active = tab;
    for (const item of tabs) {
      const on = item === tab;
      item.button.setAttribute('aria-selected', String(on));
      item.button.setAttribute('tabindex', on ? '0' : '-1');
      item.view.hidden = !on;
    }
    panel.setAttribute('aria-label', `Side panel: ${tab.title}`);
    tab.button.scrollIntoView?.({ block: 'nearest', inline: 'nearest' });
    if (focusTab) tab.button.focus();
    call(tab.onSelect);
  }

  function show() {
    if (!panel.hidden) return;
    panel.hidden = false;
    body.classList.add('panel-open');
    panelSplitter.node.hidden = !wide.matches;
    panelSplitter.refit();
    sidebarSplitter.refit();
    applyMode();
  }

  function hidePanel() {
    if (panel.hidden) return;
    const hadFocus = panel.contains(document.activeElement);
    panel.hidden = true;
    body.classList.remove('panel-open');
    panelSplitter.node.hidden = true;
    sidebarSplitter.refit();
    applyMode();
    const target = returnFocus;
    returnFocus = null;
    if (!hadFocus) return;
    // The opener can be gone (a streamed message re-renders); the composer is next best.
    if (target && target.isConnected && typeof target.focus === 'function') target.focus();
    else document.getElementById('prompt')?.focus();
  }

  // Take a tab out of the strip without running its onClose (closing or popping out).
  function detach(tab, focus) {
    const index = tabs.indexOf(tab);
    if (index < 0) return;
    tabs.splice(index, 1);
    tab.button.remove();
    tab.view.remove();
    if (active !== tab) return;
    active = null;
    const next = tabs[index] || tabs[index - 1];
    if (next) select(next, focus);
    else hidePanel();
  }

  function closeTab(tab, focus) {
    if (!tab) return;
    if (tab.popup) {
      const child = tab.popup;
      tab.popup = null;
      tab.heading = null;
      popped.splice(popped.indexOf(tab), 1);
      try { child.close(); } catch (_) {}
    }
    detach(tab, focus);
    const done = tab.onClose;
    tab.onClose = null;
    call(done);
  }

  function openPanel(options) {
    const opts = options || {};
    if (!panel.contains(document.activeElement)) returnFocus = opts.returnFocus || document.activeElement;
    // Already in its own window: refresh it there and bring that window forward.
    const away = opts.key ? popped.find((item) => item.key === opts.key) : null;
    if (away) {
      fill(away, opts);
      away.popup.focus();
      return away.id;
    }
    let tab = opts.key ? tabs.find((item) => item.key === opts.key) : null;
    if (!tab) {
      tab = makeTab(opts.key);
      tabs.push(tab);
      tablist.append(tab.button);
      content.append(tab.view);
      // Too many tabs: the oldest one that is not on screen goes.
      while (tabs.length > MAX_TABS) closeTab(tabs.find((item) => item !== tab && item !== active), false);
    }
    fill(tab, opts);
    const background = Boolean(opts.background) && active && !panel.hidden;
    show();
    if (background) return tab.id;
    select(tab, false);
    tab.view.focus({ preventScroll: true });
    return tab.id;
  }

  function togglePanel() {
    if (!panel.hidden) { hidePanel(); return true; }
    if (!active) return false;   // nothing has been opened in it yet
    if (!panel.contains(document.activeElement)) returnFocus = document.activeElement;
    show();
    select(active, true);
    return true;
  }

  hideButton.addEventListener('click', hidePanel);
  closeTabButton.addEventListener('click', () => closeTab(active, true));
  popButton.addEventListener('click', () => { if (active) popOut(active); });
  // Arrow keys, Home and End move between tabs; Delete closes the focused one.
  tablist.addEventListener('keydown', (event) => {
    const index = tabs.findIndex((item) => item.button === event.target);
    if (index < 0 || event.altKey || event.ctrlKey || event.metaKey) return;
    let next = null;
    if (event.key === 'ArrowRight') next = tabs[(index + 1) % tabs.length];
    else if (event.key === 'ArrowLeft') next = tabs[(index - 1 + tabs.length) % tabs.length];
    else if (event.key === 'Home') next = tabs[0];
    else if (event.key === 'End') next = tabs[tabs.length - 1];
    else if (event.key === 'Delete') {
      event.preventDefault();
      closeTab(tabs[index], true);
      return;
    }
    if (!next) return;
    event.preventDefault();
    select(next, true);
  });
  // Escape inside the panel hides it; it never reaches the app's Esc (stop the reply).
  panel.addEventListener('keydown', (event) => {
    if (event.key !== 'Escape' || event.isComposing) return;
    event.preventDefault();
    event.stopPropagation();
    hidePanel();
  });

  // ---------- Pop a tab out into its own window ----------
  // The window is a same-origin blank page: the tab's own node moves into it,
  // with its listeners, and the app's stylesheets and theme follow it. The
  // desktop app allows exactly these windows (desktop/core.js panelWindow).
  function copyLook(doc) {
    const target = doc.documentElement;
    for (const { name } of Array.from(target.attributes)) {
      if (name !== 'style' && !root.hasAttribute(name)) target.removeAttribute(name);
    }
    for (const { name, value } of Array.from(root.attributes)) {
      if (name !== 'style') target.setAttribute(name, value);
    }
    // Theme tokens are inline custom properties (theme.js). Copy them through
    // the CSSOM: a style attribute would need 'unsafe-inline' in the CSP.
    for (const name of Array.from(target.style)) {
      if (!root.style.getPropertyValue(name)) target.style.removeProperty(name);
    }
    for (const name of Array.from(root.style)) {
      target.style.setProperty(name, root.style.getPropertyValue(name), root.style.getPropertyPriority(name));
    }
  }

  function popOut(tab) {
    const width = Math.round(panelSplitter.width());
    const height = Math.round(window.innerHeight);
    let child = null;
    try { child = window.open('', `kova-panel-${tab.id}`, `popup=yes,width=${width},height=${height}`); } catch (_) { child = null; }
    let doc = null;
    try { doc = child && child.document; } catch (_) { doc = null; }
    if (!doc) {
      notify('Kova could not open a new window. Allow pop-ups for this page, then try again.');
      return false;
    }
    doc.title = `${tab.title} · Kova`;
    const charset = doc.createElement('meta');
    charset.setAttribute('charset', 'utf-8');
    doc.head.append(charset);
    for (const link of document.querySelectorAll('link[rel="stylesheet"]')) {
      const copy = doc.createElement('link');
      copy.rel = 'stylesheet';
      copy.href = link.href;
      doc.head.append(copy);
    }
    copyLook(doc);
    doc.body.className = 'panel-window';
    const bar = doc.createElement('header');
    bar.className = 'side-panel-head';
    const heading = doc.createElement('h1');
    heading.className = 'panel-window-title';
    heading.textContent = tab.title;
    const back = doc.createElement('button');
    back.type = 'button';
    back.className = 'btn small quiet';
    back.textContent = 'Back to panel';
    back.setAttribute('aria-label', `Move ${tab.title} back to the side panel`);
    bar.append(heading, back);
    const holder = doc.createElement('main');
    holder.className = 'side-panel-body';
    doc.body.replaceChildren(bar, holder);

    detach(tab, false);
    tab.view.hidden = false;
    // Its tab stays behind, so in the window it is a labelled region of its own.
    tab.view.setAttribute('role', 'region');
    tab.view.removeAttribute('aria-labelledby');
    tab.view.setAttribute('aria-label', tab.title);
    holder.append(doc.adoptNode(tab.view));
    tab.popup = child;
    tab.heading = heading;
    popped.push(tab);
    back.addEventListener('click', () => dock(tab));
    // Closing the window closes the tab. `closed` is polled because a blank
    // window does not always fire pagehide when the person closes it.
    const watch = window.setInterval(() => {
      if (tab.popup !== child) { window.clearInterval(watch); return; }
      if (!child.closed) return;
      window.clearInterval(watch);
      tab.popup = null;
      tab.heading = null;
      popped.splice(popped.indexOf(tab), 1);
      const done = tab.onClose;
      tab.onClose = null;
      call(done);
    }, 500);
    tab.view.focus({ preventScroll: true });
    child.focus();
    return true;
  }

  // Back from its window into the panel, as the selected tab.
  function dock(tab) {
    const child = tab.popup;
    if (!child) return;
    tab.popup = null;
    tab.heading = null;
    popped.splice(popped.indexOf(tab), 1);
    tab.view = document.adoptNode(tab.view);
    tab.view.setAttribute('role', 'tabpanel');
    tab.view.removeAttribute('aria-label');
    tab.view.setAttribute('aria-labelledby', tab.button.id);
    tabs.push(tab);
    tablist.append(tab.button);
    content.append(tab.view);
    try { child.close(); } catch (_) {}
    window.focus();
    show();
    select(tab, true);
    while (tabs.length > MAX_TABS) closeTab(tabs.find((item) => item !== tab), false);
  }

  if (typeof MutationObserver === 'function') {
    // A theme, motion or text-size change reaches the windows as well.
    new MutationObserver(() => {
      for (const tab of popped) { try { copyLook(tab.popup.document); } catch (_) {} }
    }).observe(root, { attributes: true });
  }
  window.addEventListener('pagehide', () => {
    for (const tab of popped.slice()) { try { tab.popup.close(); } catch (_) {} }
  });

  // ---------- The collapsed sidebar: an icon rail ----------
  // On wide screens a collapsed sidebar leaves this narrow column behind. Its
  // buttons drive the sidebar's own controls, so there is one behaviour each.
  function clickId(id) { document.getElementById(id)?.click(); }
  function railButton(icon, label, run, attrs) {
    const node = iconButton('sidebar-rail-button', icon, label);
    for (const [key, value] of Object.entries(attrs || {})) node.setAttribute(key, value);
    node.addEventListener('click', run);
    return node;
  }
  const rail = el('nav', 'sidebar-rail', { id: 'sidebar-rail', 'aria-label': 'Sidebar shortcuts' });
  const newChatKeys = document.getElementById('new-chat')?.getAttribute('aria-keyshortcuts');
  const expand = () => {
    if (body.classList.contains('sidebar-collapsed')) clickId('menu');
  };
  rail.append(
    railButton('i-panel-left', 'Show sidebar', () => { expand(); focusInto(sidebarEl); }),
    railButton('i-square-pen', 'New chat', () => clickId('new-chat'), newChatKeys ? { 'aria-keyshortcuts': newChatKeys } : null),
    railButton('i-search', 'Search conversations', () => {
      expand();
      const search = document.getElementById('search');
      search?.focus();
      search?.select?.();
    }),
    el('span', 'sidebar-rail-spacer', { 'aria-hidden': 'true' }),
    railButton('i-settings', 'Settings', () => clickId('open-settings')),
  );

  // ---------- Move focus between panes (F6 / Shift+F6 by default) ----------
  function visible(node) { return Boolean(node) && node.getClientRects().length > 0 && getComputedStyle(node).visibility !== 'hidden'; }
  function paneList() {
    if (!wide.matches && !panel.hidden) return [panel];     // the sheet is modal
    const list = [];
    if (visible(sidebarEl)) list.push(sidebarEl);
    else if (visible(rail)) list.push(rail);
    if (mainEl) list.push(mainEl);
    if (!panel.hidden) list.push(panel);
    return list;
  }
  function focusInto(pane) {
    if (!pane) return;
    let target = null;
    if (pane === panel) target = active ? active.button : hideButton;
    else if (pane === mainEl) target = document.getElementById('prompt');
    else if (pane === sidebarEl) {
      target = sidebarEl.querySelector('.history-row.active button, .history-row.active a') || document.getElementById('new-chat');
    } else if (pane === rail) target = rail.querySelector('button');
    if (!target || target.disabled || !visible(target)) {
      target = pane.querySelector('button:not([disabled]), [href], input, textarea, [tabindex="0"]');
    }
    target?.focus();
  }
  function focusPane(direction) {
    const list = paneList();
    if (!list.length) return false;
    const current = list.findIndex((pane) => pane.contains(document.activeElement));
    const next = current < 0
      ? list[direction > 0 ? 0 : list.length - 1]
      : list[(current + direction + list.length) % list.length];
    focusInto(next);
    return true;
  }

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
    other: () => (body.classList.contains('sidebar-collapsed') ? cssPx('--pane-rail-w', 48) : sidebarSplitter.width()),
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
  // The rail and the splitters are fixed, so they take no grid cell.
  body.append(rail, sidebarSplitter.node, panelSplitter.node, panel);
  syncSidebarSplitter();
  applyMode();

  window.KovaPanel = {
    open: openPanel,
    close: hidePanel,
    toggle: togglePanel,
    closeTab: (key) => closeTab(tabs.find((item) => item.key === key) || popped.find((item) => item.key === key), false),
    isOpen: () => !panel.hidden,
    tabCount: () => tabs.length,
  };
  window.KovaPanes = {
    focusNext: () => focusPane(1),
    focusPrevious: () => focusPane(-1),
  };
})();
