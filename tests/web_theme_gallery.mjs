// Exercise gallery cards through the actual dialog and button handlers.
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import vm from 'node:vm';

const source = readFileSync(new URL('../src/kova/web_assets/theme-gallery.js', import.meta.url), 'utf8');

class Node {
  constructor(tag) {
    this.tag = tag;
    this.children = [];
    this.attributes = {};
    this.events = {};
    this.style = {setProperty(name, value) { this[name] = value; }};
    this.textContent = '';
  }
  append(...children) {
    for (const child of children) child.parent = this;
    this.children.push(...children);
  }
  contains(node) { return this === node || this.children.some(child => child.contains(node)); }
  remove() { this.parent.children = this.parent.children.filter(node => node !== this); }
  setAttribute(name, value) { this.attributes[name] = value; }
  removeAttribute(name) { delete this.attributes[name]; }
  addEventListener(name, listener) { this.events[name] = listener; }
  fire(name, event = {}) { return this.events[name]?.({target: this, preventDefault() {}, ...event}); }
  matches(selector) { return selector === ':focus-visible' && this.keyboardFocus; }
  focus() { document.activeElement = this; }
  showModal() { this.open = true; }
  close() { this.open = false; this.fire('close'); }
}

const tick = () => new Promise(resolve => setImmediate(resolve));
const document = {body: new Node('body'), createElement: tag => new Node(tag)};
const opener = new Node('button');
const events = [];
const calls = [];
let chosen = 'dark';
const timers = [];
const fakeTimers = {
  setTimeout: (fn, ms) => { const timer = {fn, ms, live: true}; timers.push(timer); return timer; },
  clearTimeout: timer => { if (timer) timer.live = false; },
  setInterval: (fn, ms) => { const timer = {fn, ms, live: true, every: true}; timers.push(timer); return timer; },
  clearInterval: timer => { if (timer) timer.live = false; },
};
const live = () => timers.filter(timer => timer.live);
const theme = {
  choice: () => chosen,
  preview: id => events.push(['installed preview', id]),
  endPreview: () => events.push(['end installed preview']),
  previewPalette: palette => events.push(['palette preview', palette.tokens]),
  clearPalettePreview: () => events.push(['end palette preview']),
  refresh: async () => events.push(['refresh']),
  set: async id => { chosen = id; events.push(['use', id]); },
};
const forest = {id: 'forest', name: 'Forest', appearance: 'dark', installed: false,
  description: 'Green accent', swatches: ['#14121c', '#5fd787'], tokens: {'accent.default': '#5fd787'}};
const paper = {id: 'paper', name: 'Paper <example>', appearance: 'light', installed: true,
  description: 'Warm background', swatches: ['#fbfaf7'], tokens: {'bg.base': '#fbfaf7'}};
let rows = [forest, paper];
let installFails = true;
const window = {KovaTheme: theme,
  KovaThemePreview: {card: tokens => {
    const node = new Node('div');
    node.className = 'theme-preview';
    node.setAttribute('aria-hidden', 'true');
    node.style.setProperty('--preview-accent-default', tokens['accent.default']);
    return node;
  }}};
vm.runInNewContext(source, {
  window, document, ...fakeTimers,
  fetch: async (url, options) => {
    calls.push([url, options?.method]);
    if (url === '/themes/gallery') return {ok: true, json: async () => rows};
    assert.equal(url, '/themes/gallery/forest/install');
    if (installFails) return {ok: false, json: async () => ({detail: 'Theme ID already exists.'})};
    return {ok: true, json: async () => ({id: 'forest'})};
  },
});

