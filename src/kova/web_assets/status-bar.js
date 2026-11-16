/* Optional status bar for issue #204, first slice: active model + context meter.
 *
 * Hidden by default and quiet when idle: muted text only, no accent colour.
 * The data already exists on the client — the active model and its window come
 * from GET /models + /health, the used tokens are the same ~4-chars-a-token
 * estimate the terminal chat uses, and the per-answer usage objects come from
 * the cost/speed meter added for #75 (PR #217). This file adds no endpoint, no
 * fetch and no duplicate meter; app.js just passes what it already holds to
 * `window.KovaStatus.update`.
 */

(() => {
  'use strict';

  const STORE_KEY = 'kova-status-bar';
  const HIGH_RATIO = 0.8;
  const CHARS_PER_TOKEN = 4;

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

  function isShown() {
    try {
      return store ? store.getItem(STORE_KEY) === 'shown' : false;
    } catch (_) {
      return false;
    }
  }

  function paint(bar, { model, windowTokens, messages }) {
    const modelNode = $('status-bar-model');
    const contextNode = $('status-bar-context');
    const fill = $('status-bar-fill');
    const compact = $('status-bar-compact');
    if (!bar || !modelNode || !contextNode || !fill || !compact) return;

    const used = usedTokens(messages);
    const windowSize = Number(windowTokens) || 0;
    const name = model || 'No model';

    modelNode.textContent = name;
    if (windowSize > 0) {
      const ratio = used / windowSize;
      const percent = Math.min(100, Math.floor(ratio * 100));
      contextNode.textContent = `${shortCount(used)} of ${shortCount(windowSize)} · ${percent}%`;
      fill.style.width = `${percent}%`;
      bar.classList.toggle('is-high', ratio >= HIGH_RATIO);
      // Reuse the existing Compact action: no new endpoint, no duplicate flow.
      compact.hidden = ratio < HIGH_RATIO;
      bar.setAttribute('aria-label',
        `${name}: using about ${used} of ${windowSize} context tokens (${percent} percent).` +
        (ratio >= HIGH_RATIO ? ' Context is nearly full; compact to free space.' : ''));
    } else {
      contextNode.textContent = `${shortCount(used)} · window unknown`;
      fill.style.width = '0%';
      bar.classList.toggle('is-high', false);
      compact.hidden = true;
      bar.setAttribute('aria-label', `${name}: using about ${used} tokens; context window unknown.`);
    }
  }

  function setVisible(shown) {
    const bar = $('status-bar');
    const toggle = $('status-bar-toggle');
    if (toggle) toggle.checked = shown;
    if (bar) bar.hidden = !shown;
    try {
      if (store) {
        if (shown) store.setItem(STORE_KEY, 'shown');
        else store.removeItem(STORE_KEY);
      }
    } catch (_) { /* a private window must not break the composer */ }
    if (shown && typeof window !== 'undefined' && typeof window.KovaStatusRefresh === 'function') {
      try {
        window.KovaStatusRefresh();
      } catch (_) { /* the next controls() pass repaints anyway */ }
    }
  }

  function init() {
    const bar = $('status-bar');
    const toggle = $('status-bar-toggle');
    const compact = $('status-bar-compact');
    if (toggle) {
      toggle.checked = isShown();
      toggle.addEventListener('change', () => setVisible(toggle.checked));
    }
    if (compact) {
      compact.addEventListener('click', () => {
        const existing = $('compact-now');
        if (existing) existing.click();
      });
    }
    if (bar) bar.hidden = !isShown();
  }

  const api = {
    update(snapshot) {
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
