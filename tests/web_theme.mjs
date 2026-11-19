// Theme engine, browser half (#174): the choice, its shared preference, and the
// tokens of an installed theme written into the custom properties. theme.js runs
// in a fresh sandbox per scenario with a tiny DOM, a fake server and no browser.
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import vm from 'node:vm';

const SOURCE = readFileSync(new URL('../src/kova/web_assets/theme.js', import.meta.url), 'utf8');
const tick = () => new Promise(resolve => setImmediate(resolve));
const settle = async () => { for (let i = 0; i < 6; i += 1) await tick(); };

// What tokens.css would give back for each built-in, so the chrome colour can be
// checked without a stylesheet. Placeholders, not colours: this only proves the
// value is read from the cascade rather than written into the script.
const CASCADE = {dark: 'tokens.css dark bg.base', light: 'tokens.css light bg.base'};

const MIDNIGHT = {
  id: 'midnight-teal', name: 'Midnight Teal', appearance: 'dark', extends: 'kova-dark',
  tokens: {
    'bg.base': '#0b1416', 'accent.default': '#2dd4bf', 'accent.on': '#04201c',
    // Never reaches the page: not a semantic token, or not a plain colour.
    'accent.default; color': '#ffffff', 'primitive.orange': '#ff0000',
    'fg.default': 'url(https://example.com/x)', 'fg.muted': 'red',
  },
  terminal: [],
};
const PAPER = {id: 'paper', name: 'Paper', appearance: 'light', extends: 'kova-light',
  tokens: {'bg.base': '#fbfaf7', 'fg.default': '#1a1a1a'}};

function boot({stored = {}, server = {theme: 'system'}, prefersLight = false, themes = {}, list = null,
  offline = false, hold = null} = {}) {
  const store = new Map(Object.entries(stored));
  const properties = new Map();
  const root = {dataset: {}, style: {
    setProperty: (name, value) => properties.set(name, value),
    removeProperty: name => properties.delete(name),
  }};
  const meta = {attrs: {content: 'index.html'}, setAttribute(key, value) { this.attrs[key] = value; }};
  const events = [];
  const mediaListeners = [];
  const media = {matches: prefersLight, addEventListener: (type, fn) => mediaListeners.push(fn)};
  const calls = [];
  const held = [];
  const fetch = (url, options = {}) => {
    const method = options.method || 'GET';
    calls.push({url, method, body: options.body ? JSON.parse(options.body) : null});
    if (offline) return Promise.reject(new Error('offline'));
    let payload = null, ok = true;
    if (url === '/preferences') payload = method === 'PUT' ? {...server, ...JSON.parse(options.body)} : server;
    else if (url === '/themes') payload = list || [];
    else if (url.startsWith('/themes/')) {
      const id = decodeURIComponent(url.slice('/themes/'.length));
      payload = themes[id] || {detail: 'No theme'}; ok = Boolean(themes[id]);
    }
    const response = Promise.resolve({ok, json: async () => payload});
    if (hold && url === hold) return new Promise(resolve => held.push(() => resolve(response)));
    return response;
  };
  const sandbox = {
    document: {
      documentElement: root,
      getElementById: id => (id === 'theme-color' ? meta : null),
      dispatchEvent: event => events.push(event.detail),
    },
    window: {matchMedia: query => { assert.equal(query, '(prefers-color-scheme: light)'); return media; }},
    getComputedStyle: () => ({getPropertyValue: name => (name === '--bg-base' ? ` ${CASCADE[root.dataset.theme]} ` : '')}),
    CustomEvent: class { constructor(type, init) { this.type = type; this.detail = init.detail; } },
    localStorage: {
      getItem: key => (store.has(key) ? store.get(key) : null),
      setItem: (key, value) => store.set(key, String(value)),
      removeItem: key => store.delete(key),
    },
    fetch, console, JSON, encodeURIComponent,
  };
  vm.runInNewContext(SOURCE, sandbox);
  const api = vm.runInNewContext('window.KovaTheme', sandbox);
  return {api, root, meta, store, properties, events, calls, media, mediaListeners, held,
    puts: () => calls.filter(call => call.method === 'PUT').map(call => call.body)};
}

