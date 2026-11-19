// Shortcut registry, overrides, conflicts and dispatch, with no browser or packages.
// shortcuts.js is plain logic, so it is loaded straight into a vm context here;
// the DOM half (the remap list) is exercised by the app itself.
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import vm from 'node:vm';

const sandbox = {console};
vm.runInNewContext(readFileSync(new URL('../src/kova/web_assets/shortcuts.js', import.meta.url), 'utf8'), sandbox);
const S = sandbox.KovaShortcuts;
assert.equal(typeof S, 'object', 'shortcuts.js exposes KovaShortcuts');
for (const name of ['create', 'actionFor', 'matches', 'planChange', 'conflicts',
                    'loadOverrides', 'saveOverrides', 'bindingFromEvent', 'labels', 'normalizeOverrides']) {
  if (name === 'registry') continue;
  assert.equal(typeof S[name], 'function', `shortcuts.js exposes ${name}`);
}

const entry = id => S.REGISTRY.find(item => item.id === id);
const press = (key, extra = {}) => ({key, ctrlKey: false, metaKey: false, shiftKey: false, altKey: false, ...extra});
// Values made inside the vm context have that realm's prototypes; compare plain data.
const plain = value => JSON.parse(JSON.stringify(value));
const same = (actual, expected, message) => assert.deepEqual(plain(actual), plain(expected), message);

// ---------------------------------------------------------------- registry
const ids = S.REGISTRY.map(item => item.id);
same(ids, [...new Set(ids)], 'action ids are unique');
assert.equal(new Set(S.REGISTRY.map(item => item.defaultKey)).size, S.REGISTRY.length,
             'no two defaults share a key');
for (const item of S.REGISTRY) {
  assert.equal(typeof item.description, 'string');
  assert.ok(item.description.length > 0, `${item.id} has a description`);
  assert.equal(S.formatBinding(item.defaultKey), item.defaultKey,
               `${item.id} default is written in the canonical form`);
}
// The chords main already ships, named explicitly so a rename cannot lose them.
assert.equal(entry('palette').defaultKey, 'mod+k', 'the command palette stays on Ctrl/Cmd+K');
assert.equal(entry('new-conversation').defaultKey, 'mod+shift+k', 'new conversation stays remapped');
assert.equal(entry('new-incognito').defaultKey, 'mod+shift+n', 'incognito keeps its chord');
assert.ok(entry('stop-reply'), 'stop-reply is an action');
for (const mac of [false, true]) {
  same(S.conflicts(S.resolveOverrides({}), {mac}), [],
                   `the defaults never collide (mac: ${mac})`);
}

// --------------------------------------------------------------- canonical
assert.equal(S.formatBinding('Shift+Mod+K'), 'mod+shift+k', 'mod is written lowercase, in order');
assert.equal(S.formatBinding('Ctrl+Shift+K'), 'ctrl+shift+k', 'a literal Ctrl stays Ctrl');
assert.equal(S.formatBinding('shift+mod+k'), 'mod+shift+k', 'modifiers are written in a fixed order');
assert.equal(S.formatBinding(' Cmd + K '), 'meta+k');
assert.equal(S.formatBinding('Space'), 'space');
assert.equal(S.formatBinding(' '), 'space');
assert.equal(S.formatBinding('mod+/'), 'mod+/');
assert.equal(S.formatBinding('mod+k+k'), null, 'one binding holds one key');
assert.equal(S.formatBinding('hyper+k'), null, 'an unknown modifier is refused');
assert.equal(S.formatBinding('mod+notakey'), null, 'an unknown key name is refused');
assert.equal(S.formatBinding('lshift'), null, 'a modifier on its own is not a binding');
assert.equal(S.formatBinding(''), null);
assert.equal(S.formatBinding(null), null);
assert.equal(S.formatBinding({}), null);

assert.equal(S.isUsable('mod+k'), true);
assert.equal(S.isUsable('escape'), true, 'a named key works alone');
assert.equal(S.isUsable('f5'), true);
assert.equal(S.isUsable('k'), false, 'a printable key needs a modifier');
assert.equal(S.isUsable('shift+k'), false, 'shift alone would steal typing');
assert.equal(S.isUsable('nonsense'), false);