function find(root, predicate) {
  return predicate(root) ? root : root.children.map(child => find(child, predicate)).find(Boolean);
}
const action = (card, label) => find(card, node => node.tag === 'button' && node.textContent === label);
window.KovaThemeGallery.open(opener);
await tick();
const dialog = document.body.children[0];
assert.equal(dialog.tag, 'dialog');
assert.equal(dialog.open, true);
assert.equal(dialog.attributes['aria-labelledby'], 'theme-gallery-title');
const grid = find(dialog, node => node.className === 'theme-gallery-grid');
assert.deepEqual(grid.children.map(card => card.tag), ['li', 'li']);
const [forestCard, paperCard] = grid.children;
assert.ok(find(forestCard, node => node.textContent === 'Dark'));
assert.ok(find(paperCard, node => node.textContent === 'Light'));
assert.ok(find(paperCard, node => node.textContent === paper.name), 'names render as literal text');
assert.ok(find(forestCard, node => node.className === 'theme-preview'), 'each card carries a preview card');
assert.equal(find(forestCard, node => node.className === 'theme-preview').style['--preview-accent-default'], forest.swatches[1]);
assert.ok(!find(forestCard, node => node.className === 'theme-gallery-swatches'), 'the four-swatch strip is gone');
const install = action(forestCard, 'Install');
const usePaper = action(paperCard, 'Use');
const previewButton = action(forestCard, 'Preview');
assert.equal(install.attributes['aria-label'], 'Install Forest');
assert.equal(usePaper.attributes['aria-label'], `Use ${paper.name}`);

forestCard.fire('pointerenter', {pointerType: 'mouse'});
assert.deepEqual(events.at(-1), ['palette preview', forest.tokens]);
forestCard.fire('pointerleave');
assert.deepEqual(events.at(-1), ['end palette preview']);
forestCard.fire('pointerenter', {pointerType: 'mouse'});
usePaper.keyboardFocus = true;
paperCard.fire('focusin', {target: usePaper});
assert.deepEqual(events.at(-1), ['installed preview', 'paper']);
paperCard.fire('focusout', {target: usePaper, relatedTarget: opener});
assert.deepEqual(events.at(-1), ['end installed preview']);
assert.equal(calls.length, 1, 'previewing makes no writes');

forestCard.fire('pointerenter', {pointerType: 'touch'});
assert.deepEqual(events.at(-1), ['end installed preview']);
previewButton.fire('click');
assert.deepEqual(events.at(-1), ['palette preview', forest.tokens]);
assert.equal(previewButton.attributes['aria-pressed'], 'true');
previewButton.fire('click');
assert.deepEqual(events.at(-1), ['end palette preview']);

await install.fire('click');
const status = find(dialog, node => node.attributes.role === 'status');
assert.match(status.textContent, /Theme ID already exists/);
assert.equal(install.textContent, 'Install');
installFails = false;
await install.fire('click');
assert.deepEqual(calls.at(-1), ['/themes/gallery/forest/install', 'POST']);
assert.deepEqual(events.at(-1), ['refresh']);
assert.equal(action(forestCard, 'Use'), install, 'installation preserves the action button and its focus');
assert.equal(events.some(([event]) => event === 'use'), false, 'install does not activate the theme');
forestCard.fire('pointerenter', {pointerType: 'mouse'});
assert.deepEqual(events.at(-1), ['installed preview', 'forest']);
install.fire('click');
assert.deepEqual(events.slice(-2), [['end installed preview'], ['use', 'forest']]);
assert.equal(document.activeElement, opener);
assert.equal(document.body.children.length, 0);

window.KovaThemeGallery.open(opener);
await tick();
const secondDialog = document.body.children[0];
find(secondDialog, node => node.tag === 'li').fire('pointerenter', {pointerType: 'mouse'});
secondDialog.fire('cancel');
assert.deepEqual(events.at(-1), ['end installed preview'], 'Escape restores the saved theme');
assert.equal(document.activeElement, opener);

rows = [];
window.KovaThemeGallery.open(opener);
await tick();
assert.match(find(document.body, node => node.attributes.role === 'status').textContent, /No themes/);

// ---- search, try, update and remove (#177) -----------------------------------------
assert.equal(window.KovaThemeGallery.matches({id: 'forest', name: 'Forest', appearance: 'dark', description: 'Green accent'}, 'green dark'), true);
assert.equal(window.KovaThemeGallery.matches({id: 'forest', name: 'Forest', appearance: 'dark'}, 'forest light'), false);
assert.equal(window.KovaThemeGallery.matches({id: 'forest', name: 'Forest'}, '   '), true);
assert.equal(window.KovaThemeGallery.matches({id: 'a', name: 'A', author: 'Ada Lovelace'}, 'lovelace'), true);

const sky = {id: 'sky', name: 'Sky', appearance: 'light', installed: false, version: '1.1.0', author: 'Ada',
  description: 'Pale blue', swatches: ['#fff'], tokens: {'bg.base': '#fff'}};