// ---- System is the default and follows the device -------------------------
{
  const page = boot({prefersLight: true});
  assert.equal(page.api.choice(), 'system');
  assert.equal(page.root.dataset.theme, 'light', 'a light device paints light before any request');
  assert.equal(page.meta.attrs.content, CASCADE.light, 'the window colour is the cascade bg-base');
  page.media.matches = false;
  page.mediaListeners.forEach(fn => fn());
  assert.equal(page.root.dataset.theme, 'dark', 'the device switching to dark is followed');
  assert.equal(page.meta.attrs.content, CASCADE.dark);
  assert.equal(page.events.at(-1).appearance, 'dark');
  await settle();
  assert.deepEqual(page.puts(), [], 'nothing is saved just by loading');
  assert.equal(page.store.get('kova-theme-synced'), '1');
}

// ---- The first frame uses the local copy; the shared copy then wins --------
{
  const page = boot({stored: {'kova-theme': 'light', 'kova-theme-synced': '1'}, server: {theme: 'dark'}});
  assert.equal(page.root.dataset.theme, 'light', 'first paint is the local choice');
  await settle();
  assert.equal(page.api.choice(), 'dark', 'a choice made on another device arrives here');
  assert.equal(page.root.dataset.theme, 'dark');
  assert.equal(page.store.get('kova-theme'), 'dark', 'and is cached for the next first paint');
  assert.deepEqual(page.puts(), []);
}

// ---- A browser from before the sync keeps its choice and shares it once ----
{
  const page = boot({stored: {'kova-theme': 'light'}, server: {theme: 'system'}});
  await settle();
  assert.deepEqual(page.puts(), [{theme: 'light'}], 'the old local choice is sent up once');
  assert.equal(page.api.choice(), 'light');
  const again = boot({stored: {'kova-theme': 'light', 'kova-theme-synced': '1'}, server: {theme: 'system'}});
  await settle();
  assert.deepEqual(again.puts(), [], 'only once');
  assert.equal(again.api.choice(), 'system');
  // Another device already chose: its choice wins over a stale local value.
  const late = boot({stored: {'kova-theme': 'light'}, server: {theme: 'dark'}});
  await settle();
  assert.deepEqual(late.puts(), []);
  assert.equal(late.api.choice(), 'dark');
}

// ---- Choosing is saved to the shared preferences ---------------------------
{
  const page = boot();
  await settle();
  await page.api.set('light');
  assert.equal(page.root.dataset.theme, 'light');
  assert.deepEqual(page.puts().at(-1), {theme: 'light'});
  assert.equal(page.store.get('kova-theme'), 'light');
  await page.api.set('kova-dark');
  assert.equal(page.api.choice(), 'dark', 'a built-in id means the fixed choice');
  await page.api.set('Not A Theme!');
  assert.equal(page.api.choice(), 'dark', 'an invalid choice is ignored');
  assert.deepEqual(page.puts(), [{theme: 'light'}, {theme: 'dark'}]);
  await page.api.toggle();
  assert.equal(page.api.choice(), 'light', 'the shortcut flips to the opposite built-in');
}

// ---- An installed theme is resolved by the server and written as tokens ----
{
  const page = boot({themes: {'midnight-teal': MIDNIGHT, paper: PAPER}});
  await settle();
  await page.api.set('midnight-teal');
  assert.equal(page.calls.some(call => call.url === '/themes/midnight-teal'), true);
  assert.equal(page.root.dataset.theme, 'dark', 'data-theme is the theme appearance');
  assert.deepEqual(Object.fromEntries(page.properties), {
    '--bg-base': '#0b1416', '--accent-default': '#2dd4bf', '--accent-on': '#04201c',
  }, 'only semantic tokens with plain colour values become custom properties');
  assert.equal(page.meta.attrs.content, '#0b1416', 'the window colour is the theme bg.base');
  assert.equal(JSON.parse(page.store.get('kova-theme-tokens')).id, 'midnight-teal', 'cached for first paint');
  assert.equal(page.events.at(-1).name, 'Midnight Teal');

  await page.api.set('paper');
  assert.equal(page.root.dataset.theme, 'light');
  assert.deepEqual([...page.properties.keys()].sort(), ['--bg-base', '--fg-default'],
                   'switching themes clears the old properties');
  await page.api.toggle();
  assert.equal(page.api.choice(), 'dark', 'the shortcut leaves a light theme file for Dark');
  assert.equal(page.properties.size, 0, 'a built-in clears every property the file set');
  assert.equal(page.store.has('kova-theme-tokens'), false);
  assert.equal(page.meta.attrs.content, CASCADE.dark);
}

