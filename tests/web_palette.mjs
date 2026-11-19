// Command palette behavior, with no browser and no packages.
// The fake DOM is deliberately small: the palette only needs elements, text,
// attributes, listeners and a focus target.
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import vm from 'node:vm';

let focused = null;
class Element {
  constructor(tag = 'div') {
    this.tagName = tag; this.id = ''; this.className = ''; this.children = []; this.parentNode = null;
    this.listeners = {}; this.attrs = {}; this.hidden = false; this.open = false; this.type = '';
    this.placeholder = ''; this.value = ''; this.autocomplete = ''; this.spellcheck = true;
  }
  set textContent(text) { this._text = text; this.children = []; }
  get textContent() { return (this._text || '') + this.children.map(node => node.textContent).join(''); }
  append(...nodes) { for (const node of nodes) { node.parentNode = this; this.children.push(node); } }
  replaceChildren(...nodes) { this._text = ''; this.children = []; this.append(...nodes); }
  setAttribute(name, value) { this.attrs[name] = String(value); }
  getAttribute(name) { return this.attrs[name]; }
  removeAttribute(name) { delete this.attrs[name]; }
  addEventListener(type, handler) { this.listeners[type] = handler; }
  focus() { focused = this; }
  scrollIntoView() {}
  showModal() { this.open = true; this.attrs.open = ''; }
  close() {
    if (!this.open) return;
    this.open = false; delete this.attrs.open;
    this.listeners.close?.();
  }
}

function makeDocument() {
  const body = new Element('body');
  return {
    body,
    createElement: tag => new Element(tag),
    get activeElement() { return focused; },
  };
}

const walk = node => [node, ...node.children.flatMap(walk)];
const options = dialog => walk(dialog).filter(node => node.getAttribute?.('role') === 'option');
const selected = dialog => options(dialog).filter(node => node.getAttribute('aria-selected') === 'true');
const key = (dialog, name, extra = {}) => {
  let prevented = 0;
  dialog.listeners.keydown({key: name, preventDefault() { prevented += 1; }, ...extra});
  return prevented;
};

const sandbox = {console};
vm.runInNewContext(readFileSync(new URL('../src/kova/web_assets/palette.js', import.meta.url), 'utf8'), sandbox);
const {fuzzyScore, rankItems, create} = sandbox.KovaPalette;
assert.equal(typeof create, 'function', 'palette.js exposes create');
assert.equal(typeof fuzzyScore, 'function');
assert.equal(typeof rankItems, 'function');

// Fuzzy scoring: subsequences match, unrelated text does not, word starts win.
assert.notEqual(fuzzyScore('nwcn', 'New conversation'), null, 'a subsequence matches');
assert.equal(fuzzyScore('zzzz', 'New conversation'), null, 'an unrelated query does not match');
assert.equal(fuzzyScore('', 'anything'), 0, 'an empty query is neutral');
assert.ok(fuzzyScore('new', 'New conversation') > fuzzyScore('nwcn', 'New conversation'),
          'a prefix match beats a loose subsequence');
assert.ok(fuzzyScore('theme', 'Toggle theme') > fuzzyScore('theme', 'A much longer title with theme inside it'),
          'a shorter target is not drowned out by a long one');

const registry = [
  {group: 'Actions', title: 'New conversation', hint: 'Start', keywords: 'create compose',
   shortcut: ['Ctrl', 'Shift', 'K'], run() { calls.push('new'); }},
  {group: 'Actions', title: 'Toggle theme', hint: 'Light and dark', keywords: 'colour mode',
   run() { calls.push('theme'); }},
  {group: 'Settings', title: 'Privacy', hint: 'Network report', keywords: 'local only',
   run() { calls.push('privacy'); }},
  {group: 'Conversations', title: 'Quarterly notes', hint: 'Work', keywords: 'chat history',
   run() { calls.push('chat'); }},
];
const calls = [];

assert.deepEqual([...rankItems(registry, '')], registry, 'an empty query keeps registry order');
assert.deepEqual([...rankItems(registry, 'nwcn')].map(item => item.title), ['New conversation']);
assert.deepEqual([...rankItems(registry, 'thm')].map(item => item.title), ['Toggle theme']);
assert.deepEqual([...rankItems(registry, 'prvcy')].map(item => item.title), ['Privacy']);
assert.deepEqual([...rankItems(registry, 'zzzz')], []);
// A row the server already matched (a snippet hit) stays even though the query
// is not in its own title or keywords.
assert.deepEqual([...rankItems([...registry, {group: 'In messages', title: 'Quarterly notes', alwaysMatch: true}], 'zzzz')]
  .map(item => item.title), ['Quarterly notes']);
{
  const names = [...rankItems(registry, 'e')].map(item => item.group);
  const seen = new Set();
  let last = null;
  for (const name of names) {
    if (name !== last) assert.equal(seen.has(name), false, `group ${name} repeated`);
    seen.add(name); last = name;
  }
  assert.equal(names.length, 3, 'a broad query still finds the matching rows');
}

