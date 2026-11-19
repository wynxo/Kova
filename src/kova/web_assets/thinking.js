/* Thinking animation (#171): which small movement the app mark makes while a
   turn runs. Five animations share the mark — Orbit, Collide, Thunderstorm
   (the bolt), Breathe and Charge up — plus Off, and Random, which picks one
   per reply.

   The choice lives in the shared server UI preferences document, so every
   paired device and `kova prefs export` see it, and in localStorage for an
   instant first paint. thinking.css paints the movement from data-thinking on
   the mark; this module only decides the value and writes the attribute. */
(() => {
  'use strict';

  const KEY = 'kova-thinking-animation';
  const ANIMATIONS = ['thunderstorm', 'orbit', 'collide', 'breathe', 'charge'];
  const VALUES = ['off', ...ANIMATIONS, 'random'];
  const DEFAULT = 'thunderstorm';

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

  function request(url, options) {
    if (typeof fetch !== 'function') return null;
    try {
      return fetch(url, options);
    } catch (_) {
      return null;
    }
  }

  function announce() {
    try {
      if (typeof document.dispatchEvent === 'function' && typeof CustomEvent === 'function') {
        document.dispatchEvent(new CustomEvent('kova:thinking-animation', {
          detail: {value},
        }));
      }
    } catch (_) {}
  }

  // The server document is the shared copy; the local value is the quick one.
  // The server wins when it has a valid value, because a preference changed in
  // another window should arrive here too.
  function adopt(payload) {
    const next = known(payload && payload.thinking_animation);
    if (!next || next === value) return;
    value = next;
    remember(value);
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
    announce();
    const pending = request('/preferences', {
      method: 'PUT',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({thinking_animation: value}),
    });
    if (pending && typeof pending.catch === 'function') pending.catch(() => {});
  }

  /** The saved choice: "off", "random" or one animation's name. */
  function choice() {
    return value;
  }

  /** The animation this reply uses; "random" resolves to one of the five. */
  function pick() {
    if (value !== 'random') return value;
    return ANIMATIONS[Math.floor(Math.random() * ANIMATIONS.length)];
  }

  /** Choose this reply's animation on the activity mark, once per reply. */
  function apply(mark) {
    if (!mark || !mark.dataset) return;
    mark.dataset.thinking = pick();
  }

  function setChoice(next) {
    store(known(next) || DEFAULT);
  }

  window.KovaThinking = {KEY, ANIMATIONS, VALUES, choice, pick, apply, setChoice};
  load();
})();
