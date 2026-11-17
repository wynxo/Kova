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
const ui = S.create({storage: uiStorage, mac: false});
const host = new FakeNode('div');
host.ownerDocument = doc;
S.mount(host, ui);
const list = host.children[0];
const rowFor = description => list.children.find(row => row.children[0].textContent.includes(description));
const rowOf = id => rowFor(entry(id).description);
assert.equal(list.children.length, S.REGISTRY.length, 'one row per action');
assert.equal(host.children[1].children[0].disabled, true, 'Reset all starts disabled');

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
assert.equal(host.children[1].children[0].disabled, false, 'Reset all is live now');

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

console.log('Shortcut registry, overrides, conflicts, storage, dispatch and remap list passed');
