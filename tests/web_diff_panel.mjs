// diff-panel.js (#190): hunks, folded context, labelled buttons, Undo with the shown hash.
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import vm from 'node:vm';

let focused = null;
class Element {
  constructor(tag) {
    this.tagName = tag; this.children = []; this.attrs = {}; this.text = ''; this.dataset = {};
    this.className = ''; this.listeners = {}; this.disabled = false;
    this.classList = {
      toggle: (name, force) => {
        const names = new Set(this.className.split(' ').filter(Boolean));
        if (force ?? !names.has(name)) names.add(name); else names.delete(name);
        this.className = [...names].join(' ');
      },
      contains: name => this.className.split(' ').includes(name),
    };
  }
  set textContent(value) { this.text = String(value); this.children = []; }
  get textContent() { return this.text + this.children.map(child => child.textContent).join(''); }
  append(...nodes) { for (const item of nodes) { if (typeof item === 'string') { this.text += item; continue; } item.parent = this; this.children.push(item); } }
  replaceChildren(...nodes) { this.children = []; this.text = ''; this.append(...nodes); }
  replaceWith(other) { const list = this.parent.children; other.parent = this.parent; list[list.indexOf(this)] = other; }
  setAttribute(key, value) { this.attrs[key] = String(value); }
  addEventListener(type, fn) { (this.listeners[type] ||= []).push(fn); }
  click() { for (const fn of this.listeners.click || []) fn({}); }
  focus() { focused = this; }
  all() { return [this, ...this.children.flatMap(child => child.all())]; }
  querySelectorAll(selector) { return this.all().filter(node => node !== this && node.className.split(' ').includes(selector.slice(1))); }
  querySelector(selector) {
    const hunk = /^\[data-hunk="(.*)"\]$/.exec(selector);
    return this.all().find(node => hunk ? node.dataset.hunk === hunk[1] : node.className.split(' ').includes(selector.slice(1))) || null;
  }
}
const byClass = (node, cls) => node.all().filter(child => child.className.split(' ').includes(cls));

let panel = null;
const context = {
  document: {createElement: tag => new Element(tag)},
  CSS: {escape: value => value},
  window: {KovaPanel: {open: options => { panel = options; }}},
};
vm.createContext(context);
vm.runInContext(readFileSync(new URL('../src/kova/web_assets/diff-panel.js', import.meta.url), 'utf8') + '\nglobalThis.panel = KovaDiffPanel;', context);

const lines = count => Array.from({length: count}, (_, index) => `line ${index + 1}`);
const view = {
  path: '<img src=x>.txt', additions: 2, deletions: 1, supported: true, current_hash: 'a'.repeat(64),
  segments: [
    {type: 'same', old: 1, new: 1, lines: lines(10)},
    {type: 'hunk', id: 'h1', header: '@@ -11,1 +11,1 @@', state: 'applied', old: 11, new: 11, removed: ['old'], added: ['new']},
    {type: 'same', old: 12, new: 12, lines: lines(20)},
    {type: 'hunk', id: 'h2', header: '@@ -32,0 +32,1 @@', state: 'changed', old: 32, new: 32, removed: [], added: ['extra']},
    {type: 'same', old: 32, new: 33, lines: lines(2)},
  ],
};

// Pure rendering: rows are text, context folds, buttons only where Undo is possible.
const body = context.panel.render(view, {undo: () => {}, keep: () => {}, kept: new Set()});
assert.equal(body.attrs.role, 'region');
assert.equal(body.attrs['aria-label'], 'Diff of <img src=x>.txt', 'the path is text, never markup');
const hunks = byClass(body, 'diff-hunk');
assert.equal(hunks.length, 2);
assert.equal(hunks[0].attrs.tabindex, '0', 'every hunk is focusable');
assert.match(hunks[0].attrs['aria-label'], /^Change 1 of 2, line 11: 1 line removed, 1 line added, still in the file/);
assert.match(hunks[1].attrs['aria-label'], /edited since/);
assert.equal(byClass(hunks[1], 'btn').length, 0, 'a hunk edited since offers no Undo');
const [keepButton, undoButton] = byClass(hunks[0], 'btn');
assert.equal(keepButton.attrs['aria-label'], 'Keep change 1 of 2');
assert.equal(undoButton.attrs['aria-label'], 'Undo change 1 of 2, line 11');
assert.equal(byClass(hunks[0], 'file-diff-del')[0].textContent, '11-old');
assert.equal(byClass(hunks[0], 'file-diff-add')[0].textContent, '11+new');