const doc = makeDocument();
const palette = create({doc, items: () => registry, onError: error => { throw error; }});
assert.equal(palette.dialog.parentNode, doc.body, 'the dialog is attached to the body');
assert.equal(palette.dialog.id, 'command-palette');
assert.equal(palette.dialog.getAttribute('aria-labelledby'), 'command-palette-title');
assert.equal(palette.input.getAttribute('role'), 'combobox');
assert.equal(palette.input.getAttribute('aria-controls'), 'command-palette-results');
assert.equal(palette.input.getAttribute('aria-expanded'), 'false', 'closed until opened');
assert.equal(palette.isOpen(), false);

// Opening focuses the search box, renders grouped rows and selects the first one.
const restore = new Element('button');
restore.focus();
palette.open();
assert.equal(palette.isOpen(), true);
assert.equal(palette.dialog.open, true);
assert.equal(focused, palette.input, 'focus lands in the search box');
assert.equal(palette.input.getAttribute('aria-expanded'), 'true');
const groups = walk(palette.dialog).filter(node => node.getAttribute?.('role') === 'group');
assert.equal(groups.length, 3, 'one group per registry group');
assert.deepEqual(groups.map(group => group.getAttribute('aria-labelledby')),
                 ['command-palette-group-0', 'command-palette-group-2', 'command-palette-group-3']);
assert.equal(options(palette.dialog).length, 4);
assert.equal(selected(palette.dialog).length, 1);
assert.equal(palette.input.getAttribute('aria-activedescendant'), options(palette.dialog)[0].id);
assert.equal(walk(palette.dialog).find(node => node.className === 'palette-status').textContent,
             '4 results', 'the result count is announced');

// Typing filters with the fuzzy matcher and keeps exactly one active row.
palette.input.value = 'thm';
palette.input.listeners.input();
assert.deepEqual(options(palette.dialog).map(node => node.textContent.split('Light')[0]), ['Toggle theme']);
assert.equal(walk(palette.dialog).find(node => node.className === 'palette-status').textContent,
             '1 result');
assert.equal(walk(palette.dialog).find(node => node.className === 'palette-empty').hidden, true);

// Arrows wrap, Home/End jump, Enter runs the active row and closes the dialog.
const activeTitle = dialog => selected(dialog)[0]?.textContent || '';
assert.equal(activeTitle(palette.dialog).startsWith('Toggle theme'), true);
const only = options(palette.dialog)[0].id;
key(palette.dialog, 'ArrowDown');
assert.equal(palette.input.getAttribute('aria-activedescendant'), only, 'a single result stays active');
palette.input.value = '';
palette.input.listeners.input();
assert.equal(activeTitle(palette.dialog).startsWith('New conversation'), true);
key(palette.dialog, 'End');
assert.equal(activeTitle(palette.dialog).startsWith('Quarterly notes'), true);
key(palette.dialog, 'ArrowDown');
assert.equal(activeTitle(palette.dialog).startsWith('New conversation'), true, 'Down wraps to the top');
key(palette.dialog, 'ArrowUp');
assert.equal(activeTitle(palette.dialog).startsWith('Quarterly notes'), true, 'Up wraps to the bottom');
key(palette.dialog, 'Home');
assert.equal(activeTitle(palette.dialog).startsWith('New conversation'), true);
assert.equal(key(palette.dialog, 'Enter'), 1, 'Enter is handled');
assert.equal(palette.isOpen(), false, 'running a row closes the palette');
assert.deepEqual(calls, ['new']);
assert.equal(focused, restore, 'focus returns to where it came from');

// Escape closes, Tab stays trapped in the field, and reopening starts clean.
palette.open();
assert.equal(palette.input.value, '', 'reopening clears the query');
palette.input.value = 'privacy';
palette.input.listeners.input();
assert.equal(key(palette.dialog, 'Escape'), 1);
assert.equal(palette.isOpen(), false);
palette.open();
assert.equal(key(palette.dialog, 'Tab'), 1, 'Tab is intercepted');
assert.equal(focused, palette.input, 'Tab cannot leave the dialog');
key(palette.dialog, 'Enter');

