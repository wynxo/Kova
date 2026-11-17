// Reasoning display (#187): a folded "Thought for Ns" row and its setting.
// The module is loaded on its own, with a tiny DOM, a fake preferences store
// and no browser, so the logic is checked without a CDN or a package install.
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import vm from 'node:vm';

class Element {
  constructor(tag = 'div') {
    this.tagName = String(tag).toUpperCase();
    this.children = [];
    this.parentNode = null;
    this.className = '';
    this.attrs = {};
    this.dataset = {};
    this._text = '';
  }
  set textContent(text) { this._text = String(text); this.children = []; }
  get textContent() { return this._text + this.children.map(node => node.textContent).join(''); }
  append(...nodes) {
    for (const node of nodes) { node.parentNode = this; this.children.push(node); }
  }
  setAttribute(key, value) { this.attrs[key] = String(value); }
  getAttribute(key) { return this.attrs[key]; }
}

const documentElement = new Element('html');
const document = {
  documentElement,
  createElement: tag => new Element(tag),
  dispatchEvent() {},
};
const window = {};
const store = new Map();
const localStorage = {
  getItem: key => (store.has(key) ? store.get(key) : null),
  setItem: (key, value) => { store.set(key, String(value)); },
};
const calls = [];
const replies = {'/preferences': {reasoning: 'hidden'}};
const fetch = (url, options = {}) => {
  calls.push({url, method: options.method || 'GET', body: options.body});
  const payload = options.method === 'PUT' ? JSON.parse(options.body) : (replies[url] || {});
  return Promise.resolve({ok: true, json: async () => payload});
};

const sandbox = {document, window, localStorage, fetch, console};
vm.runInNewContext(readFileSync(new URL('../src/kova/web_assets/reasoning.js', import.meta.url), 'utf8'), sandbox);
const api = vm.runInNewContext('window.KovaReasoning', sandbox);

// The preference arrives from the shared server document, not only this tab.
await new Promise(resolve => setImmediate(resolve));
assert.equal(calls[0].url, '/preferences');
assert.equal(calls[0].method, 'GET', 'the module reads the shared preferences');
assert.equal(api.KEY, 'kova-reasoning');
assert.equal(api.shown(), false, 'the server preference hides reasoning');
assert.equal(documentElement.getAttribute('data-reasoning'), 'hidden');
assert.equal(store.get('kova-reasoning'), 'hidden', 'the shared value is cached locally');
assert.equal(api.row(0, 12000, 'Let me think.'), null, 'a hidden setting creates no row');

// Showing reasoning is one checked switch, saved both locally and server-side.
api.setShown(true);
assert.equal(api.shown(), true);
assert.equal(documentElement.getAttribute('data-reasoning'), 'shown');
assert.equal(store.get('kova-reasoning'), 'shown');
const put = calls.at(-1);
assert.equal(put.url, '/preferences');
assert.equal(put.method, 'PUT');
assert.deepEqual(JSON.parse(put.body), {reasoning: 'shown'});

// A finished burst is one collapsed row, in muted text with a left rule.
const reasoning = api.row(1000, 13000, 'Step one.\n<script>alert(1)</script>');
assert.ok(reasoning, 'a shown burst becomes a row');
assert.equal(reasoning.tagName, 'DETAILS');
assert.equal(reasoning.className, 'reasoning');
assert.equal(Boolean(reasoning.open), false, 'the row starts collapsed');
const summary = reasoning.children[0];
const body = reasoning.children[1];
assert.equal(summary.tagName, 'SUMMARY');
assert.equal(summary.textContent, 'Thought for 12s');
assert.equal(body.tagName, 'PRE');
assert.equal(body.className, 'reasoning-text');
assert.equal(body.children.length, 0, 'reasoning text is never parsed as markup');
assert.equal(body.textContent, 'Step one.\n<script>alert(1)</script>');
assert.equal(summary.getAttribute('aria-expanded'), undefined,
  'the native details element owns expanded state');

// Durations are whole seconds and never claim "Thought for 0s".
assert.equal(api.seconds(0), 1);
assert.equal(api.seconds(-500), 1);
assert.equal(api.seconds(400), 1);
assert.equal(api.seconds(12400), 12);
assert.equal(api.seconds(12600), 13);
assert.match(api.row(0, 12000, 'x').textContent, /^Thought for 12s/);

// An empty burst and a hidden switch produce no row at all.
assert.equal(api.row(0, 5000, '   '), null);
api.setShown(false);
assert.equal(api.row(0, 5000, 'still here'), null, 'hiding stops new rows');
assert.equal(documentElement.getAttribute('data-reasoning'), 'hidden');

console.log('Reasoning rows, duration and the shared show/hide setting passed');
