/* Reasoning display (#187): one calm line while the model thinks, then a
   collapsed "Thought for 12s" row that opens into the reasoning text.

   app.js keeps the existing thinking activity row (thinking.css) on screen
   while reasoning streams. When a burst ends it asks this module for the
   folded row, so the turn view never builds markup for the text itself and
   every string reaches the DOM as textContent, never as HTML.

   The preference has one switch, "shown" or "hidden", in Settings > General.
   It is saved in localStorage for an instant first paint and in the existing
   server-side UI preferences document, so it survives a new browser and
   `kova prefs export`. A browser that cannot reach the server keeps the local
   choice instead of losing it. */
(() => {
  'use strict';

  const KEY = 'kova-reasoning';
  const VALUES = ['shown', 'hidden'];
  const DEFAULT = 'shown';
  const root = document.documentElement;
  let value = stored();

  function stored() {
    try {
      const saved = localStorage.getItem(KEY);
      return VALUES.includes(saved) ? saved : DEFAULT;
    } catch (_) {
      return DEFAULT;
    }
  }

  function paintAttribute() {
    try { root?.setAttribute?.('data-reasoning', value); } catch (_) {}
  }

  function remember(next) {
    try { localStorage.setItem(KEY, next); } catch (_) {}
  }

  function announce() {
    try {
      if (typeof document.dispatchEvent === 'function' && typeof CustomEvent === 'function') {
        document.dispatchEvent(new CustomEvent('kova:reasoning', {detail: {shown: shown()}}));
      }
    } catch (_) {}
  }

  function request(url, options) {
    if (typeof fetch !== 'function') return null;
    try {
      return fetch(url, options);
    } catch (_) {
      return null;
    }
  }

  // The server document is the shared copy; the local value is the quick one.
  // The server wins when it has a valid value, because a preference changed in
  // another window should arrive here too.
  function adopt(payload) {
    const next = payload && payload.reasoning;
    if (!VALUES.includes(next) || next === value) return;
    value = next;
    remember(value);
    paintAttribute();
    announce();
  }

  function load() {
    const pending = request('/preferences', {headers: {'Accept': 'application/json'}});
    if (!pending || typeof pending.then !== 'function') return;
    pending
      .then(response => (response && response.ok ? response.json() : null))
      .then(payload => { if (payload) adopt(payload); })
      .catch(() => {});
  }

  function store(next) {
    if (!VALUES.includes(next) || next === value) return;
    value = next;
    remember(value);
    paintAttribute();
    announce();
    const pending = request('/preferences', {
      method: 'PUT',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({reasoning: value}),
    });
    if (pending && typeof pending.catch === 'function') pending.catch(() => {});
  }

  function shown() {
    return value !== 'hidden';
  }

  function setShown(visible) {
    store(visible ? 'shown' : 'hidden');
  }

  // Whole seconds, never "0s" and never a decimal that changes width while it
  // is read. A burst under half a second still took a moment.
  function seconds(span) {
    const ms = Math.max(0, Number(span) || 0);
    return Math.max(1, Math.round(ms / 1000));
  }

  // The collapsed row for one finished reasoning burst, or null when reasoning
  // is hidden or the model produced no text for it.
  function row(start, end, text) {
    if (!shown() || typeof text !== 'string' || !text.trim()) return null;
    const details = document.createElement('details');
    details.className = 'reasoning';
    const summary = document.createElement('summary');
    summary.className = 'reasoning-summary';
    summary.textContent = `Thought for ${seconds(end - start)}s`;
    const body = document.createElement('pre');
    body.className = 'reasoning-text';
    body.textContent = text;
    details.append(summary, body);
    return details;
  }

  paintAttribute();

  window.KovaReasoning = {KEY, shown, setShown, seconds, row};
  load();
})();