// ------------------------------------------------------------------ labels
same(S.labels('mod+shift+k', {mac: false}), ['Ctrl', 'Shift', 'K']);
same(S.labels('mod+shift+k', {mac: true}), ['⌘', '⇧', 'K']);
same(S.labels('ctrl+alt+escape', {mac: true}), ['⌃', '⌥', 'Esc']);
same(S.labels('mod+space', {mac: false}), ['Ctrl', 'Space']);
same(S.labels('mod+/', {mac: false}), ['Ctrl', '/']);
same(S.labels('nonsense', {mac: false}), []);

// ----------------------------------------------------------- event matching
assert.equal(S.matches('mod+k', press('k', {ctrlKey: true}), {mac: false}), true);
assert.equal(S.matches('mod+k', press('K', {ctrlKey: true}), {mac: false}), true, 'the key is case-insensitive');
assert.equal(S.matches('mod+k', press('k', {metaKey: true}), {mac: false}), false, 'mod is not Meta on a PC');
assert.equal(S.matches('mod+k', press('k', {metaKey: true}), {mac: true}), true, 'mod is Meta on a Mac');
assert.equal(S.matches('mod+k', press('k', {ctrlKey: true}), {mac: true}), false, 'mod is not Ctrl on a Mac');
assert.equal(S.matches('mod+k', press('k', {ctrlKey: true, shiftKey: true}), {mac: false}), false,
             'a shifted key is a different chord');
assert.equal(S.matches('mod+shift+k', press('K', {ctrlKey: true, shiftKey: true}), {mac: false}), true);
assert.equal(S.matches('mod+shift+k', press('k', {ctrlKey: true}), {mac: false}), false);
assert.equal(S.matches('mod+k', press('k', {ctrlKey: true, metaKey: true}), {mac: false}), false,
             'an extra modifier does not match');
assert.equal(S.matches('escape', press('Escape'), {mac: false}), true);
assert.equal(S.matches('mod+space', press(' ', {ctrlKey: true}), {mac: false}), true);
assert.equal(S.matches('mod+/', press('/', {ctrlKey: true}), {mac: false}), true);

// A registered action answers through actionFor, and an override moves it.
assert.equal(S.actionFor(press('k', {ctrlKey: true}), {}, {mac: false}), 'palette');
assert.equal(S.actionFor(press('k', {ctrlKey: true, shiftKey: true}), {}, {mac: false}), 'new-conversation');
assert.equal(S.actionFor(press('n', {ctrlKey: true, shiftKey: true}), {}, {mac: false}), 'new-incognito');
assert.equal(S.actionFor(press('z', {ctrlKey: true}), {}, {mac: false}), null, 'an unbound chord does nothing');
const moved = {palette: 'mod+shift+p'};
assert.equal(S.actionFor(press('P', {ctrlKey: true, shiftKey: true}), moved, {mac: false}), 'palette');
assert.equal(S.actionFor(press('k', {ctrlKey: true}), moved, {mac: false}), null,
             'the old palette chord is free after the move');
assert.equal(S.actionFor(press('k', {ctrlKey: true, shiftKey: true}), moved, {mac: false}), 'new-conversation');

// ------------------------------------------------------------- conflicts
same(
  S.conflicts({palette: 'mod+k', 'toggle-sidebar': 'ctrl+k'}, {mac: false}),
  [{chord: 'ctrl+k', ids: ['palette', 'toggle-sidebar']}],
  'mod and ctrl are the same physical chord on a PC');
same(S.conflicts({palette: 'mod+k', 'toggle-sidebar': 'ctrl+k'}, {mac: true}), [],
                 'on a Mac Ctrl and Cmd are different chords');
same(
  S.conflicts({palette: 'mod+k', 'new-conversation': 'meta+k'}, {mac: true}),
  [{chord: 'meta+k', ids: ['palette', 'new-conversation']}]);
same(S.conflicts({}, {mac: false}), []);

// ------------------------------------------------------------ planChange
const clash = S.planChange('toggle-sidebar', 'mod+k', {}, {mac: false});
assert.equal(clash.ok, false);
assert.equal(clash.error, 'conflict');
same(clash.conflictWith, ['palette'], 'the conflict names the holder');
assert.equal(S.planChange('new-conversation', 'mod+k', {}, {mac: false}).error, 'conflict');
assert.equal(S.planChange('toggle-sidebar', 'mod+shift+k', {}, {mac: false}).conflictWith[0], 'new-conversation');
assert.equal(S.planChange('toggle-sidebar', 'meta+k', {}, {mac: false}).ok, true,
             'Meta+K is free on a PC');
assert.equal(S.planChange('toggle-sidebar', 'meta+k', {}, {mac: true}).error, 'conflict',
             'Meta+K is the palette on a Mac');

