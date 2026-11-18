// Start screen suggestions (#194): local defaults, the shared preference,
// trimming and bounds, the off switch and the editor dialog. Runs the real
// starters.js with a small DOM and no browser or packages.
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import vm from 'node:vm';

class Element {
  constructor(tag = 'div') {
    this.tagName = tag; this.children = []; this.parentNode = null;
    this.className = ''; this.hidden = false; this.value = ''; this.type = '';
    this.textContent = ''; this.listeners = {}; this.attrs = {};
  }
  append(...nodes) { for (const node of nodes) { node.parentNode = this; this.children.push(node); } }
  replaceChildren(...nodes) { this.children = []; this.append(...nodes); }
  remove() { if (this.parentNode) this.parentNode.children.splice(this.parentNode.children.indexOf(this), 1); }
  setAttribute(name, value) { this.attrs[name] = String(value); }
  addEventListener(type, handler) { (this.listeners[type] ||= []).push(handler); }
  dispatch(type, event = {}) { for (const handler of this.listeners[type] || []) handler(event); }
  focus() { document.activeElement = this; }
  click() { if (typeof this.onclick === 'function') this.onclick(); this.dispatch('click'); }
  showModal() { this.open = true; }
}

const events = [];
const body = new Element('body');
const document = {
  body, activeElement: null, listeners: {},
  createElement: tag => new Element(tag),
  addEventListener(type, handler) { (this.listeners[type] ||= []).push(handler); },
  dispatchEvent(event) {
    events.push(event.detail);
    for (const handler of this.listeners[event.type] || []) handler(event);
    return true;
  },
};
const store = new Map();
let error = null;
const sandbox = {
  document, window: {}, console, CustomEvent: class { constructor(type, init) { this.type = type; this.detail = init?.detail; } },
  localStorage: {
    getItem: key => (store.has(key) ? store.get(key) : null),
    setItem: (key, value) => { store.set(key, String(value)); },
  },
  fetch: async (path, options = {}) => {
    if (!error && path === '/preferences' && !options.method) {
      return {ok: true, json: async () => ({starters: ['From the server', 'Second one']})};
    }
    if (options.method === 'PUT') {
      if (error) throw error;
      return {ok: true, json: async () => JSON.parse(options.body)};
    }
    throw new TypeError('offline');
  },
};
vm.runInNewContext(readFileSync(new URL('../src/kova/web_assets/starters.js', import.meta.url), 'utf8'), sandbox);
const starters = sandbox.KovaStarters;
assert.ok(starters, 'starters.js exposes KovaStarters');

// Defaults appear until a shared list or a local edit says otherwise.
assert.deepEqual([...starters.current()], [...starters.DEFAULT]);
// The module adopts the server list when it answers.
await new Promise(resolve => setTimeout(resolve, 0));
assert.deepEqual([...starters.current()], ['From the server', 'Second one']);
assert.deepEqual([...events.at(-1).starters], ['From the server', 'Second one']);

// Saving trims, drops blanks and duplicates, and keeps the four-item limit.
await starters.save(['  Plan a trip  ', 'Plan a trip', '', 'Write it up', 'One more', 'Too many']);
assert.deepEqual([...starters.current()], ['Plan a trip', 'Write it up', 'One more', 'Too many']);

// The renderer draws quiet buttons and only fills the composer through onPick.
const host = new Element();
const picked = [];
starters.render(host, text => picked.push(text));
assert.equal(host.hidden, false);
assert.equal(host.children.length, 4);
assert.equal(host.children[0].className, 'starter');
assert.equal(host.children[0].textContent, 'Plan a trip');
host.children[0].click();
assert.deepEqual(picked, ['Plan a trip']);

// An empty list is the off switch: no chips, no orphaned gap.
await starters.save([]);
starters.render(host, () => {});
assert.equal(starters.current().length, 0);
assert.equal(host.hidden, true);

// The editor offers four lines and resolves with the cleaned list.
const editing = starters.openEditor();
const dialog = body.children.at(-1);
const fields = dialog.children.find(child => child.className === 'starters-fields');
const inputs = fields.children.map(label => label.children[0]);
assert.equal(inputs.length, 4, 'four editable suggestions');
inputs[0].value = '  Ship it  ';
inputs[2].value = 'Ship it';
const buttons = dialog.children.at(-1).children;
buttons.find(button => button.textContent === 'Save').click();
assert.deepEqual([...(await editing)], ['Ship it']);

// Turn off is explicit and distinct from Cancel.
const turningOff = starters.openEditor();
const second = body.children.at(-1);
second.children.at(-1).children.find(button => button.textContent === 'Turn off').click();
assert.deepEqual([...(await turningOff)], []);

const cancelled = starters.openEditor();
const third = body.children.at(-1);
third.children.at(-1).children.find(button => button.textContent === 'Cancel').click();
assert.equal(await cancelled, null);

// A failed write still leaves the local list usable offline.
error = new TypeError('offline');
await starters.save(['Offline prompt']);
assert.deepEqual([...starters.current()], ['Offline prompt']);

console.log('starters ok');
