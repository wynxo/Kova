/* Theme engine, browser half (#174).

   The choice is one value: "system", "dark", "light", or the id of a theme file
   installed in Kova's home. It lives in the server-side UI preferences document,
   so every paired device and `kova prefs export` see the same theme, and in
   localStorage for an instant first paint.

   The built-in palettes are already in tokens.css, keyed by data-theme. A theme
   file is resolved on the server (GET /themes/<id>: its tokens merged over the
   base it extends) and written here into the semantic custom properties on
   <html>, so every component that reads var(--accent-default) follows it. This
   script never invents a colour: values come from tokens.css or the server, and
   anything that is not a semantic token name or a plain colour is dropped.

   It runs from <head>, before the body is parsed, so the first frame is already
   in the right theme; it touches only <html> and the theme-color meta tag. */
(() => {
  'use strict';

  const KEY = 'kova-theme';             // the choice (shared with the old toggle)
  const CACHE = 'kova-theme-tokens';    // the last resolved theme file, for first paint
  const SYNCED = 'kova-theme-synced';   // set once this browser has used the server copy
  const FIXED = ['system', 'dark', 'light'];
  const BUILTIN = {'kova-dark': 'dark', 'kova-light': 'light'};
  const ID = /^[a-z0-9][a-z0-9-]{1,39}$/;
  const TOKEN = /^(bg|fg|border|accent|status|syntax)\.[a-z0-9-]+$/;
  const COLOUR = /^(#[0-9a-fA-F]{3,8}|rgba?\([0-9.,%\s/]+\))$/;

  const root = document.documentElement;
  const media = typeof window.matchMedia === 'function'
    ? window.matchMedia('(prefers-color-scheme: light)') : null;

  let choice = 'system';
  let custom = null;        // {id, name, appearance, tokens} for the active theme file
  let applied = [];         // custom properties this module set, to clear them again
  let installed = [];       // valid theme files from GET /themes
  let generation = 0;       // drops a slow theme fetch that a newer choice replaced

  function read(key) {
    try { return localStorage.getItem(key); } catch (_) { return null; }
  }
  function write(key, value) {
    try {
      if (value === null) localStorage.removeItem(key);
      else localStorage.setItem(key, value);
    } catch (_) {}
  }

  function normalise(value) {
    if (typeof value !== 'string') return null;
    if (BUILTIN[value]) return BUILTIN[value];
    if (FIXED.includes(value) || ID.test(value)) return value;
    return null;
  }

  function request(url, options) {
    if (typeof fetch !== 'function') return null;
    try { return fetch(url, options); } catch (_) { return null; }
  }
  function getJson(url) {
    const pending = request(url, {headers: {'Accept': 'application/json'}});
    if (!pending || typeof pending.then !== 'function') return Promise.resolve(null);
    return pending.then(response => (response && response.ok ? response.json() : null)).catch(() => null);
  }

  function systemAppearance() {
    return media && media.matches ? 'light' : 'dark';
  }

  /** The appearance actually on screen: "dark" or "light". */
  function appearance() {
    if (choice === 'dark' || choice === 'light') return choice;
    if (custom && custom.id === choice) return custom.appearance;
    return systemAppearance();
  }

  // Only semantic tokens with plain colour values reach the style sheet. The
  // server already validated the file; this keeps the browser honest anyway.
  function cleanTokens(tokens) {
    const clean = {};
    if (!tokens || typeof tokens !== 'object') return clean;
    for (const [name, value] of Object.entries(tokens)) {
      if (TOKEN.test(name) && typeof value === 'string' && COLOUR.test(value.trim())) {
        clean[name] = value.trim();
      }
    }
    return clean;
  }
  function cleanTheme(payload) {
    if (!payload || typeof payload !== 'object' || !ID.test(payload.id || '')) return null;
    if (payload.appearance !== 'dark' && payload.appearance !== 'light') return null;
    return {
      id: payload.id,
      name: typeof payload.name === 'string' ? payload.name : payload.id,
      appearance: payload.appearance,
      tokens: cleanTokens(payload.tokens),
    };
  }

  const property = name => `--${name.replace('.', '-')}`;

  function clearProperties() {
    for (const name of applied) {
      try { root.style.removeProperty(name); } catch (_) {}
    }
    applied = [];
  }

  // The browser chrome (and the desktop shell's window) takes the page's base
  // colour. Built-in palettes are read back from tokens.css, already applied.
  function paintChrome() {
    const meta = document.getElementById && document.getElementById('theme-color');
    if (!meta) return;
    let colour = custom && custom.id === choice ? custom.tokens['bg.base'] : '';
    if (!colour) {
      try {
        colour = getComputedStyle(root).getPropertyValue('--bg-base').trim();
      } catch (_) {
        colour = '';
      }
    }
    if (colour) meta.setAttribute('content', colour);
  }

  function announce() {
    try {
      if (typeof document.dispatchEvent === 'function' && typeof CustomEvent === 'function') {
        document.dispatchEvent(new CustomEvent('kova:theme', {
          detail: {choice, appearance: appearance(), name: label(choice)},
        }));
      }
    } catch (_) {}
  }

  function paint() {
    clearProperties();
    const active = custom && custom.id === choice ? custom : null;
    root.dataset.theme = active ? active.appearance : appearance();
    if (active) {
      for (const [name, value] of Object.entries(active.tokens)) {
        const css = property(name);
        try { root.style.setProperty(css, value); applied.push(css); } catch (_) {}
      }
    }
    paintChrome();
    announce();
  }

  // A theme file needs its resolved tokens. The last one used is cached so a
  // reload paints it at once; the server copy then replaces it.
  function loadCustom(id) {
    const ticket = ++generation;
    return getJson(`/themes/${encodeURIComponent(id)}`).then(payload => {
      if (ticket !== generation || choice !== id) return;
      const theme = cleanTheme(payload);
      if (!theme || theme.id !== id) {
        // Missing or broken here (not installed on this PC yet, say): follow
        // the device until it can be loaded, and keep the saved choice.
        custom = null;
        write(CACHE, null);
        paint();
        return;
      }
      custom = theme;
      write(CACHE, JSON.stringify(theme));
      paint();
    });
  }

  function adopt(next, {save = false} = {}) {
    const value = normalise(next);
    if (!value) return Promise.resolve();
    const changed = value !== choice;
    choice = value;
    write(KEY, value);
    if (save) {
      const pending = request('/preferences', {
        method: 'PUT',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({theme: value}),
      });
      if (pending && typeof pending.catch === 'function') pending.catch(() => {});
    }
    if (FIXED.includes(value)) {
      generation += 1;
      custom = null;
      write(CACHE, null);
      paint();
      return Promise.resolve();
    }
    if (changed && custom && custom.id !== value) custom = null;
    paint();
    return loadCustom(value);
  }

  /** Choose a theme: "system", "dark", "light" or an installed theme id. */
  function set(next) {
    return adopt(next, {save: true});
  }

  /** The light/dark shortcut: always lands on the built-in opposite of what is shown. */
  function toggle() {
    return set(appearance() === 'light' ? 'dark' : 'light');
  }

  function label(value) {
    if (value === 'system') return 'System';
    if (value === 'dark') return 'Dark';
    if (value === 'light') return 'Light';
    const found = installed.find(theme => theme.id === value) || (custom && custom.id === value ? custom : null);
    return found ? found.name : value;
  }

  /** Every choice a picker offers: the three fixed ones, then installed theme files. */
  function choices() {
    return [
      {id: 'system', name: 'System', appearance: systemAppearance(), source: 'built-in'},
      {id: 'light', name: 'Light', appearance: 'light', source: 'built-in'},
      {id: 'dark', name: 'Dark', appearance: 'dark', source: 'built-in'},
      ...installed.map(theme => ({...theme, source: 'file'})),
    ];
  }

  /** Re-read the installed theme files from the server. */
  function refresh() {
    return getJson('/themes').then(list => {
      if (!Array.isArray(list)) return choices();
      installed = list
        .filter(entry => entry && !entry.error && !BUILTIN[entry.id] && ID.test(entry.id || '')
          && (entry.appearance === 'dark' || entry.appearance === 'light'))
        .map(entry => ({
          id: entry.id,
          name: typeof entry.name === 'string' && entry.name ? entry.name : entry.id,
          appearance: entry.appearance,
        }));
      announce();
      return choices();
    });
  }

  // First paint: the local copy, and the cached theme file if it is the one chosen.
  choice = normalise(read(KEY)) || 'system';
  if (!FIXED.includes(choice)) {
    try {
      const cached = cleanTheme(JSON.parse(read(CACHE) || 'null'));
      if (cached && cached.id === choice) custom = cached;
    } catch (_) {}
  }
  paint();
  if (!FIXED.includes(choice)) loadCustom(choice);

  // The device's light/dark setting only matters while "system" is chosen.
  const onSystem = () => { if (choice === 'system') paint(); };
  if (media && typeof media.addEventListener === 'function') media.addEventListener('change', onSystem);
  else if (media && typeof media.addListener === 'function') media.addListener(onSystem);

  // The server copy is shared by every device. A browser that chose a theme
  // before the choice synced (only "kova-theme" in localStorage) sends its own
  // choice up once, but only while the server still has the default: a choice
  // another device already made wins.
  function sync() {
    return getJson('/preferences').then(payload => {
      const remote = normalise(payload && payload.theme);
      if (!remote) return;
      if (!read(SYNCED)) {
        const local = normalise(read(KEY));
        write(SYNCED, '1');
        if (local && local !== remote && remote === 'system') return adopt(local, {save: true});
      }
      if (remote !== choice) return adopt(remote);
    });
  }

  window.KovaTheme = {KEY, appearance, choice: () => choice, choices, label, refresh, set, sync, toggle};
  sync();
  refresh();
})();