const plainKey = S.planChange('toggle-sidebar', 'k', {}, {mac: false});
assert.equal(plainKey.error, 'needs-modifier');
assert.equal(S.planChange('toggle-sidebar', 'hyper+j', {}, {mac: false}).error, 'invalid');
assert.equal(S.planChange('nope', 'mod+j', {}, {mac: false}).error, 'unknown-action');

const changed = S.planChange('toggle-sidebar', 'Mod+J', {}, {mac: false});
assert.equal(changed.ok, true);
assert.equal(changed.binding, 'mod+j');
same(changed.overrides, {'toggle-sidebar': 'mod+j'});
same(S.planChange('toggle-sidebar', 'mod+b', {}, {mac: false}).overrides, {},
                 'choosing the default again drops the override');
same(S.planChange('palette', 'mod+shift+p', {palette: 'mod+shift+p'}, {mac: false}).overrides,
                 {palette: 'mod+shift+p'}, 'a second change keeps the first');
assert.equal(S.planChange('palette', 'mod+k', {'toggle-sidebar': 'mod+j'}, {mac: false}).overrides['toggle-sidebar'],
             'mod+j', 'going back to the default leaves other overrides alone');

// --------------------------------------------------------------- storage
class MemoryStorage {
  constructor(values = {}) { this.values = {...values}; this.writes = 0; }
  getItem(key) { return Object.hasOwn(this.values, key) ? this.values[key] : null; }
  setItem(key, value) { this.writes += 1; this.values[key] = String(value); }
  removeItem(key) { delete this.values[key]; }
}

const loaded = S.loadOverrides(new MemoryStorage({
  [S.STORAGE_KEY]: JSON.stringify({
    palette: 'Shift+Mod+P',
    nope: 'mod+q',
    shortcuts: 'mod+/',            // the default, so not an override
    'toggle-sidebar': 'hyper+j',   // invalid
  }),
}));
same(loaded, {palette: 'mod+shift+p'},
                 'only known actions with valid, non-default keys are kept');
same(S.loadOverrides(new MemoryStorage({[S.STORAGE_KEY]: '{oops'})), {},
                 'broken JSON falls back to the defaults');
same(S.loadOverrides(new MemoryStorage({[S.STORAGE_KEY]: '[1,2]'})), {});
same(S.loadOverrides(null), {});
same(S.loadOverrides({getItem() { throw new Error('blocked'); }}), {});

const saved = new MemoryStorage();
S.saveOverrides(saved, {palette: 'Mod+Shift+P'});
assert.equal(saved.getItem(S.STORAGE_KEY), '{"palette":"mod+shift+p"}');
S.saveOverrides(saved, {palette: 'mod+k'});
assert.equal(saved.getItem(S.STORAGE_KEY), null, 'an empty override map clears the entry');
const broken = {getItem: () => null, setItem() { throw new Error('full'); }};
assert.doesNotThrow(() => S.saveOverrides(broken, {palette: 'mod+p'}));

// ---------------------------------------------------------------- instance
const storage = new MemoryStorage();
const app = S.create({storage, mac: false});
assert.equal(app.mac, false);
same(app.overrides(), {});
assert.equal(app.binding('palette'), 'mod+k');
same(app.labels('new-conversation'), ['Ctrl', 'Shift', 'K']);
assert.equal(app.actionFor(press('k', {ctrlKey: true})), 'palette');
assert.equal(app.all().find(item => item.id === 'palette').isDefault, true);
same(app.conflicts(), []);

let notified = 0;
const unsubscribe = app.subscribe(() => { notified += 1; });
const refused = app.set('toggle-sidebar', 'mod+k');
assert.equal(refused.ok, false);
assert.equal(refused.error, 'conflict');
assert.equal(notified, 0, 'a refused change saves nothing');
assert.equal(storage.getItem(S.STORAGE_KEY), null);
assert.equal(app.binding('toggle-sidebar'), 'mod+b');

assert.equal(app.set('toggle-sidebar', 'mod+j').ok, true);
assert.equal(notified, 1, 'a saved change notifies the panels');
assert.equal(app.binding('toggle-sidebar'), 'mod+j');
assert.equal(app.all().find(item => item.id === 'toggle-sidebar').isDefault, false);
assert.equal(app.actionFor(press('j', {ctrlKey: true})), 'toggle-sidebar');
assert.equal(app.actionFor(press('b', {ctrlKey: true})), null);
assert.equal(JSON.parse(storage.getItem(S.STORAGE_KEY))['toggle-sidebar'], 'mod+j',
             'the change is in preferences');

