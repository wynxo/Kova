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
  localStorage: {data: {}, getItem(key) { return this.data[key] ?? null; }, setItem(key, value) { this.data[key] = String(value); }},
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

// Word-level highlights: only the words that changed are marked (#190).
const words = context.panel.wordDiff('const total = add(a, b);', 'const total = add(a, c);');
assert.deepEqual(JSON.parse(JSON.stringify(words)), {
  old: [{text: 'const total = add(a, ', changed: false}, {text: 'b', changed: true}, {text: ');', changed: false}],
  new: [{text: 'const total = add(a, ', changed: false}, {text: 'c', changed: true}, {text: ');', changed: false}],
});
assert.equal(context.panel.wordDiff('hello world', 'goodbye moon'), null, 'a rewritten line is not marked word by word');
assert.equal(context.panel.wordDiff('same', 'same'), null);
assert.equal(context.panel.wordDiff('a '.repeat(400), 'b '.repeat(400)), null, 'very long lines are not compared');
const edited = {path: 'app.py', supported: true, segments: [
  {type: 'hunk', id: 'w1', header: '@@ -1,2 +1,1 @@', state: 'applied', old: 1, new: 1,
   removed: ['total = add(a, b)', 'print(total)'], added: ['total = add(a, c)']},
]};
const unified = context.panel.render(edited, {});
const marks = byClass(unified, 'diff-word');
assert.deepEqual(marks.map(mark => [mark.tagName, mark.textContent]), [['mark', 'b'], ['mark', 'c']]);
assert.equal(byClass(unified, 'file-diff-del')[0].textContent, '1-total = add(a, b)', 'marks keep the line text whole');

// Split layout: removed lines on the left, added on the right, blanks where one side is shorter.
const split = context.panel.render({...view, segments: [...view.segments.slice(0, 2), ...edited.segments]}, {split: true});
assert.ok(split.className.includes('is-split'));
const pairs = byClass(split, 'diff-split-row');
const last = pairs.slice(-2);
assert.deepEqual(last[0].children.map(half => half.className.split(' ').find(name => name.startsWith('file-diff-') && name !== 'file-diff-row')),
                 ['file-diff-del', 'file-diff-add']);
assert.ok(last[1].children[1].className.includes('diff-split-empty'), 'an unpaired removed line has a blank right side');
assert.equal(last[1].children[1].attrs['aria-hidden'], 'true');
assert.equal(last[1].children[0].textContent, '2-print(total)', 'each side numbers its own file');
const context3 = pairs.find(pair => pair.children[0].className.includes('file-diff-context'));
assert.equal(context3.children[0].textContent, context3.children[1].textContent, 'unchanged lines show on both sides');
const splitFold = byClass(split, 'diff-expand')[0];
splitFold.click();
assert.ok(byClass(split, 'diff-expanded')[0].children.every(child => child.className === 'diff-split-row'), 'a fold opens in the same layout');

// The panel opens one tab per file and remembers Unified or Split on this device.
await context.panel.open({chatId: 'c1', checkpointId: 9, path: 'app.py', background: true, request: async () => structuredClone(edited)});
assert.equal(panel.key, 'diff:c1:9:app.py', 'the same file and change set reuse one tab');
assert.equal(panel.background, true);
const toggle = byClass(panel.node, 'diff-layout')[0];
assert.equal(toggle.attrs.role, 'group');
const [unifiedButton, splitButton] = byClass(toggle, 'btn');
assert.equal(unifiedButton.attrs['aria-pressed'], 'true');
splitButton.click();
assert.equal(splitButton.attrs['aria-pressed'], 'true');
assert.equal(context.localStorage.data['kova-diff-layout'], 'split');
assert.ok(byClass(panel.node, 'diff-panel-diff')[0].className.includes('is-split'));
assert.equal(focused, splitButton, 'focus stays on the toggle');
await context.panel.open({chatId: 'c1', checkpointId: 9, path: 'app.py', request: async () => structuredClone(edited)});
assert.ok(byClass(panel.node, 'diff-panel-diff')[0].className.includes('is-split'), 'the next diff opens split');
context.localStorage.getItem = () => { throw new Error('blocked'); };
await context.panel.open({chatId: 'c1', checkpointId: 9, path: 'app.py', request: async () => structuredClone(edited)});
assert.ok(!byClass(panel.node, 'diff-panel-diff')[0].className.includes('is-split'), 'blocked storage falls back to unified');
// Keep file marks every pending hunk in the current panel without a write.
let requests = 0;
await context.panel.open({chatId: 'c1', checkpointId: 10, path: 'notes.txt', request: async (_url, options) => {
  assert.equal(options, undefined);
  requests += 1;
  return {...structuredClone(view), segments: [...view.segments, ...edited.segments]};
}});
const keepFile = byClass(byClass(panel.node, 'diff-panel-meta')[0], 'btn')
  .find(control => control.textContent === 'Keep file');
assert.ok(keepFile);
keepFile.click();
assert.equal(requests, 1);
assert.equal(byClass(panel.node, 'diff-hunk-kept').length, 2);
assert.equal(keepFile.disabled, true);
console.log('diff panel ok');
