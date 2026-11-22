/* Motion preference (#181): Full, Reduced or Off, as one attribute on <html>.

   motion.css turns <html data-motion> into the whole system — shorter duration
   tokens, one-shot animations, or no movement at all — so no component needs a
   rule of its own. This module only decides the value.

   The choice lives in the shared server UI preferences document, so every
   paired device and `kova prefs export` see it, and in localStorage for an
   instant first paint. "Full" is the default and follows the device: a system
   that asks for reduced motion (prefers-reduced-motion) resolves it to
   "reduced". An explicit Reduced or Off always wins.

   It runs from <head>, before the body is parsed, so the first frame is already
   still when it should be; it touches only <html>. */
(() => {
  'use strict';

  const KEY = 'kova-motion';
  const VALUES = ['full', 'reduced', 'off'];
  const DEFAULT = 'full';
  const root = document.documentElement;
  const query = typeof window.matchMedia === 'function'
    ? window.matchMedia('(prefers-reduced-motion: reduce)') : null;

  let value = stored();

  function known(candidate) {
    return VALUES.includes(candidate) ? candidate : null;
  }

  function stored() {
    try {
      return known(localStorage.getItem(KEY)) || DEFAULT;
    } catch (_) {
      return DEFAULT;
    }
  }

  function remember(next) {
    try { localStorage.setItem(KEY, next); } catch (_) {}
  }

  function systemReduced() {
    return Boolean(query && query.matches);
  }

  // What the stylesheet acts on: Full defers to the device, the other two are
  // absolute, so a reader can always ask for less motion but never be forced
  // into more than the system allows.
  function effective() {
    return value === 'full' && systemReduced() ? 'reduced' : value;
  }

  function paint() {
    try { root?.setAttribute?.('data-motion', effective()); } catch (_) {}
  }

  function announce() {
    try {
      if (typeof document.dispatchEvent === 'function' && typeof CustomEvent === 'function') {
        document.dispatchEvent(new CustomEvent('kova:motion', {
          detail: {mode: value, applied: effective()},
        }));
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
    const next = known(payload && payload.motion);
    if (!next || next === value) return;
    value = next;
    remember(value);
    paint();
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
    paint();
    announce();
    const pending = request('/preferences', {
      method: 'PUT',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({motion: value}),
    });
    if (pending && typeof pending.catch === 'function') pending.catch(() => {});
  }

  /** The saved choice: "full", "reduced" or "off". */
  function mode() {
    return value;
  }

  /** What is on screen now, after the device's reduced-motion preference. */
  function applied() {
    return effective();
  }

  function setMode(next) {
    store(known(next) || DEFAULT);
  }

  paint();
  // The device preference can change while the window is open.
  if (query) {
    if (typeof query.addEventListener === 'function') query.addEventListener('change', paint);
    else if (typeof query.addListener === 'function') query.addListener(paint);
  }

  // A window opened from `kova web` (or a pairing URL) reaches this script
  // before app.js has exchanged its one-use token for a session cookie, so the
  // shared document is not readable yet. Keep the local copy for this first
  // frame; app.js calls sync() once the window is signed in.
  const loginPending = () => typeof location === 'object' &&
    /(^|[#&])(ticket|pair)=/.test(location.hash || '');

  window.KovaMotion = {KEY, mode, applied, setMode, sync: load};
  if (!loginPending()) load();
})();
