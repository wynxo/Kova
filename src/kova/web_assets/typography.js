/* Typography, density and content width (#179, #205): one attribute each on
   <html>.

   The stylesheet does the work in one block: `data-text-size` swaps the
   --type-size-* tokens, `data-density` the --space-* tokens, `data-font` the
   font stack and `data-chat-width` the --column the conversation, composer and
   start screen share, so no component needs a rule of its own. This module only
   decides the four values.

   The choices live in the shared server UI preferences document, so every
   paired device and `kova prefs export` see them, and in localStorage for an
   instant first paint. "default", "inter" and "normal" are the defaults and
   leave the shipped token values alone, so an untouched install looks exactly
   as before.

   It runs from <head>, before the body is parsed, so the first frame is already
   at the right size and density; it touches only <html>. */

(() => {
  'use strict';

  // One entry per preference, in the order the settings page shows them.
  const PREFS = [
    {key: 'kova-text-size', name: 'text_size', attr: 'data-text-size',
     values: ['small', 'default', 'large'], fallback: 'default'},
    {key: 'kova-density', name: 'density', attr: 'data-density',
     values: ['compact', 'default', 'comfortable'], fallback: 'default'},
    {key: 'kova-font', name: 'font', attr: 'data-font',
     values: ['inter', 'system', 'mono'], fallback: 'inter'},
    // Room around the conversation, then the two reading details (#205).
    {key: 'kova-chat-width', name: 'chat_width', attr: 'data-chat-width',
     theme: '--chat-width', values: ['narrow', 'normal', 'wide'], fallback: 'normal'},
    {name: 'avatars', key: 'kova-avatars', attr: 'data-avatars',
     theme: '--chat-avatars', values: ['on', 'off'], fallback: 'on'},
    {name: 'message_style', key: 'kova-message-style', attr: 'data-message-style',
     theme: '--chat-message-style', values: ['document', 'bubbles', 'compact'], fallback: 'document'},
    {name: 'message_timestamps', key: 'kova-message-timestamps', attr: 'data-message-timestamps',
     values: ['never', 'hover', 'always'], fallback: 'never'},
    // Theme backgrounds (#206): on by default, so a theme's gradient or image
    // shows. Off keeps the same theme and leaves the panels solid.
    {name: 'backgrounds', key: 'kova-backgrounds', attr: 'data-backgrounds',
     values: ['on', 'off'], fallback: 'on'},
  ];

  const root = document.documentElement;
  const state = {};
  let revision = 0;

  function known(pref, candidate) {
    return pref.values.includes(candidate) || (pref.theme && candidate === 'theme') ? candidate : null;
  }

  for (const pref of PREFS) {
    let stored = null;
    try { stored = known(pref, localStorage.getItem(pref.key)); } catch (_) {}
    state[pref.name] = stored || (pref.theme ? 'theme' : pref.fallback);
  }

  function remember(pref) {
    try { localStorage.setItem(pref.key, state[pref.name]); } catch (_) {}
  }

  function paint(pref) {
    let value = state[pref.name];
    if (value === 'theme') {
      try { value = getComputedStyle(root).getPropertyValue(pref.theme).trim(); } catch (_) {}
      if (!pref.values.includes(value)) value = pref.fallback;
    }
    root?.setAttribute?.(pref.attr, value);
  }

  function paintChatTheme() {
    for (const target of PREFS) if (target.theme) paint(target);
    const shape = typeof getComputedStyle === 'function'
      ? getComputedStyle(root).getPropertyValue('--chat-bubble-shape').trim() : '';
    root?.setAttribute?.('data-bubble-shape', shape === 'sharp' ? 'sharp' : 'round');
  }

  function announce() {
    try {
      if (typeof document.dispatchEvent === 'function' && typeof CustomEvent === 'function') {
        document.dispatchEvent(new CustomEvent('kova:typography', {
          detail: {textSize: state.text_size, density: state.density, font: state.font},
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
    let changed = false;
    for (const pref of PREFS) {
      const next = known(pref, payload && payload[pref.name]);
      if (!next || next === state[pref.name]) continue;
      state[pref.name] = next;
      remember(pref);
      paint(pref);
      changed = true;
    }
    if (changed) announce();
  }

  function sync() {
    const requested = ++revision;
    const pending = request('/preferences', {headers: {'Accept': 'application/json'}});
    if (!pending || typeof pending.then !== 'function') return;
    return pending
      .then(response => (response && response.ok ? response.json() : null))
      .then(payload => { if (payload && requested === revision) adopt(payload); })
      .catch(() => {});
  }

  function pref(name) {
    return PREFS.find(candidate => candidate.name === name);
  }

  function store(name, next) {
    const target = pref(name);
    if (!target) return;
    const value = known(target, next);
    if (!value || value === state[name]) return;
    revision += 1;
    state[name] = value;
    remember(target);
    paint(target);
    announce();
    const pending = request('/preferences', {
      method: 'PUT',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({[name]: value}),
    });
    if (pending && typeof pending.catch === 'function') pending.catch(() => {});
  }

  for (const target of PREFS) paint(target);
  paintChatTheme();
  document.addEventListener?.('kova:theme', paintChatTheme);

  window.KovaTypography = {
    sync,
    textSize: () => state.text_size,
    density: () => state.density,
    font: () => state.font,
    chatWidth: () => state.chat_width,
    avatars: () => state.avatars,
    messageStyle: () => state.message_style,
    messageTimestamps: () => state.message_timestamps,
    backgrounds: () => state.backgrounds,
    setTextSize: next => store('text_size', next),
    setDensity: next => store('density', next),
    setFont: next => store('font', next),
    setChatWidth: next => store('chat_width', next),
    setAvatars: next => store('avatars', next),
    setMessageStyle: next => store('message_style', next),
    setMessageTimestamps: next => store('message_timestamps', next),
    setBackgrounds: next => store('backgrounds', next),
  };
  sync();
})();
