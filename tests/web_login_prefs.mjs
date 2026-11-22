// First launch: a window arrives with a one-use login ticket in the fragment,
// before app.js can exchange it for a session cookie. These modules read the
// shared preferences document, which is refused (401) until that exchange
// happens, so their first read waits for the signed-in page instead. Runs each
// module with a small DOM and no browser or packages.
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import vm from 'node:vm';

const tick = () => new Promise(resolve => setImmediate(resolve));
const settle = async () => { for (let i = 0; i < 6; i += 1) await tick(); };

const MODULES = [
  {file: 'theme.js', global: 'KovaTheme', wake: ['sync', 'refresh'], expect: ['/preferences', '/themes']},
  {file: 'typography.js', global: 'KovaTypography', expect: ['/preferences']},
  {file: 'motion.js', global: 'KovaMotion', expect: ['/preferences']},
  {file: 'custom_css.js', global: 'KovaCustomCss', expect: ['/preferences']},
  {file: 'reasoning.js', global: 'KovaReasoning', expect: ['/preferences']},
  {file: 'thinking.js', global: 'KovaThinking', expect: ['/preferences']},
  {file: 'starters.js', global: 'KovaStarters', expect: ['/preferences']},
  {file: 'shortcuts.js', global: 'KovaShortcuts', pick: module => module.current, expect: ['/preferences']},
];

function element(tag = 'div') {
  return {
    tagName: String(tag).toUpperCase(), dataset: {}, style: {}, children: [], hidden: false,
    textContent: '', className: '', attrs: {}, listeners: {},
    appendChild(child) { this.children.push(child); return child; },
    append(...nodes) { this.children.push(...nodes); },
    replaceChildren(...nodes) { this.children = nodes; },
    setAttribute(key, value) { this.attrs[key] = String(value); },
    removeAttribute(key) { delete this.attrs[key]; },
    getAttribute(key) { return this.attrs[key]; },
    remove() {}, focus() {},
    addEventListener(type, handler) { (this.listeners[type] ||= []).push(handler); },
  };
}

function boot(file, {hash = '', search = ''} = {}) {
  const calls = [];
  const fetch = (url, options = {}) => {
    calls.push({url, method: options.method || 'GET'});
    if (url === '/themes') return Promise.resolve({ok: true, json: async () => []});
    return Promise.resolve({ok: true, json: async () => ({})});
  };
  const root = element('html');
  const document = {
    documentElement: root, head: element('head'), body: element('body'), activeElement: null,
    listeners: {},
    addEventListener(type, handler) { (this.listeners[type] ||= []).push(handler); },
    dispatchEvent(event) { for (const handler of this.listeners[event.type] || []) handler(event); return true; },
    createElement: tag => element(tag),
    getElementById: () => null,
    querySelector: () => null,
    querySelectorAll: () => [],
  };
  const store = new Map();
  const sandbox = {
    document, console, fetch, setTimeout, clearTimeout, URLSearchParams,
    location: {hash, search},
    localStorage: {
      getItem: key => (store.has(key) ? store.get(key) : null),
      setItem: (key, value) => store.set(key, String(value)),
      removeItem: key => store.delete(key),
    },
    getComputedStyle: () => ({getPropertyValue: () => ''}),
    matchMedia: () => ({matches: false, addEventListener() {}, removeEventListener() {}}),
    CustomEvent: class { constructor(type, init) { this.type = type; this.detail = init?.detail; } },
  };
  sandbox.window = sandbox;
  sandbox.globalThis = sandbox;
  vm.runInNewContext(readFileSync(new URL(`../src/kova/web_assets/${file}`, import.meta.url), 'utf8'),
                     sandbox);
  return {sandbox, calls};
}

const gets = calls => calls.filter(call => call.method === 'GET').map(call => call.url);

for (const {file, global: name, pick, wake, expect} of MODULES) {
  // 1. Opened from `kova web`: nothing is read until the page is signed in.
  const pending = boot(file, {hash: '#ticket=one-use-ticket'});
  await settle();
  assert.deepEqual(gets(pending.calls), [],
    `${file} asked for the shared document before the login ticket was exchanged`);
  const module = pick ? pick(pending.sandbox[name]) : pending.sandbox[name];
  for (const call of wake || ['sync']) {
    assert.equal(typeof module?.[call], 'function', `${file} must expose ${call}() for the signed-in page`);
    await module[call]();
  }
  await settle();
  const after = gets(pending.calls);
  for (const url of expect) {
    assert.ok(after.includes(url), `${file} never read ${url} once signed in (got ${JSON.stringify(after)})`);
  }

  // 2. An already signed-in reload still reads it immediately, as before.
  const direct = boot(file);
  await settle();
  assert.deepEqual(gets(direct.calls), expect,
    `${file} stopped reading the shared document on a normal reload`);
}