// ---- A reload paints the cached theme file at once ------------------------
{
  const cached = JSON.stringify({id: 'midnight-teal', name: 'Midnight Teal', appearance: 'dark',
    tokens: {'bg.base': '#0b1416', 'accent.default': '#2dd4bf'}});
  const page = boot({stored: {'kova-theme': 'midnight-teal', 'kova-theme-tokens': cached, 'kova-theme-synced': '1'},
    server: {theme: 'midnight-teal'}, themes: {'midnight-teal': MIDNIGHT}, hold: '/themes/midnight-teal'});
  assert.equal(page.properties.get('--accent-default'), '#2dd4bf', 'the cache paints before the server answers');
  assert.equal(page.meta.attrs.content, '#0b1416');
  page.held.forEach(resolve => resolve());
  await settle();
  assert.equal(page.properties.get('--accent-on'), '#04201c', 'the server copy then replaces it');
}

// ---- A theme file missing on this PC falls back to the device --------------
{
  const page = boot({prefersLight: true, stored: {'kova-theme-synced': '1'}, server: {theme: 'gone-theme'}});
  await settle();
  assert.equal(page.api.choice(), 'gone-theme', 'the shared choice is kept for when it is installed');
  assert.equal(page.root.dataset.theme, 'light', 'meanwhile the device appearance is used');
  assert.equal(page.properties.size, 0);
  assert.deepEqual(page.puts(), [], 'and nothing is overwritten');
}

// ---- A slow theme answer never overrides a newer choice --------------------
{
  const page = boot({themes: {'midnight-teal': MIDNIGHT}, hold: '/themes/midnight-teal'});
  await settle();
  const pending = page.api.set('midnight-teal');
  await page.api.set('light');
  page.held.forEach(resolve => resolve());
  await pending; await settle();
  assert.equal(page.api.choice(), 'light');
  assert.equal(page.root.dataset.theme, 'light');
  assert.equal(page.properties.size, 0, 'the late theme file was dropped');
}

// ---- The picker lists valid installed themes after the fixed three ---------
{
  const page = boot({list: [
    {id: 'kova-dark', name: 'Kova Dark', appearance: 'dark', error: null},
    {id: 'kova-light', name: 'Kova Light', appearance: 'light', error: null},
    {id: 'midnight-teal', name: 'Midnight Teal', appearance: 'dark', error: null},
    {id: 'broken', name: null, appearance: null, error: 'broken.json: Missing required key'},
    {id: '../etc', name: 'Sneaky', appearance: 'dark', error: null},
  ]});
  await settle();
  assert.deepEqual([...page.api.choices().map(choice => choice.id)], ['system', 'light', 'dark', 'midnight-teal']);
  assert.equal(page.api.label('midnight-teal'), 'Midnight Teal');
  assert.equal(page.api.label('system'), 'System');
}

// ---- Offline: the local choice stays, nothing throws -----------------------
{
  const page = boot({stored: {'kova-theme': 'light'}, offline: true});
  await settle();
  assert.equal(page.api.choice(), 'light');
  assert.equal(page.root.dataset.theme, 'light');
  assert.equal(page.store.has('kova-theme-synced'), false, 'it tries to sync again next time');
}

// A draft replaces page tokens without changing the saved selection or its cache.
{
  const page = boot({themes: {paper: PAPER}});
  await settle();
  await page.api.set('paper');
  const saved = page.store.get('kova-theme-tokens');
  page.api.preview({appearance: 'dark', tokens: {'bg.base': '#123456'}});
  assert.equal(page.root.dataset.theme, 'dark');
  assert.equal(page.meta.attrs.content, '#123456');
  assert.equal(page.api.choice(), 'paper');
  assert.equal(page.store.get('kova-theme-tokens'), saved);
  page.api.clearPreview();
  assert.equal(page.root.dataset.theme, 'light');
  assert.equal(page.meta.attrs.content, PAPER.tokens['bg.base'], 'cancel restores an installed theme');
  page.api.preview({appearance: 'dark', tokens: {'bg.base': '#123456'}});
  await page.api.sync();
  assert.equal(page.root.dataset.theme, 'dark', 'a preference update does not replace the preview');
  page.api.clearPreview();
  assert.equal(page.root.dataset.theme, 'dark', 'cancel restores the latest saved System choice');
  assert.equal(page.properties.size, 0);
}

// An otherwise valid theme ID must not collide with inherited object properties.
{
  const palette = {...PAPER, id: 'constructor'};
  const page = boot({themes: {constructor: palette}, list: [palette]});
  await settle();
  assert.ok(page.api.choices().some(choice => choice.id === 'constructor'));
  await page.api.set('constructor');
  assert.equal(page.api.choice(), 'constructor');
  assert.equal(page.root.dataset.theme, 'light');
}

console.log('Theme engine: choice, sync, installed tokens and window colour passed');
