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
  const BACKGROUND_FILE = /^[a-z0-9][a-z0-9._-]{0,63}\.(?:png|jpg|webp)$/;
  const CHAT_TOKENS = {
    'chat.width': ['narrow', 'normal', 'wide'],
    'chat.message-style': ['document', 'bubbles', 'compact'],
    'chat.avatars': ['on', 'off'],
    'chat.zen': ['on', 'off'],
    'chat.bubble-shape': ['round', 'sharp'],
  };

  const root = document.documentElement;
  const media = typeof window.matchMedia === 'function'
    ? window.matchMedia('(prefers-color-scheme: light)') : null;

  let choice = 'system';
  let draft = null;         // live editor preview; never written to preferences or cache
  let custom = null;        // {id, name, appearance, tokens} for the active theme file
  let applied = [];         // custom properties this module set, to clear them again
  let backgroundApplied = []; // the background properties, cleared on every repaint
  let installed = [];       // valid theme files from GET /themes
  let generation = 0;       // drops a slow theme fetch that a newer choice replaced
  let previewing = null;    // the choice on screen before preview(), null when none

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
    if (Object.hasOwn(BUILTIN, value)) return BUILTIN[value];
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
    if (draft) return draft.appearance;
    if (choice === 'dark' || choice === 'light') return choice;
    if (custom && custom.id === choice) return custom.appearance;
    return systemAppearance();
  }

  // Only semantic tokens with plain colours or known chat values reach CSS. The
  // server already validated the file; this keeps the browser honest anyway.
  function cleanTokens(tokens) {
    const clean = {};
    if (!tokens || typeof tokens !== 'object') return clean;
    for (const [name, value] of Object.entries(tokens)) {
      if (TOKEN.test(name) && typeof value === 'string' && COLOUR.test(value.trim())) {
        clean[name] = value.trim();
      } else if (Object.hasOwn(CHAT_TOKENS, name) && CHAT_TOKENS[name].includes(value)) {
        clean[name] = value;
      }
    }
    return clean;
  }
  // A background is decorative data from the server, not CSS: a gradient of
  // two to four plain colours, or one image file in themes/assets. Anything
  // else is dropped, as with tokens.
  function cleanBackground(value) {
    if (!value || typeof value !== 'object') return null;
    const number = (candidate, low, high, fallback) =>
      (typeof candidate === 'number' && Number.isFinite(candidate) && candidate >= low && candidate <= high
        ? candidate : fallback);
    if (value.kind === 'gradient') {
      const stops = Array.isArray(value.stops) ? value.stops : [];
      const clean = stops.filter(stop => typeof stop === 'string' && COLOUR.test(stop.trim()))
        .map(stop => stop.trim());
      if (clean.length < 2 || clean.length > 4) return null;
      return {kind: 'gradient', stops: clean, angle: number(value.angle, 0, 360, 160)};
    }
    if (value.kind === 'image' && typeof value.file === 'string' && BACKGROUND_FILE.test(value.file)) {
      return {
        kind: 'image',
        file: value.file,
        dim: number(value.dim, 0, 0.8, 0.35),
        blur: number(value.blur, 0, 24, 0),
      };
    }
    return null;
  }
  function cleanTheme(payload) {
    if (!payload || typeof payload !== 'object' || !ID.test(payload.id || '')) return null;
    if (payload.appearance !== 'dark' && payload.appearance !== 'light') return null;
    return {
      id: payload.id,
      name: typeof payload.name === 'string' ? payload.name : payload.id,
      appearance: payload.appearance,
      tokens: cleanTokens(payload.tokens),
      background: cleanBackground(payload.background),
    };
  }

  const property = name => `--${name.replace('.', '-')}`;

  function clearProperties() {
    for (const name of applied) {
      try { root.style.removeProperty(name); } catch (_) {}
    }
    applied = [];
  }

  function setBackgroundProperty(name, value) {
    try {
      root.style.setProperty(name, value);
      backgroundApplied.push(name);
    } catch (_) {}
  }

  function clearBackground() {
    for (const name of backgroundApplied) {
      try { root.style.removeProperty(name); } catch (_) {}
    }
    backgroundApplied = [];
    try { delete root.dataset.background; } catch (_) {}
  }

  // The stylesheet draws one fixed layer behind the shell from these values.
  // An image is addressed through the authenticated assets route, never a
  // user-supplied URL; the file name was already checked above.
  function paintBackground(active) {
    clearBackground();
    const background = active && active.background;
    if (!background) return;
    if (background.kind === 'gradient') {
      const stops = background.stops.join(', ');
      setBackgroundProperty('--theme-background', `linear-gradient(${background.angle}deg, ${stops})`);
    } else {
      const url = `/themes/assets/${encodeURIComponent(background.file)}`;
      setBackgroundProperty('--theme-background', `url("${url}")`);
      setBackgroundProperty('--theme-background-dim', String(background.dim));
      setBackgroundProperty('--theme-background-blur', `${background.blur}px`);
    }
    try { root.dataset.background = background.kind; } catch (_) {}
  }

  // The browser chrome (and the desktop shell's window) takes the page's base
  // colour. Built-in palettes are read back from tokens.css, already applied.
  function paintChrome(active) {
    const meta = document.getElementById && document.getElementById('theme-color');
    if (!meta) return;
    let colour = active?.tokens['bg.base'];
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
          detail: {choice, appearance: appearance(), name: label(choice), preview: !!draft || previewing !== null},
        }));
      }
    } catch (_) {}
  }

  function paint() {
    clearProperties();
    const active = draft || (custom?.id === choice ? custom : null);
    root.dataset.theme = appearance();
    paintBackground(active);
    if (active) {
      for (const [name, value] of Object.entries(active.tokens)) {
        const css = property(name);
        try { root.style.setProperty(css, value); applied.push(css); } catch (_) {}
      }
    }
    paintChrome(active);
    announce();
  }

  function previewPalette(palette) {
    if (palette.appearance !== 'light' && palette.appearance !== 'dark') return;
    draft = {appearance: palette.appearance, tokens: cleanTokens(palette.tokens)};
    paint();
  }

  function clearPalettePreview() {
    if (!draft) return;
    draft = null;
    paint();
  }

  function detailUrl(id) {
    // The gallery route also covers the pre-existing theme ID "gallery".
    return `/themes/${encodeURIComponent(id)}${id === 'gallery' ? '?installed=true' : ''}`;
  }

  // A theme file needs its resolved tokens. The last one used is cached so a
  // reload paints it at once; the server copy then replaces it.
  function loadCustom(id) {
    const ticket = ++generation;
    return getJson(detailUrl(id)).then(payload => {
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
    previewing = null;
    write(KEY, value);
    if (save) {
      const pending = request('/preferences', {
        method: 'PUT',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({theme: value}),
      });
      if (pending && typeof pending.catch === 'function') pending.catch(() => {});
    }
    return apply(value);
  }

  function apply(next) {
    const changed = next !== choice;
    choice = next;
    if (FIXED.includes(next)) {
      generation += 1;
      custom = null;
      write(CACHE, null);
      paint();
      return Promise.resolve();
    }
    if (changed && custom && custom.id !== next) custom = null;
    paint();
    return loadCustom(next);
  }

  /** Choose a theme: "system", "dark", "light" or an installed theme id. */
  function set(next) {
    return adopt(next, {save: true});
  }

  /** Show a theme without choosing it: a picker's highlighted row, before Enter. */
  function preview(next) {
    const value = normalise(next);
    if (!value || value === choice) return Promise.resolve();
    if (previewing === null) previewing = choice;
    return apply(value);
  }

  /** Put back the theme that was showing before `preview`, and forget the preview. */
  function endPreview() {
    if (previewing === null) return Promise.resolve();
    const value = previewing;
    previewing = null;
    return apply(normalise(value) || 'system');
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
        .filter(entry => entry && !entry.error && !Object.hasOwn(BUILTIN, entry.id) && ID.test(entry.id || '')
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

  window.KovaTheme = {KEY, appearance, choice: () => choice, choices, clearPalettePreview, detailUrl, endPreview, label, preview, previewPalette, refresh, set, sync, toggle};
  sync();
  refresh();
})();
