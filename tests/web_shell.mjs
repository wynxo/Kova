// The app shell's context ring: the percentage it shows, the 80% warning it
// shares with the status bar, and its on/off switch. Runs the real shell.js
// with a small stand-in for the page; no browser.
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import vm from 'node:vm';

const SOURCE = readFileSync(new URL('../src/kova/web_assets/shell.js', import.meta.url), 'utf8');

class Element {
  constructor() {
    this.textContent = '';
    this.hidden = false;
    this.title = '';
    this.attrs = {};
    this.listeners = {};
    this._classes = new Set();
    this.classList = {
      toggle: (name, force) => {
        const add = force ?? !this._classes.has(name);
        if (add) this._classes.add(name); else this._classes.delete(name);
      },
      contains: name => this._classes.has(name),
    };
  }
  setAttribute(name, value) { this.attrs[name] = String(value); }
  addEventListener(type, fn) { this.listeners[type] = fn; }
}

const elements = new Map(['context-ring', 'context-ring-fill', 'context-ring-text', 'context-ring-toggle']
  .map(id => [id, new Element()]));
const ring = elements.get('context-ring');
const fill = elements.get('context-ring-fill');
const text = elements.get('context-ring-text');
const toggle = elements.get('context-ring-toggle');
const stored = new Map();
const documentListeners = [];

const sandbox = {
  document: {
    getElementById: id => elements.get(id) || null,
    addEventListener: (type, fn) => documentListeners.push([type, fn]),
  },
  localStorage: {
    getItem: key => (stored.has(key) ? stored.get(key) : null),
    setItem: (key, value) => stored.set(key, String(value)),
  },
  console,
};
vm.runInNewContext(SOURCE, sandbox);
const shell = sandbox.KovaShell;
assert.ok(shell && typeof shell.paintRing === 'function', 'shell.js exposes paintRing');

// A window and some use: the figure, the arc and the spoken label agree.
shell.paintRing({used: 62000, windowSize: 100000});
assert.equal(ring.hidden, false);
assert.equal(text.textContent, '62%');
assert.equal(fill.attrs['stroke-dasharray'], '62 100');
assert.equal(ring.attrs['aria-label'], 'Context used: 62 percent');
assert.equal(ring.title, '62000 of 100000 context tokens used');
assert.equal(ring.classList.contains('is-high'), false, 'well below the limit is quiet');

// The percentage is exact for whole figures. used / windowSize * 100 gives
// 28.999999999999996 for 29 of 100 and 56.99999999999999 for 57 of 100, which
// floor one percent short.
for (const [used, windowSize, percent] of [[29, 100, 29], [57, 100, 57], [58, 200, 29], [2900, 10000, 29]]) {
  shell.paintRing({used, windowSize});
  assert.equal(text.textContent, `${percent}%`, `${used} of ${windowSize}`);
  assert.equal(fill.attrs['stroke-dasharray'], `${percent} 100`, `${used} of ${windowSize} arc`);
}

// The warning starts at 80%, the status bar's threshold, and never passes 100.
shell.paintRing({used: 79999, windowSize: 100000});
assert.equal(ring.classList.contains('is-high'), false, '79% is still quiet');
shell.paintRing({used: 80000, windowSize: 100000});
assert.equal(ring.classList.contains('is-high'), true);
shell.paintRing({used: 250000, windowSize: 100000});
assert.equal(text.textContent, '100%', 'an overfull chat reads 100%, not more');

// No window (a chat with no model yet) hides the ring.
shell.paintRing({used: 10, windowSize: 0});
assert.equal(ring.hidden, true);

// The last reading is kept: turning the ring back on repaints it without
// waiting for the next status-bar update.
shell.paintRing({used: 62000, windowSize: 100000});
shell.setRing(false);
assert.equal(stored.get('kova-context-ring'), 'off');
assert.equal(ring.hidden, true);
assert.equal(toggle.attrs['aria-checked'], 'false');
assert.equal(shell.ringOn(), false);
shell.paintRing({used: 95000, windowSize: 100000});
assert.equal(ring.hidden, true, 'an off ring stays off through a paint');
shell.setRing(true);
assert.equal(stored.get('kova-context-ring'), 'on');
assert.equal(ring.hidden, false);
assert.equal(toggle.attrs['aria-checked'], 'true');
assert.equal(text.textContent, '95%', 'the ring comes back with the latest reading');

// The switch in Settings flips it once the page has loaded.
const loaded = documentListeners.find(([type]) => type === 'DOMContentLoaded');
assert.ok(loaded, 'the switch waits for the page');
loaded[1]();
toggle.listeners.click();
assert.equal(shell.ringOn(), false, 'clicking the switch turns the ring off');
toggle.listeners.click();
assert.equal(shell.ringOn(), true);

// A page without the ring (the gallery, an older cached shell) must not throw.
const bare = {document: {getElementById: () => null, addEventListener() {}}, localStorage: sandbox.localStorage, console};
vm.runInNewContext(SOURCE, bare);
bare.KovaShell.paintRing({used: 10, windowSize: 100});
bare.KovaShell.setRing(false);
bare.KovaShell.setRing(true);

console.log('Shell ring: percentage, threshold and switch passed');
