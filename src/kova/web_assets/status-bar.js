/* Optional status bar for issue #204: active model, context meter, speed,
 * connection and phone. Each item has its own switch, and the whole bar can
 * sit under the message box or at the window's bottom edge.
 *
 * Hidden by default and quiet when idle: muted text only, no accent colour.
 * The data already exists on the client — the active model and its window come
 * from GET /models + /health, the used tokens are the same ~4-chars-a-token
 * estimate the terminal chat uses, the speed is the last answer's own
 * `tokens_per_second` (the cost/speed object added for #75, PR #217), and the
 * connection and phone facts come from the page itself. This file adds no
 * endpoint, no fetch and no duplicate meter; app.js just passes a snapshot to
 * `window.KovaStatus.update`.
 *
 * The choices are this window's, kept in localStorage beside the original
 * shown/hidden value: the bar has to paint before the server answers, and it
 * has to keep working when the server cannot be reached at all.
 */

(() => {
  'use strict';

  const STORE_KEY = 'kova-status-bar';
  const ITEMS_KEY = 'kova-status-bar-items';
  const PLACEMENT_KEY = 'kova-status-bar-placement';
  const HIGH_RATIO = 0.8;
  const CHARS_PER_TOKEN = 4;

  // One switch per item, in the order the bar reads left to right. All on: the
  // bar is already opt-in, so its first paint is the whole row, and Settings is
  // where a reader takes a part out again.
  const ITEMS = ['model', 'context', 'speed', 'connection', 'phone'];
  const DEFAULT_ITEMS = {model: true, context: true, speed: true, connection: true, phone: true};
  const PLACEMENTS = ['composer', 'edge'];
  const DEFAULT_PLACEMENT = 'composer';

  // The elements each switch owns. The meter and its numbers are one item.
  const NODES = {
    model: ['status-bar-model'],
    context: ['status-bar-meter', 'status-bar-context'],
    speed: ['status-bar-speed'],
    connection: ['status-bar-connection'],
    phone: ['status-bar-phone'],
  };

  const doc = typeof document !== 'undefined' ? document : null;
  const store = (() => {
    try {
      if (typeof localStorage !== 'undefined') return localStorage;
    } catch (_) { /* storage unavailable: the bar simply stays hidden */ }
    return null;
  })();

  function $(id) {
    return doc ? doc.getElementById(id) : null;
  }

  // Same k/M thresholds as the per-answer meter in app.js (`shortTokens`, #217).
  function shortCount(count) {
    count = Number(count) || 0;
    if (count < 1000) return String(count);
    if (count < 10000) return `${(count / 1000).toFixed(1).replace(/\.0$/, '')}k`;
    if (count < 1e6) return `${Math.round(count / 1000)}k`;
    return `${(count / 1e6).toFixed(1).replace(/\.0$/, '')}M`;
  }

  function usedTokens(messages) {
    let chars = 0;
    for (const message of messages || []) {
      if (message && typeof message.content === 'string') chars += message.content.length;
    }
    return Math.round(chars / CHARS_PER_TOKEN);
  }

  // The speed of the most recent answer that measured one. History keeps the
  // same usage object the per-answer meter reads, so a reload still shows it.
  function lastSpeed(messages) {
    const list = messages || [];
    for (let index = list.length - 1; index >= 0; index -= 1) {
      const usage = list[index] && list[index].usage;
      const speed = Number(usage && usage.tokens_per_second);
      if (speed > 0) return speed;
    }
    return 0;
  }

  function isShown() {
    try {
      return store ? store.getItem(STORE_KEY) === 'shown' : false;
    } catch (_) {
      return false;
    }
  }

  function readItems() {
    const items = {...DEFAULT_ITEMS};
    try {
      const raw = store ? store.getItem(ITEMS_KEY) : null;
      const saved = raw ? JSON.parse(raw) : null;
      if (saved && typeof saved === 'object') {
        for (const name of ITEMS) {
          if (typeof saved[name] === 'boolean') items[name] = saved[name];
        }
      }
    } catch (_) { /* an older or broken value means every item is on */ }
    return items;
  }

  function readPlacement() {
    try {
      const saved = store ? store.getItem(PLACEMENT_KEY) : null;
      return PLACEMENTS.includes(saved) ? saved : DEFAULT_PLACEMENT;
    } catch (_) {
      return DEFAULT_PLACEMENT;
    }
  }

  function remember(key, value) {
    try {
      if (store) store.setItem(key, String(value));
    } catch (_) { /* a private window must not break the composer */ }
  }

  function forget(key) {
    try {
      if (store) store.removeItem(key);
    } catch (_) { /* a private window must not break the composer */ }
  }

  // Show or hide every item's elements and put the bar where the reader asked
  // for it. Called at start, on every change, and whenever the bar is shown.
  function applyAppearance() {
    const bar = $('status-bar');
    const items = readItems();
    for (const name of ITEMS) {
      for (const id of NODES[name]) {
        const node = $(id);
        if (node) node.hidden = !items[name];
      }
    }
    if (!bar) return;
    const placement = readPlacement();
    bar.setAttribute('data-placement', placement);
    if (doc && doc.body && doc.body.classList) {
      // Only a visible bar at the edge needs room kept for it.
      doc.body.classList.toggle('status-bar-edge', placement === 'edge' && isShown());
    }
  }

  function paint(bar, {model, windowTokens, messages, connected, phone, tokensPerSecond}) {
    const modelNode = $('status-bar-model');
    const meter = $('status-bar-meter');
    const contextNode = $('status-bar-context');
    const fill = $('status-bar-fill');
    const compact = $('status-bar-compact');
    const speedNode = $('status-bar-speed');
    const connectionNode = $('status-bar-connection');
    const phoneNode = $('status-bar-phone');
    if (!bar || !modelNode || !contextNode || !fill || !compact) return;

    const items = readItems();
    const used = usedTokens(messages);
    const windowSize = Number(windowTokens) || 0;
    const name = model || 'No model';
    const speed = Math.round(Number(tokensPerSecond) || lastSpeed(messages));
    const online = connected !== false;
    const summary = [`${name}:`];

    modelNode.textContent = name;
    if (windowSize > 0) {
      const ratio = used / windowSize;
      const percent = Math.min(100, Math.floor(ratio * 100));
      contextNode.textContent = `${shortCount(used)} of ${shortCount(windowSize)} · ${percent}%`;
      fill.style.width = `${percent}%`;
      bar.classList.toggle('is-high', ratio >= HIGH_RATIO);
      // Reuse the existing Compact action: no new endpoint, no duplicate flow.
      compact.hidden = ratio < HIGH_RATIO || !items.context;
      summary.push(`using about ${used} of ${windowSize} context tokens (${percent} percent).`);
      if (ratio >= HIGH_RATIO) summary.push('Context is nearly full; compact to free space.');
    } else {
      contextNode.textContent = `${shortCount(used)} · window unknown`;
      fill.style.width = '0%';
      bar.classList.toggle('is-high', false);
      compact.hidden = true;
      summary.push(`using about ${used} tokens; context window unknown.`);
    }
    if (meter) {
      // The numbers are already on screen; the title adds the exact figures for
      // a pointer resting on the meter.
      meter.title = windowSize > 0
        ? `${used} of ${windowSize} context tokens used`
        : `${used} tokens used; the context window is unknown`;
    }

    if (speedNode) {
      speedNode.textContent = speed > 0 ? `${speed} tok/s` : '';
      speedNode.hidden = !items.speed || speed <= 0;
      if (!speedNode.hidden) summary.push(`The last reply ran at about ${speed} tokens per second.`);
    }
    if (connectionNode) {
      connectionNode.textContent = online ? 'Connected' : 'Offline';
      connectionNode.classList.toggle('is-offline', !online);
      connectionNode.hidden = !items.connection;
      if (!connectionNode.hidden) summary.push(online ? 'Connected to Kova.' : 'Kova is offline.');
    }
    if (phoneNode) {
      phoneNode.textContent = 'Phone';
      phoneNode.title = 'This window is a paired phone';
      phoneNode.hidden = !items.phone || !phone;
      if (!phoneNode.hidden) summary.push('This window is a paired phone.');
    }
    bar.setAttribute('aria-label', summary.join(' '));
  }

  function syncSettings(shown) {
    const fieldset = $('status-bar-items');
    if (fieldset) fieldset.hidden = !shown;
  }

  function refresh() {
    if (typeof window !== 'undefined' && typeof window.KovaStatusRefresh === 'function') {
      try {
        window.KovaStatusRefresh();
      } catch (_) { /* the next controls() pass repaints anyway */ }
    }
  }

  function setVisible(shown) {
    const bar = $('status-bar');
    const toggle = $('status-bar-toggle');
    if (toggle) toggle.checked = shown;
    if (bar) bar.hidden = !shown;
    if (shown) remember(STORE_KEY, 'shown');
    else forget(STORE_KEY);
    applyAppearance();
    syncSettings(shown);
    if (shown) refresh();
  }

  function setItem(name, on) {
    if (!ITEMS.includes(name)) return;
    const items = readItems();
    items[name] = Boolean(on);
    remember(ITEMS_KEY, JSON.stringify(items));
    applyAppearance();
    const box = $(`status-bar-item-${name}`);
    if (box) box.checked = items[name];
    refresh();
  }

  function setPlacement(placement) {
    if (!PLACEMENTS.includes(placement)) return;
    remember(PLACEMENT_KEY, placement);
    applyAppearance();
    const select = $('status-bar-placement');
    if (select) select.value = placement;
  }

  function init() {
    const bar = $('status-bar');
    const toggle = $('status-bar-toggle');
    const compact = $('status-bar-compact');
    const items = readItems();
    if (toggle) {
      toggle.checked = isShown();
      toggle.addEventListener('change', () => setVisible(toggle.checked));
    }
    for (const name of ITEMS) {
      const box = $(`status-bar-item-${name}`);
      if (!box) continue;
      box.checked = items[name];
      box.addEventListener('change', () => setItem(name, box.checked));
    }
    const placement = $('status-bar-placement');
    if (placement) {
      placement.value = readPlacement();
      placement.addEventListener('change', () => setPlacement(placement.value));
    }
    if (compact) {
      compact.addEventListener('click', () => {
        const existing = $('compact-now');
        if (existing) existing.click();
      });
    }
    if (bar) bar.hidden = !isShown();
    applyAppearance();
    syncSettings(isShown());
  }

  const api = {
    update(snapshot) {
      // The context ring next to the model reads the same figures, shown or not.
      if (globalThis.KovaShell) {
        const s = snapshot || {};
        globalThis.KovaShell.paintRing({used: usedTokens(s.messages), windowSize: Number(s.windowTokens) || 0});
      }
      if (!isShown()) return;
      const bar = $('status-bar');
      if (bar) {
        bar.hidden = false;
        paint(bar, snapshot || {});
      }
    },
    setVisible,
    isVisible: isShown,
    // Tested seams; not part of the UI contract.
    _usedTokens: usedTokens,
    _shortCount: shortCount,
    _lastSpeed: lastSpeed,
    _highRatio: HIGH_RATIO,
  };

  if (typeof window !== 'undefined') {
    window.KovaStatus = api;
  } else if (typeof globalThis !== 'undefined') {
    globalThis.KovaStatus = api;
  }

  if (doc) {
    if (doc.readyState === 'loading') doc.addEventListener('DOMContentLoaded', init);
    else init();
  }
})();