const moss = {id: 'moss', name: 'Moss', appearance: 'dark', installed: true, version: '2.0.0',
  installed_version: '1.0.0', update: true, description: 'Mossy', swatches: ['#111'], tokens: {'bg.base': '#111'}};
rows = [sky, moss];
const requests = [];
let failNext = '';
const gallery2 = {
  window, document, ...fakeTimers,
  fetch: async (url, options) => {
    requests.push([url, options?.method || 'GET']);
    if (url === '/themes/gallery') return {ok: true, json: async () => rows};
    if (failNext) { const detail = failNext; failNext = ''; return {ok: false, statusText: 'x', json: async () => ({detail})}; }
    return {ok: true, json: async () => ({})};
  },
};
vm.runInNewContext(source, gallery2);   // a second copy bound to a recording fetch
window.KovaThemeGallery.open(opener);
await tick();
const dlg = document.body.children.at(-1);
const [skyCard, mossCard] = find(dlg, node => node.className === 'theme-gallery-grid').children;
const search = find(dlg, node => node.tag === 'input');
const note = find(dlg, node => node.attributes.role === 'status');
assert.equal(search.type, 'search');
assert.equal(search.attributes['aria-label'], 'Search themes');

// Version, author and the installed-vs-bundled difference are literal text.
const metaOf = card => find(card, node => node.className === 'theme-gallery-meta').textContent;
assert.equal(metaOf(skyCard), 'By Ada · Version 1.1.0');
assert.equal(metaOf(mossCard), 'Version 2.0.0 · Installed 1.0.0');
// Only what applies is offered.
assert.equal(action(skyCard, 'Update').hidden, true);
assert.equal(action(skyCard, 'Remove').hidden, true);
assert.equal(action(mossCard, 'Update').hidden, false);
assert.equal(action(mossCard, 'Update').attributes['aria-label'], 'Update Moss to version 2.0.0');
assert.equal(action(mossCard, 'Remove').hidden, false);

// Search hides cards without touching the server and says how many are left.
search.value = 'pale';
search.fire('input');
assert.equal(skyCard.hidden, false); assert.equal(mossCard.hidden, true);
assert.match(note.textContent, /1 of 2 themes/);
search.value = 'nothing like this';
search.fire('input');
assert.match(note.textContent, /No theme matches "nothing like this"/);
search.value = '';
search.fire('input');
assert.equal(skyCard.hidden || mossCard.hidden, false);
assert.equal(note.textContent, '');
assert.equal(requests.length, 1, 'searching is local');

// Update: replaces the file, refreshes, re-applies the chosen theme and drops the button.
chosen = 'moss';
await action(mossCard, 'Update').fire('click');
assert.deepEqual(requests.at(-1), ['/themes/gallery/moss/update', 'POST']);
assert.deepEqual(events.slice(-2), [['refresh'], ['use', 'moss']], 'a chosen theme is reloaded from the new file');
assert.equal(action(mossCard, 'Update').hidden, true);
assert.equal(metaOf(mossCard), 'Version 2.0.0');
assert.match(note.textContent, /Updated Moss to version 2.0.0/);
chosen = 'dark';
moss.update = true; moss.installed_version = '1.0.0'; mossCard.updateAction();
failNext = 'That theme is already up to date.';
await action(mossCard, 'Update').fire('click');
assert.match(note.textContent, /Could not update Moss\. That theme is already up to date/);
assert.equal(action(mossCard, 'Update').hidden, false, 'a failed update stays available');

// Remove asks twice, names what it removes, and resets a chosen theme.
const removeMoss = action(mossCard, 'Remove');
removeMoss.fire('click');
assert.equal(removeMoss.textContent, 'Confirm remove');
assert.match(removeMoss.attributes['aria-label'], /Confirm: remove Moss/);
assert.equal(requests.some(([url, method]) => method === 'DELETE'), false, 'one click deletes nothing');
removeMoss.fire('focusout', {relatedTarget: opener});
assert.equal(removeMoss.textContent, 'Remove', 'leaving the button cancels the confirmation');
removeMoss.fire('click');
live().at(-1).fn();                       // the confirmation times out by itself
assert.equal(removeMoss.textContent, 'Remove');
chosen = 'moss';
removeMoss.fire('click');
await removeMoss.fire('click');
assert.deepEqual(requests.at(-1), ['/themes/moss', 'DELETE']);
assert.equal(chosen, 'system', 'removing the chosen theme falls back to the device');
assert.equal(moss.installed, false);
assert.equal(action(mossCard, 'Install') !== undefined, true, 'it can be installed again');
assert.equal(action(mossCard, 'Remove').hidden, true);
assert.match(note.textContent, /Removed Moss/);
failNext = 'Could not remove the theme file.';
moss.installed = true; mossCard.updateAction(); chosen = 'dark';
removeMoss.fire('click');
await removeMoss.fire('click');
assert.match(note.textContent, /Could not remove Moss\. Could not remove the theme file/);