const folds = byClass(body, 'diff-expand');
assert.equal(folds.length, 2, 'the top run and the long middle run fold; two trailing lines do not');
assert.equal(folds[0].textContent, 'Show 7 more lines');
assert.equal(folds[1].textContent, 'Show 14 more lines');
assert.match(folds[1].attrs['aria-label'], /lines 15 to 28/);
const before = byClass(body, 'file-diff-context').length;
folds[1].click();
assert.equal(byClass(body, 'file-diff-context').length, before + 14, 'expanding shows the folded lines');
assert.equal(focused.className, 'diff-expanded', 'focus lands on what was revealed');

// Arrow keys move between hunks.
const keydown = body.listeners.keydown[0];
let prevented = false;
keydown({key: 'ArrowDown', target: hunks[0], preventDefault: () => { prevented = true; }});
assert.ok(prevented && focused === hunks[1]);

// The panel: loads, undoes with the hash it was shown, then redraws from the reply.
const calls = [];
const undone = structuredClone(view);
undone.segments[1].state = 'undone';
undone.current_hash = 'b'.repeat(64);
let changed = 0;
await context.panel.open({chatId: 'c1', checkpointId: 7, path: 'notes.txt', onChange: () => changed++,
  request: async (url, options) => { calls.push({url, options}); return options ? {checkpoint_id: 8, view: undone} : structuredClone(view); }});
assert.equal(panel.title, 'Diff · notes.txt');
assert.equal(calls[0].url, '/chats/c1/checkpoints/7/hunks?path=notes.txt');
const first = byClass(panel.node, 'diff-hunk')[0];
await byClass(first, 'btn')[1].click();
await new Promise(resolve => setTimeout(resolve, 0));
assert.equal(calls[1].url, '/chats/c1/checkpoints/7/hunks/undo');
assert.deepEqual(JSON.parse(calls[1].options.body), {path: 'notes.txt', hunk: 'h1', expected_hash: 'a'.repeat(64)});
assert.equal(changed, 1);
assert.match(byClass(panel.node, 'diff-panel-status')[0].textContent, /Undid change 1\. Saved as change set 8/);
const redrawn = byClass(panel.node, 'diff-hunk')[0];
assert.ok(redrawn.className.includes('diff-hunk-undone'));
assert.equal(focused, redrawn, 'focus stays on the hunk that was undone');

// Keep is local: the hunk loses its buttons and says so.
const again = structuredClone(view);
await context.panel.open({chatId: 'c1', checkpointId: 7, path: 'notes.txt', request: async () => structuredClone(again)});
byClass(byClass(panel.node, 'diff-hunk')[0], 'btn')[0].click();
const kept = byClass(panel.node, 'diff-hunk')[0];
assert.ok(kept.className.includes('diff-hunk-kept'));
assert.equal(byClass(kept, 'btn').length, 0);

// A refused undo explains itself and leaves the buttons usable.
await context.panel.open({chatId: 'c1', checkpointId: 7, path: 'notes.txt', request: async (url, options) => {
  if (options) throw new Error('notes.txt changed since you opened this diff. Reopen it to see the current changes.');
  return structuredClone(view);
}});
const target = byClass(panel.node, 'diff-hunk')[0];
const buttons = byClass(target, 'btn');
buttons[1].click();
await new Promise(resolve => setTimeout(resolve, 0));
const status = byClass(panel.node, 'diff-panel-status')[0];
assert.ok(status.className.includes('is-error'));
assert.match(status.textContent, /changed since you opened this diff/);
assert.ok(!buttons[1].disabled);
assert.equal(byClass(status, 'btn')[0].textContent, 'Reload diff');
console.log('diff panel ok');