const reloaded = S.create({storage, mac: false});
assert.equal(reloaded.binding('toggle-sidebar'), 'mod+j', 'a new page starts with the saved keys');
assert.equal(reloaded.actionFor(press('j', {ctrlKey: true})), 'toggle-sidebar');

app.reset('toggle-sidebar');
assert.equal(app.binding('toggle-sidebar'), 'mod+b');
assert.equal(notified, 2);
assert.equal(app.overrides()['toggle-sidebar'], undefined);

app.set('toggle-sidebar', 'mod+j');
app.set('palette', 'mod+shift+p');
assert.equal(app.all().filter(item => !item.isDefault).length, 2);
app.resetAll();
assert.equal(notified, 5);
same(app.overrides(), {});
assert.equal(app.binding('palette'), 'mod+k');
assert.equal(storage.getItem(S.STORAGE_KEY), null);

const before = notified;
unsubscribe();
app.resetAll();
assert.equal(notified, before, 'an unsubscribed panel is not called again');

const macApp = S.create({storage: new MemoryStorage(), mac: true});
same(macApp.labels('palette'), ['⌘', 'K']);
assert.equal(macApp.actionFor(press('k', {metaKey: true})), 'palette');

// ------------------------------------------------------ recording from events
assert.equal(S.bindingFromEvent(press('k', {ctrlKey: true, shiftKey: true}), {mac: false}), 'mod+shift+k');
assert.equal(S.bindingFromEvent(press('K', {ctrlKey: true, shiftKey: true}), {mac: false}), 'mod+shift+k');
assert.equal(S.bindingFromEvent(press('k', {metaKey: true}), {mac: true}), 'mod+k');
assert.equal(S.bindingFromEvent(press('k', {ctrlKey: true}), {mac: true}), 'ctrl+k');
assert.equal(S.bindingFromEvent(press('k', {metaKey: true}), {mac: false}), 'meta+k');
assert.equal(S.bindingFromEvent(press('Control', {ctrlKey: true}), {mac: false}), null,
             'a modifier on its own records nothing');
assert.equal(S.bindingFromEvent(press('Shift', {shiftKey: true}), {mac: false}), null);
assert.equal(S.bindingFromEvent(press('Escape'), {mac: false}), 'escape');
assert.equal(S.bindingFromEvent(press(' ', {ctrlKey: true}), {mac: false}), 'mod+space');
assert.equal(S.bindingFromEvent({key: 'k', ctrlKey: true, isComposing: true}, {mac: false}), null);
assert.equal(S.bindingFromEvent(press('Dead'), {mac: false}), null);

// A recorded binding round-trips through the planner and the matcher.
const recorded = S.bindingFromEvent(press('p', {ctrlKey: true, altKey: true}), {mac: false});
assert.equal(recorded, 'mod+alt+p');
assert.equal(S.planChange('palette', recorded, {}, {mac: false}).ok, true);
assert.equal(S.matches(recorded, press('p', {ctrlKey: true, altKey: true}), {mac: false}), true);