// Try: temporary, reverts by itself after TRY_SECONDS, and is never saved.
assert.equal(window.KovaThemeGallery.TRY_SECONDS, 30);
const before = events.length;
const tryMoss = action(mossCard, 'Try');
assert.equal(tryMoss.attributes['aria-label'], 'Try Moss for 30 seconds');
tryMoss.fire('click');
assert.equal(dlg.open, false, 'the gallery closes so the real app is visible');
assert.deepEqual(events.slice(before).filter(([name]) => name !== 'end installed preview' && name !== 'end palette preview'),
  [['installed preview', 'moss']]);
let banner = document.body.children.at(-1);
assert.equal(banner.className, 'theme-try');
assert.equal(banner.attributes.role, 'group');
assert.equal(find(banner, node => node.attributes.role === 'status').textContent, 'Trying Moss. It reverts unless you keep it.');
assert.equal(document.activeElement, action(banner, 'Keep'), 'focus moves to Keep');
const timeout = live().find(timer => !timer.every);
assert.equal(timeout.ms, 30000);
const ticker = live().find(timer => timer.every);
ticker.fn(); ticker.fn();
assert.equal(find(banner, node => node.className === 'theme-try-left').textContent, '28 s');
const eventsBefore = events.length;
timeout.fn();                                     // 30 s pass
assert.deepEqual(events.slice(eventsBefore), [['end installed preview']]);
assert.equal(document.body.children.includes(banner), false);
assert.equal(live().length, 0, 'no timer outlives the trial');
assert.equal(events.slice(before).some(([name]) => name === 'use'), false, 'trying never chooses the theme');
assert.equal(document.activeElement, opener);

// Trying a theme that is not installed paints its palette, and Keep installs then chooses it.
window.KovaThemeGallery.open(opener);
await tick();
const dlg3 = document.body.children.at(-1);
const skyCard3 = find(dlg3, node => node.className === 'theme-gallery-grid').children[0];
action(skyCard3, 'Try').fire('click');
assert.deepEqual(events.at(-1), ['palette preview', sky.tokens]);
banner = document.body.children.at(-1);
await action(banner, 'Keep').fire('click');
assert.deepEqual(requests.slice(-1)[0], ['/themes/gallery/sky/install', 'POST']);
assert.deepEqual(events.slice(-2), [['refresh'], ['use', 'sky']]);
assert.equal(sky.installed, true);
assert.equal(document.body.children.includes(banner), false);
assert.equal(live().length, 0);
assert.equal(events.some(([name], index) => name === 'end palette preview' && index > events.length - 3), false,
  'keeping does not revert first');

// A failed Keep says why, keeps the trial running and can be retried; Revert and Escape end it.
sky.installed = false;
window.KovaThemeGallery.open(opener);
await tick();
const dlg4 = document.body.children.at(-1);
action(find(dlg4, node => node.className === 'theme-gallery-grid').children[0], 'Try').fire('click');
banner = document.body.children.at(-1);
failNext = 'Theme ID already exists.';
await action(banner, 'Keep').fire('click');
assert.match(find(banner, node => node.attributes.role === 'status').textContent, /Could not keep Sky\. Theme ID already exists/);
assert.equal(document.body.children.includes(banner), true);
assert.equal(live().filter(timer => !timer.every).length, 1, 'the revert timer is running again');
action(banner, 'Revert').fire('click');
assert.equal(document.body.children.includes(banner), false);
assert.deepEqual(events.at(-1), ['end palette preview']);
window.KovaThemeGallery.open(opener);
await tick();
action(find(document.body.children.at(-1), node => node.className === 'theme-gallery-grid').children[0], 'Try').fire('click');
banner = document.body.children.at(-1);
banner.fire('keydown', {key: 'Escape'});
assert.equal(document.body.children.includes(banner), false);
assert.equal(live().length, 0);