// No matches: the empty state is shown, the count says so, and Enter is inert.
palette.open();
palette.input.value = 'zzzzz';
palette.input.listeners.input();
assert.equal(options(palette.dialog).length, 0);
assert.equal(walk(palette.dialog).find(node => node.className === 'palette-empty').hidden, false);
assert.equal(walk(palette.dialog).find(node => node.className === 'palette-status').textContent,
             'No results');
assert.equal(palette.input.getAttribute('aria-activedescendant'), undefined);
assert.equal(key(palette.dialog, 'Enter'), 0, 'Enter does nothing without a row');

// refresh() re-reads the registry without losing the query.
registry.push({group: 'Settings', title: 'Privacy report export', keywords: 'network',
               run() { calls.push('export'); }});
palette.refresh();
assert.equal(options(palette.dialog).length, 0, 'the current query still filters');
assert.equal(walk(palette.dialog).find(node => node.className === 'palette-status').textContent,
             'No results');
palette.input.value = 'privacy';
palette.input.listeners.input();
palette.refresh();
assert.equal(options(palette.dialog).length, 2, 'a refreshed registry is visible at once');
assert.equal(palette.input.value, 'privacy', 'refresh keeps the query');

// Clicking a row runs it just like Enter.
options(palette.dialog)[1].onclick();
assert.equal(palette.isOpen(), false);
assert.deepEqual(calls, ['new', 'new', 'export']);

// A failing action is reported, never thrown into the key handler.
registry.push({group: 'Actions', title: 'Explode', run() { throw new Error('boom'); }});
palette.open();
palette.input.value = 'explode';
palette.input.listeners.input();
let reported = null;
const failing = create({doc: makeDocument(), items: () => registry, onError: error => { reported = error; }});
failing.open();
failing.input.value = 'explode';
failing.input.listeners.input();
key(failing.dialog, 'Enter');
assert.equal(reported?.message, 'boom');
assert.equal(failing.isOpen(), false);

// Live preview (#183): the highlighted row previews, moving on ends it, Escape
// reverts without running anything, and Enter reverts before the row commits.
{
  const events = [];
  const previews = [
    {group: 'Appearance', title: 'Theme: Light', previewKey: 'light', keywords: 'theme',
     preview() { events.push('preview light'); }, previewEnd() { events.push('end light'); },
     run() { events.push('run light'); }},
    {group: 'Appearance', title: 'Theme: Dark', previewKey: 'dark', keywords: 'theme',
     preview() { events.push('preview dark'); }, previewEnd() { events.push('end dark'); },
     run() { events.push('run dark'); }},
  ];
  const previewDoc = makeDocument();
  const previewPalette = create({doc: previewDoc, items: () => previews.map(row => ({...row}))});
  previewPalette.open();
  assert.deepEqual(events, ['preview light'], 'the first highlighted row previews at once');
  // A rebuild (a keystroke) keeps the same preview instead of restarting it.
  previewPalette.refresh();
  assert.deepEqual(events, ['preview light'], 'the same previewKey is not restarted');
  key(previewPalette.dialog, 'ArrowDown');
  assert.deepEqual(events, ['preview light', 'end light', 'preview dark'],
                   'moving the highlight reverts the old row and previews the new one');
  key(previewPalette.dialog, 'Escape');
  assert.deepEqual(events, ['preview light', 'end light', 'preview dark', 'end dark'],
                   'Escape reverts the preview');
  assert.equal(previewPalette.isOpen(), false);
  previewPalette.open();
  key(previewPalette.dialog, 'Enter');
  assert.deepEqual(events.slice(-3), ['preview light', 'end light', 'run light'],
                   'Enter reverts the preview and then commits the row');
}

// A query the palette cannot resolve itself (server search) is handed back.
{
  const seen = [];
  const searchDoc = makeDocument();
  const searchPalette = create({doc: searchDoc, items: () => registry, onQuery: value => seen.push(value)});
  searchPalette.open();
  searchPalette.input.value = 'quarterly';
  searchPalette.input.listeners.input();
  assert.deepEqual(seen, ['quarterly'], 'every keystroke reports the query');
}

console.log('Palette focus, ARIA, fuzzy filtering, keyboard, preview and registry refresh passed');