// ------------------------------------------------------- the remap list
// The Settings rows record the next keypress, refuse a chord another action
// owns, and restore a default. A deliberately small DOM is enough for that.
class FakeNode {
  constructor(tag = 'div') {
    this.tagName = tag; this.className = ''; this.children = []; this.parentNode = null;
    this.listeners = {}; this.attrs = {}; this.hidden = false; this.disabled = false;
    this.type = ''; this.id = ''; this._text = '';
  }
  set textContent(text) { this._text = String(text); this.children = []; }
  get textContent() { return this._text + this.children.map(node => node.textContent).join(''); }
  append(...nodes) { for (const node of nodes) { node.parentNode = this; this.children.push(node); } }
  replaceChildren(...nodes) { this._text = ''; this.children = []; this.append(...nodes); }
  setAttribute(name, value) { this.attrs[name] = String(value); }
  getAttribute(name) { return Object.hasOwn(this.attrs, name) ? this.attrs[name] : null; }
  hasAttribute(name) { return Object.hasOwn(this.attrs, name); }
  remove() {
    if (!this.parentNode) return;
    const index = this.parentNode.children.indexOf(this);
    if (index >= 0) this.parentNode.children.splice(index, 1);
    this.parentNode = null;
  }
  querySelectorAll(selector) {
    const found = walkNodes(this).filter(node => node !== this);
    if (selector === '[data-shortcut-row]') return found.filter(node => node.hasAttribute('data-shortcut-row'));
    if (selector === '[data-shortcut]') return found.filter(node => node.hasAttribute('data-shortcut'));
    return [];
  }
  addEventListener(type, fn) { (this.listeners[type] ||= []).push(fn); }
  removeEventListener(type, fn) {
    this.listeners[type] = (this.listeners[type] || []).filter(listener => listener !== fn);
  }
}
const doc = {
  listeners: {},
  createElement: tag => new FakeNode(tag),
  addEventListener(type, fn) { (this.listeners[type] ||= []).push(fn); },
  removeEventListener(type, fn) {
    this.listeners[type] = (this.listeners[type] || []).filter(listener => listener !== fn);
  },
};
const walkNodes = node => [node, ...node.children.flatMap(walkNodes)];
const fireKey = event => {
  let prevented = 0, stopped = 0;
  for (const listener of [...(doc.listeners.keydown || [])]) {
    listener({...event, preventDefault() { prevented += 1; }, stopPropagation() { stopped += 1; }});
  }
  return {prevented, stopped};
};
const uiStorage = new MemoryStorage();
const ui = S.create({storage: uiStorage, mac: false, sync: false});
const host = new FakeNode('div');
host.ownerDocument = doc;
S.mount(host, ui);
const presetRow = host.children[0];
const list = host.children[1];
const foot = host.children[2];
const presetSelect = presetRow.children[2];
const customOption = presetSelect.children[3];
const rowFor = description => list.children.find(row => row.children[0].textContent.includes(description));
const rowOf = id => rowFor(entry(id).description);
assert.equal(list.children.length, S.REGISTRY.length, 'one row per action');
assert.equal(presetSelect.value, 'default', 'the default preset starts selected');
assert.equal(customOption.disabled, true, 'Custom is a state, not a choice');
assert.equal(foot.children[0].disabled, true, 'Reset all starts disabled');

// Clicking the keys starts recording, and the next chord is saved at once.
const conversation = rowOf('new-conversation');
conversation.children[1].listeners.click[0]();
assert.equal(conversation.children[1].textContent, 'Press keys…');
const savedKeys = fireKey({key: 'J', ctrlKey: true, altKey: true});
assert.equal(savedKeys.prevented, 1, 'the recorded key is not typed into the page');
assert.equal(ui.binding('new-conversation'), 'mod+alt+j');
assert.equal(JSON.parse(uiStorage.getItem(S.STORAGE_KEY))['new-conversation'], 'mod+alt+j');
assert.equal(conversation.children[1].textContent, 'CtrlAltJ', 'the row shows the new keys');
assert.equal(conversation.children[2].hidden, false, 'a changed row offers Reset');
assert.equal(foot.children[0].disabled, false, 'Reset all is live now');
assert.equal(presetSelect.value, 'custom', 'recording a key leaves the preset');
assert.equal(customOption.disabled, false, 'Custom is now the current choice');

// A chord another action owns is refused, named, and never saved.
const paletteRow = rowOf('palette');
paletteRow.children[1].listeners.click[0]();
const clashKeys = fireKey({key: 'j', ctrlKey: true, altKey: true});
assert.equal(clashKeys.prevented, 1);
assert.equal(ui.binding('palette'), 'mod+k', 'the refused chord changes nothing');
const error = walkNodes(paletteRow).find(node => node.className === 'settings-row-help shortcut-error');
assert.equal(error.hidden, false, 'the clash is explained under the row');
assert.ok(error.textContent.includes('Already used by'), error.textContent);
assert.ok(error.textContent.includes('New conversation'), 'the row names the holder');
assert.equal(paletteRow.children[1].textContent, 'CtrlK', 'the old keys are back');

// Escape cancels a recording; Reset restores one default; Reset all clears them.
paletteRow.children[1].listeners.click[0]();
fireKey({key: 'Escape'});
assert.equal(ui.binding('palette'), 'mod+k');
conversation.children[2].listeners.click[0]();
assert.equal(ui.binding('new-conversation'), 'mod+shift+k');
same(ui.overrides(), {});
assert.equal(presetSelect.value, 'default', 'resetting the last key returns to Default');

