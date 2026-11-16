// DOM-level checks for the optional status bar (#204): hidden by default,
// quiet when idle, fed by data the server already exposes. No browser needed.
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import vm from 'node:vm';

class Element {
  constructor(tag = 'div') {
    this.tagName = tag; this.children = [];
    this._text = ''; this.hidden = false; this.checked = false;
    this.style = {}; this.attrs = {}; this.listeners = {};
    this._classes = new Set();
    this.classList = {
      toggle: (name, force) => {
        const add = force ?? !this._classes.has(name);
        if (add) this._classes.add(name); else this._classes.delete(name);
      },
      contains: name => this._classes.has(name),
    };
    this.clicked = 0;
  }
  set textContent(text) { this._text = text; }
  get textContent() { return this._text; }
  setAttribute(key, value) { this.attrs[key] = String(value); }
  getAttribute(key) { return this.attrs[key]; }
  addEventListener(type, fn) { this.listeners[type] = fn; }
  click() { this.clicked++; this.listeners.click?.(); }
}

const elements = new Map();
const ids = ['status-bar', 'status-bar-model', 'status-bar-context', 'status-bar-fill',
             'status-bar-compact', 'status-bar-toggle', 'compact-now'];
for (const id of ids) elements.set(id, new Element(id === 'status-bar-compact' || id === 'compact-now' ? 'button' : id === 'status-bar-toggle' ? 'input' : 'div'));

const stored = new Map();
const sandbox = {
  document: {
    readyState: 'complete',
    getElementById: id => elements.get(id) || null,
    addEventListener() {},
  },
  localStorage: {
    getItem: key => (stored.has(key) ? stored.get(key) : null),
    setItem: (key, value) => stored.set(key, String(value)),
    removeItem: key => stored.delete(key),
  },
  window: {},
  console,
};
sandbox.window.localStorage = sandbox.localStorage;
vm.runInNewContext(readFileSync(new URL('../src/kova/web_assets/status-bar.js', import.meta.url), 'utf8'), sandbox);
const bar = vm.runInNewContext('window.KovaStatus', sandbox);
const win = vm.runInNewContext('window', sandbox);

// Hidden by default: nothing stored, nothing on screen, toggle off.
assert.equal(bar.isVisible(), false);
assert.equal(elements.get('status-bar').hidden, true);
assert.equal(elements.get('status-bar-toggle').checked, false);
bar.update({model: 'lab', windowTokens: 131072, messages: [{role: 'user', content: 'hi'}]});
assert.equal(elements.get('status-bar').hidden, true, 'an update must not opt the user in');
console.log('hidden by default passed');

// Opting in persists and paints the model plus the context meter.
bar.setVisible(true);
assert.equal(stored.get('kova-status-bar'), 'shown');
assert.equal(elements.get('status-bar').hidden, false);
const messages = [{role: 'user', content: 'x'.repeat(4000)}, {role: 'assistant', content: 'y'.repeat(4000)}];
bar.update({model: 'lab', windowTokens: 100000, messages});
assert.equal(elements.get('status-bar-model').textContent, 'lab');
assert.equal(elements.get('status-bar-context').textContent, '2k of 100k · 2%');
assert.equal(elements.get('status-bar-fill').style.width, '2%');
assert.equal(elements.get('status-bar-compact').hidden, true, 'quiet well below the limit');
assert.ok(elements.get('status-bar').attrs['aria-label'].includes('lab'));
console.log('opt-in model and meter passed');

// Past 80% the bar offers the existing Compact action, reusing its button.
bar.update({model: 'lab', windowTokens: 1000, messages: [{role: 'user', content: 'z'.repeat(3600)}]});
assert.equal(elements.get('status-bar-context').textContent, '900 of 1k · 90%');
assert.equal(elements.get('status-bar-fill').style.width, '90%');
assert.equal(elements.get('status-bar-compact').hidden, false);
assert.ok(elements.get('status-bar').attrs['aria-label'].includes('compact'));
elements.get('status-bar-compact').click();
assert.equal(elements.get('compact-now').clicked, 1, 'Compact reuses the existing composer action');
console.log('nearly-full Compact offer passed');

// An unknown window still names the model instead of guessing.
bar.update({model: 'lab', windowTokens: 0, messages});
assert.ok(elements.get('status-bar-context').textContent.includes('window unknown'));
assert.equal(elements.get('status-bar-compact').hidden, true);
bar.update({model: '', windowTokens: 0, messages: []});
assert.equal(elements.get('status-bar-model').textContent, 'No model');
console.log('unknown window passed');

// The toggle and the bridge the composer calls back through.
let refreshed = 0;
win.KovaStatusRefresh = () => { refreshed++; };
elements.get('status-bar-toggle').listeners.change();
assert.equal(refreshed, 1, 'enabling asks the composer for fresh data');
bar.setVisible(false);
assert.equal(stored.has('kova-status-bar'), false);
assert.equal(elements.get('status-bar').hidden, true);
assert.equal(elements.get('status-bar-toggle').checked, false);
console.log('toggle persistence and refresh bridge passed');

// Same k/M thresholds as the per-answer meter (#217), not a second dialect.
assert.equal(bar._shortCount(999), '999');
assert.equal(bar._shortCount(1234), '1.2k');
assert.equal(bar._shortCount(340), '340');
assert.equal(bar._usedTokens([{content: 'abcd'}, {content: 42}, {}]), 1);
assert.equal(bar._highRatio, 0.8);
console.log('shared number formatting passed');
