// Exercise the editor's real controls with an in-memory DOM and a fake theme API.
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import vm from 'node:vm';

const source = readFileSync(new URL('../src/kova/web_assets/theme-editor.js', import.meta.url), 'utf8');
const palettes = JSON.parse(readFileSync(new URL('../src/kova/tokens.json', import.meta.url))).themes;
const tick = () => new Promise(resolve => setImmediate(resolve));

class Node {
  constructor(tag) {
    this.tagName = tag;
    this.children = [];
    this.attributes = {};
    this.events = {};
    this.dataset = {};
    this.value = '';
  }
  append(...children) {
    for (const child of children) child.parentNode = this;
    this.children.push(...children);
  }
  replaceChildren(...children) { this.children = []; this.append(...children); }
  remove() { this.parentNode.children = this.parentNode.children.filter(child => child !== this); }
  setAttribute(name, value) { this.attributes[name] = value; }
  addEventListener(name, listener) { this.events[name] = listener; }
  click() { if (!this.disabled) return this.fire('click'); }
  fire(name, event = {}) {
    return this.events[name]?.({target: this, preventDefault() {}, stopPropagation() {}, ...event});
  }
  focus() {}
  find(fn) { return fn(this) ? this : this.children.map(child => child.find(fn)).find(Boolean); }
}

const body = new Node('body');
const calls = [];
const previews = [];
const downloaded = [];
const selected = [];
let closes = 0;
let pauseRecheck;
const theme = {
  choice: () => 'system', appearance: () => 'dark',
  detailUrl: id => `/themes/${encodeURIComponent(id)}`,
  choices: () => [
    {id: 'system', name: 'System'}, {id: 'light', name: 'Light'}, {id: 'dark', name: 'Dark'},
    {id: 'my-light', name: 'My Light', appearance: 'light'},
  ],
  previewPalette: draft => previews.push({...draft, tokens: {...draft.tokens}}),
  clearPalettePreview: () => previews.push(null),
  set: async id => { selected.push(id); },
  refresh: async () => theme.choices(),
};
const installed = {id: 'my-light', name: 'My Light', appearance: 'light', extends: 'kova-light',
  tokens: {...palettes['kova-light'].tokens, 'bg.base': '#fafafa', 'syntax.keyword': '#113355'},
  terminal: palettes['kova-light'].terminal};
const contrastMessage = 'fg.default on bg.base = 1.00:1, below 4.5:1';
function resolve(payload) {
  const base = palettes[payload.extends || `kova-${payload.appearance}`];
  const tokens = {...base.tokens};
  for (const [group, values] of Object.entries(payload.tokens || {})) {
    for (const [name, value] of Object.entries(values)) tokens[`${group}.${name}`] = value;
  }
  return {id: payload.id, appearance: payload.appearance, tokens};
}
const window = {KovaTheme: theme};
vm.runInNewContext(source, {
  window, document: {body, createElement: tag => new Node(tag)},
  CSS: {supports: () => true},
  URL: {createObjectURL: blob => { downloaded.push(blob); return 'blob:theme'; }, revokeObjectURL() {}},
  Blob, setTimeout: fn => setTimeout(fn, 0), clearTimeout,
  fetch: async (url, options) => {
    const payload = options?.body ? JSON.parse(options.body) : null;
    calls.push({url, payload});
    const id = url.slice('/themes/'.length);
    if (!payload) {
      const palette = id === 'my-light' ? installed : {id, name: id, ...palettes[id]};
      return {ok: true, json: async () => palette};
    }
    const resolved = resolve(payload);
    const bad = resolved.tokens['fg.default'] === resolved.tokens['bg.base'];
    if (pauseRecheck && bad && payload.id !== 'bad-import') await pauseRecheck;
    return {ok: !bad, json: async () => bad ? {detail: [contrastMessage]} : resolved};
  },
});

const byId = id => body.find(node => node.id === id);
const button = label => body.find(node => node.tagName === 'button' && node.textContent === label);
const open = async () => { window.KovaThemeEditor.open(() => { closes += 1; }); await tick(); await tick(); };
const change = (node, value, event = 'input') => { node.value = value; return node.fire(event); };
const settle = async () => { await new Promise(resolve => setTimeout(resolve, 10)); await tick(); };

await open();
const start = byId('theme-editor-base');
assert.equal(start.value, 'dark', 'System uses the effective theme as its starting palette');
assert.equal(previews.at(-1).tokens['bg.base'], '#14121c');
const fg = body.find(node => node.attributes['aria-label'] === 'fg.default hex');
const bgPicker = body.find(node => node.attributes['aria-label'] === 'bg.base colour');
assert.equal(fg.value, '#e8e6f0');
assert.equal(button('Save').disabled, false);

const beforeInvalid = previews.at(-1);
change(fg, '#12');
assert.equal(fg.attributes['aria-invalid'], 'true');
assert.equal(previews.at(-1), beforeInvalid, 'incomplete hex does not paint');
assert.equal(button('Save').disabled, true);
change(fg, '#14121c');
assert.equal(previews.at(-1).tokens['fg.default'], '#14121c', 'valid hex paints immediately');
assert.equal(button('Save').disabled, true, 'save waits for the check');
await settle();
assert.equal(button('Save').disabled, true, 'contrast failure blocks Save');
assert.ok(body.find(node => node.textContent === contrastMessage), 'the failed pair is visible');

let release;
pauseRecheck = new Promise(resolve => { release = resolve; });
change(fg, '#ffffff');
await settle();
change(fg, '#14121c');
await settle();
change(fg, '#ffffff');
await settle();
release();
await tick();
assert.equal(button('Save').disabled, false, 'a stale contrast failure cannot block a newer passing draft');
change(bgPicker, '#15131d');
assert.equal(previews.at(-1).tokens['bg.base'], '#15131d');
button('Cancel').click();
assert.equal(previews.at(-1), null);
assert.equal(closes, 1);
assert.equal(calls.some(call => call.url === '/themes'), false, 'no installation on cancel');