// Picking a preset replaces the whole map; Custom is not a choice.
presetSelect.value = 'vim';
presetSelect.listeners.change[0]();
assert.equal(ui.binding('next-conversation'), 'alt+j');
assert.equal(ui.binding('previous-conversation'), 'alt+k');
assert.equal(ui.binding('palette'), 'mod+shift+p');
assert.equal(presetSelect.value, 'vim');
assert.equal(customOption.disabled, true);
presetSelect.value = 'custom';
presetSelect.listeners.change[0]();
assert.equal(ui.binding('palette'), 'mod+shift+p', 'Custom cannot be picked, so nothing changes');
assert.equal(presetSelect.value, 'vim', 'the select snaps back to the preset in use');
presetSelect.value = 'emacs';
presetSelect.listeners.change[0]();
assert.equal(ui.binding('next-conversation'), 'alt+n', 'Emacs-ish moves down with M-n');
assert.equal(ui.binding('previous-conversation'), 'alt+p', 'and up with M-p');
presetSelect.value = 'default';
presetSelect.listeners.change[0]();
same(ui.overrides(), {});

// ---------------------------------------------------------------- presets
same(S.PRESET_NAMES, ['default', 'vim', 'emacs']);
for (const name of S.PRESET_NAMES) {
  const preset = S.PRESETS[name];
  assert.equal(typeof preset.label, 'string', `${name} has a label`);
  for (const [id, binding] of Object.entries(preset.overrides)) {
    assert.ok(entry(id), `${name} only changes known actions`);
    assert.equal(S.formatBinding(binding), binding, `${name}: ${id} is canonical`);
    assert.equal(S.isUsable(binding), true, `${name}: ${id} never steals typing`);
  }
  for (const mac of [false, true]) {
    same(S.conflicts(S.resolveOverrides(preset.overrides), {mac}), [],
         `${name} never collides (mac: ${mac})`);
  }
}
same(S.PRESETS.default.overrides, {});
assert.equal(S.PRESETS.vim.overrides['next-conversation'], 'alt+j');
assert.equal(S.PRESETS.vim.overrides['previous-conversation'], 'alt+k');
assert.equal(S.PRESETS.emacs.overrides['next-conversation'], 'alt+n');
assert.equal(S.PRESETS.emacs.overrides['previous-conversation'], 'alt+p');
assert.equal(S.presetOf({}), 'default');
assert.equal(S.presetOf(S.PRESETS.vim.overrides), 'vim');
assert.equal(S.presetOf(S.PRESETS.emacs.overrides), 'emacs');
assert.equal(S.presetOf({palette: 'mod+shift+p'}), 'custom', 'one preset key alone is Custom');
assert.equal(S.presetOf({palette: 'mod+k'}), 'default', 'a default entry is not an override');

const presetApp = S.create({storage: new MemoryStorage(), mac: false, sync: false});
assert.equal(presetApp.preset(), 'default');
assert.equal(presetApp.applyPreset('nope').error, 'unknown-preset');
assert.equal(presetApp.applyPreset('vim').ok, true);
assert.equal(presetApp.preset(), 'vim');
assert.equal(presetApp.binding('next-conversation'), 'alt+j');
assert.equal(presetApp.actionFor(press('j', {altKey: true})), 'next-conversation');
assert.equal(presetApp.set('toggle-theme', 'mod+alt+t').ok, true);
assert.equal(presetApp.preset(), 'custom', 'recording one key leaves the preset');
assert.equal(presetApp.applyPreset('emacs').ok, true);
assert.equal(presetApp.binding('toggle-theme'), 'mod+shift+l', 'a preset replaces the whole map');
assert.equal(presetApp.applyPreset('default').ok, true);
same(presetApp.overrides(), {});

// A preset's plain Alt keys never steal typing, but a command chord still works.
assert.equal(S.actionFor(press('ArrowDown', {altKey: true}), {}, {mac: false}), 'next-conversation');
assert.equal(S.actionFor(press('ArrowUp', {altKey: true}), {}, {mac: false}), 'previous-conversation');
const guardApp = S.create({storage: new MemoryStorage(), mac: false, sync: false});
guardApp.applyPreset('vim');
assert.equal(guardApp.actionFor(press('j', {altKey: true})), 'next-conversation');
assert.equal(guardApp.actionFor(press('j', {altKey: true}), {guardTyping: true}), null,
             'Alt+J inside a text field stays typing');
assert.equal(guardApp.actionFor(press('b', {ctrlKey: true}), {guardTyping: true}), 'toggle-sidebar',
             'a Ctrl chord still fires while typing');
