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
    this.style = {};
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
const theme = {
  preview: id => events.push(['installed preview', id]),
  endPreview: () => events.push(['end installed preview']),
  previewPalette: palette => events.push(['palette preview', palette.tokens]),
  clearPalettePreview: () => events.push(['end palette preview']),
  refresh: async () => events.push(['refresh']),
  set: id => events.push(['use', id]),
};
const forest = {id: 'forest', name: 'Forest', appearance: 'dark', installed: false,
  description: 'Green accent', swatches: ['#14121c', '#5fd787'], tokens: {'accent.default': '#5fd787'}};
const paper = {id: 'paper', name: 'Paper <example>', appearance: 'light', installed: true,
  description: 'Warm background', swatches: ['#fbfaf7'], tokens: {'bg.base': '#fbfaf7'}};
let rows = [forest, paper];
let installFails = true;
const window = {KovaTheme: theme};
vm.runInNewContext(source, {
  window, document,
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
assert.ok(find(forestCard, node => node.style.backgroundColor === forest.swatches[1]));
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