await open();
await change(byId('theme-editor-base'), 'my-light', 'change');
assert.equal(previews.at(-1).appearance, 'light');
assert.equal(previews.at(-1).tokens['syntax.keyword'], '#113355', 'duplicate keeps non-editable tokens');

// ---- Background: none, gradient, noise or image, previewed and checked like colours (#206)
{
  const kind = byId('theme-editor-background-kind');
  assert.equal(kind.value, 'none');
  assert.deepEqual(kind.children.map(option => option.value), ['none', 'gradient', 'noise', 'image']);
  assert.equal(previews.at(-1).background, undefined, 'a theme without a background previews without one');
  const checked = () => calls.at(-1).payload;
  const plain = value => JSON.parse(JSON.stringify(value));

  change(kind, 'gradient', 'change');
  assert.deepEqual(plain(previews.at(-1).background), {kind: 'gradient', stops: ['#fafafa', previews.at(-1).background.stops[1]], angle: 160});
  assert.equal(previews.at(-1).background.stops[1], previews.at(-1).tokens['bg.raised'].slice(0, 7));
  assert.equal(button('Save').disabled, true, 'save waits for the new check');
  await settle();
  assert.equal(checked().background.kind, 'gradient', 'the server checks the background with the colours');
  assert.equal(button('Save').disabled, false);
  change(byId('theme-editor-background-stop-2'), '#123456');
  assert.equal(previews.at(-1).background.stops[1], '#123456');
  change(byId('theme-editor-background-angle'), '90');
  assert.equal(previews.at(-1).background.angle, 90);
  assert.equal(byId('theme-editor-background-angle').parentNode.parentNode.children[1].textContent, '90°');
  button('Add colour').click();
  assert.equal(previews.at(-1).background.stops.length, 3);
  button('Add colour').click();
  assert.equal(previews.at(-1).background.stops.length, 4);
  assert.equal(button('Add colour').disabled, true, 'four colours at most');
  button('Remove colour').click(); button('Remove colour').click();
  assert.equal(button('Remove colour').disabled, true, 'two colours at least');
  assert.equal(previews.at(-1).background.stops.length, 2);

  change(kind, 'noise', 'change');
  assert.deepEqual(plain(previews.at(-1).background), {kind: 'noise', opacity: 0.06});
  assert.equal(byId('theme-editor-background-stop-1'), undefined, 'the gradient controls are gone');
  change(byId('theme-editor-background-opacity'), '0.12');
  assert.equal(previews.at(-1).background.opacity, 0.12);

  change(kind, 'image', 'change');
  assert.deepEqual(plain(previews.at(-1).background), {kind: 'image', file: '', dim: 0.55, blur: 0});
  change(byId('theme-editor-background-file'), ' night-sky.png ');
  assert.equal(previews.at(-1).background.file, 'night-sky.png');
  change(byId('theme-editor-background-dim'), '0.5');
  change(byId('theme-editor-background-blur'), '8');
  assert.deepEqual(plain(previews.at(-1).background), {kind: 'image', file: 'night-sky.png', dim: 0.5, blur: 8});
  await settle();
  assert.deepEqual(plain(checked().background), {kind: 'image', file: 'night-sky.png', dim: 0.5, blur: 8});

  change(kind, 'none', 'change');
  assert.equal(previews.at(-1).background, undefined);
  await settle();
  assert.equal('background' in checked(), false, 'None leaves the key out of the file');
  // A duplicate of a theme that has a background keeps it, so the editor shows and re-saves it.
  installed.background = {kind: 'noise', opacity: 0.1};
  await change(byId('theme-editor-base'), 'dark', 'change');
  await change(byId('theme-editor-base'), 'my-light', 'change');
  assert.deepEqual(plain(previews.at(-1).background), {kind: 'noise', opacity: 0.1});
  assert.equal(byId('theme-editor-background-kind').value, 'noise');
  delete installed.background;
  await change(byId('theme-editor-base'), 'dark', 'change');
  await change(byId('theme-editor-base'), 'my-light', 'change');
}

const file = byId('theme-editor-file');
const importFile = async payload => {
  const text = JSON.stringify(payload);
  file.files = [{size: text.length, text: async () => text}];
  await file.fire('change');
};
const imported = {id: 'portable-theme', name: 'Portable', appearance: 'dark', version: '2.0.0',
  description: 'A theme to share', tokens: {syntax: {keyword: '#eeeeee'}, bg: {overlay: '#12345680'}}};
const beforeImport = previews.at(-1);
await importFile({...imported, id: 'bad-import', tokens: {fg: {default: '#14121c'}}});
assert.equal(previews.at(-1), beforeImport, 'rejected import never paints');
assert.equal(calls.at(-1).url, '/themes?validate_only=true');
await importFile(imported);
assert.equal(byId('theme-editor-id').value, imported.id);
assert.equal(previews.at(-1).tokens['bg.overlay'], '#12345680');
assert.equal(previews.at(-1).tokens['syntax.keyword'], '#eeeeee');

button('Export').click();
assert.deepEqual(JSON.parse(await downloaded.at(-1).text()), imported, 'an unedited import exports identically');
await button('Save').click();
assert.deepEqual(calls.find(call => call.url === '/themes').payload, imported);
assert.deepEqual(selected, ['portable-theme']);
assert.equal(previews.at(-1), null);
assert.equal(closes, 2);

console.log('Theme editor: editable palette, live validation, preview, import, export and Save passed');