assert.equal(guardApp.actionFor(press('Escape'), {guardTyping: true}), 'stop-reply',
             'Escape still stops the reply');

// ------------------------------------------------------------ dialog search
const dialogList = new FakeNode('dl');
const typingRow = new FakeNode('div');
const typingTerm = new FakeNode('dt');
typingTerm.textContent = 'Send the message';
const typingKeys = new FakeNode('dd');
const enterKey = new FakeNode('kbd');
enterKey.textContent = 'Enter';
typingKeys.append(enterKey);
typingRow.append(typingTerm, typingKeys);
dialogList.append(typingRow);
const search = new FakeNode('input');
search.value = '';
const emptyNote = new FakeNode('p');
emptyNote.hidden = true;
const dialogPage = {
  createElement: tag => new FakeNode(tag),
  getElementById: id => ({
    'shortcut-list': dialogList, 'shortcut-search': search, 'shortcut-empty': emptyNote,
  })[id] || null,
};
const visibleRows = () => dialogList.children.filter(row => !row.hidden);
S.renderDialog(presetApp, {doc: dialogPage});
assert.equal(visibleRows().length, S.REGISTRY.length + 1, 'every action and the typing keys show');
assert.equal(emptyNote.hidden, true);

S.renderDialog(presetApp, {doc: dialogPage, query: 'sidebar'});
assert.equal(visibleRows().length, 1, 'a name filters the list');
assert.equal(visibleRows()[0].getAttribute('data-shortcut-row'), 'toggle-sidebar');

S.renderDialog(presetApp, {doc: dialogPage, query: 'ctrl b'});
assert.equal(visibleRows()[0].getAttribute('data-shortcut-row'), 'toggle-sidebar',
             'the keys are searchable too');

S.renderDialog(presetApp, {doc: dialogPage, query: 'ctrl+b'});
assert.equal(visibleRows()[0].getAttribute('data-shortcut-row'), 'toggle-sidebar',
             'a chord typed with + is the same search');

S.renderDialog(presetApp, {doc: dialogPage, query: 'sEnd'});
assert.equal(visibleRows().length, 1);
assert.equal(visibleRows()[0], typingRow, 'the typing keys filter with the list');

S.renderDialog(presetApp, {doc: dialogPage, query: 'nothing-here'});
assert.equal(visibleRows().length, 0);
assert.equal(emptyNote.hidden, false, 'an empty result says so');
assert.ok(emptyNote.textContent.includes('nothing-here'), emptyNote.textContent);

search.value = 'light';
S.renderDialog(presetApp, {doc: dialogPage});
assert.equal(visibleRows().length, 1, 'the dialog reads its own search box');
assert.equal(visibleRows()[0].getAttribute('data-shortcut-row'), 'toggle-theme');
search.value = '';
S.renderDialog(presetApp, {doc: dialogPage});
assert.equal(visibleRows().length, S.REGISTRY.length + 1);
assert.equal(emptyNote.hidden, true);

// --------------------------------------------------------- keys on the page
assert.equal(S.ariaKeyshortcuts('mod+shift+k'), 'Control+Shift+K Meta+Shift+K');
assert.equal(S.ariaKeyshortcuts('escape'), 'Escape');
assert.equal(S.ariaKeyshortcuts('ctrl+alt+arrowup'), 'Control+Alt+ArrowUp');
assert.equal(S.ariaKeyshortcuts('nonsense'), '');

const newChatButton = new FakeNode('button');
newChatButton.setAttribute('data-shortcut', 'new-conversation');
newChatButton.textContent = 'New chat';
const toggleButton = new FakeNode('button');
toggleButton.setAttribute('data-shortcut', 'toggle-sidebar');
toggleButton.setAttribute('title', 'Toggle sidebar');
toggleButton.textContent = 'Toggle sidebar';
const hintPage = {
  querySelectorAll: selector => (selector === '[data-shortcut]'
    ? [newChatButton, toggleButton] : []),
};
S.applyHints(presetApp, hintPage);
assert.equal(newChatButton.getAttribute('aria-keyshortcuts'), 'Control+Shift+K Meta+Shift+K');
assert.equal(newChatButton.getAttribute('title'), 'New chat (Ctrl Shift K)',
             'the tooltip carries the keys');
assert.equal(toggleButton.getAttribute('title'), 'Toggle sidebar (Ctrl B)');
presetApp.set('toggle-sidebar', 'mod+j');
S.applyHints(presetApp, hintPage);
assert.equal(toggleButton.getAttribute('title'), 'Toggle sidebar (Ctrl J)',
             'the hint follows a recorded key');
assert.equal(toggleButton.getAttribute('aria-keyshortcuts'), 'Control+J Meta+J');
assert.equal(toggleButton.getAttribute('data-shortcut-title'), 'Toggle sidebar',
             'the original title is kept once');
presetApp.resetAll();

// -------------------------------------------------------------- shared sync
function fakeServer(shortcuts = {}) {
  const calls = [];
  const fetchImpl = async (url, options = {}) => {
    calls.push({url, options});
    if ((options.method || 'GET') === 'GET') {
      return {ok: true, json: async () => ({theme: 'system', shortcuts})};
    }
    return {ok: true, json: async () => ({})};
  };
  const puts = () => calls.filter(call => call.options.method === 'PUT');
  return {fetchImpl, calls, puts};
}

// The shared document wins after the first paint, and becomes the next cache.
const sharedServer = fakeServer({palette: 'mod+shift+p'});
const sharedCache = new MemoryStorage();
const shared = S.create({storage: sharedCache, mac: false, fetchImpl: sharedServer.fetchImpl});
assert.equal(shared.binding('palette'), 'mod+k', 'the cached first paint still has the defaults');
await shared.ready;
assert.equal(shared.binding('palette'), 'mod+shift+p', 'the shared document arrives and wins');
assert.equal(JSON.parse(sharedCache.getItem(S.STORAGE_KEY)).palette, 'mod+shift+p',
             'the shared keys become the next first paint');
assert.equal(sharedCache.getItem(S.SYNCED_KEY), '1');
assert.equal(sharedServer.puts().length, 0, 'adopting does not write the server back');

// Keys from before sync are sent once, into an empty shared document.
const oldCache = new MemoryStorage({[S.STORAGE_KEY]: JSON.stringify({palette: 'mod+alt+k'})});
const emptyServer = fakeServer({});
const migrated = S.create({storage: oldCache, mac: false, fetchImpl: emptyServer.fetchImpl});
await migrated.ready;
assert.equal(emptyServer.puts().length, 1, 'the local keys are sent once');
assert.equal(JSON.parse(emptyServer.puts()[0].options.body).shortcuts.palette, 'mod+alt+k');
assert.equal(migrated.binding('palette'), 'mod+alt+k');
assert.equal(oldCache.getItem(S.SYNCED_KEY), '1');

const secondServer = fakeServer({palette: 'mod+alt+k'});
const again = S.create({storage: oldCache, mac: false, fetchImpl: secondServer.fetchImpl});
await again.ready;
assert.equal(secondServer.puts().length, 0, 'the migration happens once');
assert.equal(again.binding('palette'), 'mod+alt+k');

// A recorded key is written to the shared document as it is saved.
const liveServer = fakeServer({palette: 'mod+shift+p'});
const liveApp = S.create({storage: new MemoryStorage(), mac: false,
                          fetchImpl: liveServer.fetchImpl});
await liveApp.ready;
liveApp.set('toggle-sidebar', 'mod+j');
assert.equal(liveServer.puts().length, 1);
assert.equal(JSON.parse(liveServer.puts()[0].options.body).shortcuts['toggle-sidebar'], 'mod+j');

// Offline and a key recorded before the first reply both keep the local keys.
const offlineCache = new MemoryStorage({[S.STORAGE_KEY]: JSON.stringify({palette: 'mod+alt+k'})});
const offline = S.create({storage: offlineCache, mac: false,
                          fetchImpl: async () => { throw new Error('offline'); }});
await offline.ready;
assert.equal(offline.binding('palette'), 'mod+alt+k', 'offline keeps the cached keys');

let reply;
const earlyWrites = [];
const early = S.create({
  storage: new MemoryStorage(), mac: false,
  fetchImpl: (url, options = {}) => {
    if (options.method === 'PUT') { earlyWrites.push(options.body); return Promise.resolve({ok: true}); }
    return new Promise(resolve => { reply = resolve; });
  },
});
early.set('palette', 'mod+alt+k');
reply({ok: true, json: async () => ({shortcuts: {palette: 'mod+shift+p'}})});
await early.ready;
assert.equal(early.binding('palette'), 'mod+alt+k', 'a key recorded before the first reply stays');
assert.equal(earlyWrites.length, 1, 'and it is sent to the server');

console.log('Shortcut registry, overrides, conflicts, storage, dispatch, presets, '
  + 'dialog search, page hints and shared sync passed');
