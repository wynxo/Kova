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
    this.classList = {
      add: (...names) => {
        const set = new Set((this.className || '').split(' ').filter(Boolean));
        for (const name of names) set.add(name);
        this.className = [...set].join(' ');
      },
      remove: (...names) => {
        const set = new Set((this.className || '').split(' ').filter(Boolean));
        for (const name of names) set.delete(name);
        this.className = [...set].join(' ');
      },
      contains: name => (this.className || '').split(' ').includes(name),
    };
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
  let clipboardText = '';
  const navigator = {clipboard: {writeText: async text => { clipboardText = text;}}};
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
const appearance = {radiusValue: 'default', densityValue: 'default', fontValue: 'inter',
  radius() { return this.radiusValue; },
  density() { return this.densityValue; },
  font() { return this.fontValue; },
  setRadius(value) { this.radiusValue = value; },
  setDensity(value) { this.densityValue = value; },
  setFont(value) { this.fontValue = value; }};
function resolve(payload) {
  const base = palettes[payload.extends || `kova-${payload.appearance}`];
  const tokens = {...base.tokens};
  for (const [group, values] of Object.entries(payload.tokens || {})) {
    for (const [name, value] of Object.entries(values)) tokens[`${group}.${name}`] = value;
  }
  return {id: payload.id, appearance: payload.appearance, tokens};
}
const window = {KovaTheme: theme, KovaTypography: appearance};
vm.runInNewContext(source, {
  window, document: {body, createElement: tag => new Node(tag)},
  CSS: {supports: () => true},
  navigator,
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
    const accentBad = payload.tokens?.accent?.default === '#000000';
    const bad = resolved.tokens['fg.default'] === resolved.tokens['bg.base'];
    if (pauseRecheck && bad && payload.id !== 'bad-import') await pauseRecheck;
    const detail = bad ? [contrastMessage]
      : accentBad ? ['accent.default on bg.surface = 1.00:1, below 3.0:1'] : null;
    return {ok: !detail, json: async () => detail ? {detail} : resolved};
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
assert.equal(previews.at(-1).tokens['bg.base'], '#1a1a1a');
const fg = body.find(node => node.attributes['aria-label'] === 'fg.default hex');
const bgPicker = body.find(node => node.attributes['aria-label'] === 'bg.base colour');
assert.equal(fg.value, '#ececec');
assert.equal(button('Save').disabled, false);
const reset = button('Reset to base');
assert.equal(reset.disabled, false, 'Reset to base is available once a base loads');
change(fg, '#123456');
assert.equal(previews.at(-1).tokens['fg.default'], '#123456', 'the edit paints immediately');
reset.click();
await settle();
assert.equal(previews.at(-1).tokens['fg.default'], '#ececec', 'Reset to base reverts the edit');
assert.equal(body.find(node => node.attributes['aria-label'] === 'fg.default hex').value, '#ececec', 'the field shows the base colour again');

const beforeInvalid = previews.at(-1);
change(fg, '#12');
assert.equal(fg.attributes['aria-invalid'], 'true');
assert.equal(previews.at(-1), beforeInvalid, 'incomplete hex does not paint');
assert.equal(button('Save').disabled, true);
change(fg, '#1a1a1a');
assert.equal(previews.at(-1).tokens['fg.default'], '#1a1a1a', 'valid hex paints immediately');
assert.equal(button('Save').disabled, true, 'save waits for the check');
await settle();
assert.equal(button('Save').disabled, true, 'contrast failure blocks Save');
assert.ok(body.find(node => node.textContent === contrastMessage), 'the failed pair is visible');
const failingRow = body.find(node => node.dataset.token === 'fg.default');
assert.ok(failingRow.className.includes('is-invalid'), 'the failing row is marked beside its values');

let release;
pauseRecheck = new Promise(resolve => { release = resolve; });
change(fg, '#ffffff');
await settle();
change(fg, '#1a1a1a');
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
await importFile({...imported, id: 'bad-import', tokens: {fg: {default: '#1a1a1a'}}});
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
  // ---- Share string export and import (#176)
  await open();
  await change(byId('theme-editor-base'), 'my-light', 'change');
  await settle();
  const copyButton = button('Copy share string');
  const importStringButton = button('Import string');
  const share = byId('theme-editor-share');
  const fgStr = body.find(node => node.attributes['aria-label'] === 'fg.default hex');
  const good = fgStr.value; // the valid default for the loaded theme

  // A valid draft copies a share string that carries the Kova prefix.
  assert.equal(copyButton.disabled, false);
  copyButton.click();
  const copied = clipboardText;
  assert.ok(copied.startsWith('kova-theme:'), 'the share string carries the Kova prefix');
  const tokensBefore = JSON.parse(JSON.stringify(previews.at(-1).tokens));
  const idBefore = byId('theme-editor-id').value;

   // Pasting that share string imports it identically to a file.
   share.value = copied;
   await importStringButton.click();
   await settle();
  assert.deepEqual(previews.at(-1).tokens, tokensBefore, 'a pasted share string imports identically');
  assert.equal(byId('theme-editor-id').value, idBefore, 'the theme id round-trips through the share string');
  assert.equal(calls.at(-1).url, '/themes?validate_only=true', 'import string reuses file validation');
  const countBefore = previews.length;

  // A string without the prefix is rejected without repainting.
  share.value = 'not a share string';
  await importStringButton.click();
  await settle();
  assert.equal(previews.length, countBefore, 'a malformed share string never paints');

  // The copy button waits for a valid draft.
  await change(fgStr, '#12', 'input');
  await settle();
  assert.equal(copyButton.disabled, true, 'Copy share string waits for a valid draft');
  await change(fgStr, good, 'input');
  await settle();
  assert.equal(copyButton.disabled, false);

  await button('Cancel').click();
  await settle();
  assert.equal(previews.at(-1), null);
  assert.equal(closes, 3);

  // ---- Simple and Advanced modes (#176)
  await open();
  assert.equal(byId('theme-editor-tab-simple').attributes['aria-selected'], 'true',
    'Simple is the first editor mode');
  assert.equal(byId('theme-editor-tab-advanced').attributes['aria-selected'], 'false');
  assert.equal(byId('theme-editor-simple').hidden, false);
  assert.equal(byId('theme-editor-advanced').hidden, true,
    'Advanced starts folded away behind the tab');
  const baseTokens = JSON.parse(JSON.stringify(previews.at(-1).tokens));

  // Accent derives every accent role, and an invalid hex never repaints.
  const accentHex = byId('theme-editor-simple-accent-hex');
  change(accentHex, '#2dd4bf');
  assert.equal(previews.at(-1).tokens['accent.default'], '#2dd4bf');
  assert.notEqual(previews.at(-1).tokens['accent.hover'], '#2dd4bf',
    'hover is derived from the chosen accent');
  assert.notEqual(previews.at(-1).tokens['accent.pressed'], '#2dd4bf');
  assert.ok([baseTokens['fg.default'], baseTokens['fg.inverse']]
    .includes(previews.at(-1).tokens['accent.on']), 'on-text is one of the theme text extremes');
  assert.equal(body.find(node => node.attributes['aria-label'] === 'accent.default hex').value,
    '#2dd4bf', 'the Advanced row follows the Simple accent');
  await settle();
  assert.equal(calls.at(-1).payload.tokens.accent.default, '#2dd4bf',
    'the derived accent is sent to the contrast check');
  button('Export').click();
  assert.equal(JSON.parse(await downloaded.at(-1).text()).tokens.accent.default, '#2dd4bf',
    'a Simple edit exports as a normal theme token');
  const beforeBadAccent = previews.at(-1);
  change(accentHex, '#12');
  assert.equal(accentHex.attributes['aria-invalid'], 'true');
  assert.equal(previews.at(-1), beforeBadAccent, 'an incomplete accent does not paint');
  change(accentHex, '#2dd4bf');

  // A failed accent pair also marks the Simple row, not only the footer list.
  change(accentHex, '#000000');
  await settle();
  const simpleAccentRow = body.find(node => (node.className || '').includes('theme-editor-simple-accent'));
  assert.ok(simpleAccentRow.className.includes('is-invalid'),
    'the Simple accent is marked when its pair fails');
  change(accentHex, '#2dd4bf');
  await settle();

  // Tone retints only the background roles; Neutral restores the source values.
  button('Warm').click();
  assert.notEqual(previews.at(-1).tokens['bg.base'], baseTokens['bg.base']);
  assert.notEqual(previews.at(-1).tokens['bg.surface'], baseTokens['bg.surface']);
  const warmBase = previews.at(-1).tokens['bg.base'];
  assert.equal(body.find(node => node.attributes['aria-label'] === 'bg.base hex').value, warmBase,
    'the Advanced rows follow a tone change too');
  button('Cool').click();
  assert.notEqual(previews.at(-1).tokens['bg.base'], warmBase);
  button('Neutral').click();
  assert.equal(previews.at(-1).tokens['bg.base'], baseTokens['bg.base'],
    'Neutral restores the theme rather than tinting it');

  // Contrast moves the text roles away from and back to the theme's own values.
  const contrast = byId('theme-editor-contrast');
  change(contrast, '0');
  assert.notEqual(previews.at(-1).tokens['fg.default'], baseTokens['fg.default']);
  assert.equal(byId('theme-editor-contrast').parentNode.parentNode.children[1].textContent, 'Softer 50%');
  const softer = previews.at(-1).tokens['fg.default'];
  change(contrast, '100');
  assert.notEqual(previews.at(-1).tokens['fg.default'], softer);
  change(contrast, '50');
  assert.equal(previews.at(-1).tokens['fg.default'], baseTokens['fg.default'],
    'Default contrast keeps the source palette exactly');

  // Corners, density and font are appearance settings, not theme tokens.
  const tokensBeforePrefs = JSON.stringify(previews.at(-1).tokens);
  change(byId('theme-editor-radius'), '2');
  assert.equal(appearance.radiusValue, 'round');
  assert.equal(byId('theme-editor-radius').parentNode.parentNode.children[1].textContent, 'Round');
  byId('theme-editor-density-compact').click();
  assert.equal(appearance.densityValue, 'compact');
  byId('theme-editor-font-mono').click();
  assert.equal(appearance.fontValue, 'mono');
  assert.equal(JSON.stringify(previews.at(-1).tokens), tokensBeforePrefs,
    'appearance settings never enter the theme token map');
  assert.equal(byId('theme-editor-density-compact').attributes['aria-checked'], 'true');
  assert.equal(byId('theme-editor-font-mono').attributes['aria-checked'], 'true');

  // Advanced exposes every semantic group, including code.
  byId('theme-editor-tab-advanced').click();
  assert.equal(byId('theme-editor-simple').hidden, true);
  assert.equal(byId('theme-editor-advanced').hidden, false);
  assert.ok(body.find(node => node.tagName === 'h3' && node.textContent === 'Code'),
    'Advanced groups the syntax roles under Code');
  assert.ok(body.find(node => node.attributes['aria-label'] === 'syntax.keyword hex'),
    'Advanced can edit every semantic syntax role');
  const modes = byId('theme-editor-tab-simple').parentNode;
  modes.fire('keydown', {key: 'ArrowLeft', preventDefault() {}});
  assert.equal(byId('theme-editor-tab-simple').attributes['aria-selected'], 'true',
    'the mode tabs are operable with the arrow keys');

  // Reset to base clears every Simple edit too.
  button('Reset to base').click();
  assert.equal(previews.at(-1).tokens['accent.default'], baseTokens['accent.default']);
  assert.equal(byId('theme-editor-simple-accent-hex').value, baseTokens['accent.default']);
  assert.equal(byId('theme-editor-contrast').value, '50');

  await button('Cancel').click();
  await settle();
  assert.equal(previews.at(-1), null);
  assert.equal(closes, 4);

  // ---- Drag-and-drop import (#176): a theme file dropped on the panel
  {
    await open();
    const panel = body.find(node => node.tagName === 'aside');
    assert.ok(body.find(node => (node.className || '').includes('theme-editor-hint')
      && (node.textContent || '').includes('drop a theme JSON file')),
      'the sources row names the drop zone');
    const dragFile = payload => {
      const text = JSON.stringify(payload);
      return {size: text.length, text: async () => text};
    };
    const filesDrag = file => ({types: ['Files'], files: [file], dropEffect: ''});

    // A file drag claims the panel and outlines it; leaving puts it back.
    let claimed = false;
    const claim = () => { claimed = true; };
    panel.fire('dragenter', {dataTransfer: filesDrag(dragFile(imported)), preventDefault: claim});
    assert.equal(claimed, true, 'dragenter claims a file drag');
    assert.ok(panel.classList.contains('is-drop-target'), 'the panel outlines itself as the drop target');
    claimed = false;
    panel.fire('dragover', {dataTransfer: filesDrag(dragFile(imported)), preventDefault: claim});
    assert.equal(claimed, true, 'dragover keeps the drop allowed');
    panel.fire('dragleave', {});
    assert.equal(panel.classList.contains('is-drop-target'), false, 'dragging away puts the outline back');

    // A text drag is never claimed, so dropping text into a field stays native.
    claimed = false;
    panel.fire('dragover', {dataTransfer: {types: ['text/plain']}, preventDefault: claim});
    assert.equal(claimed, false, 'a text drag is not claimed');
    assert.equal(panel.classList.contains('is-drop-target'), false);

    // A dropped theme file imports through the same path as the picker.
    claimed = false;
    panel.fire('drop', {dataTransfer: filesDrag(dragFile(imported)), preventDefault: claim});
    await settle();
    assert.equal(claimed, true, 'the drop is claimed');
    assert.equal(panel.classList.contains('is-drop-target'), false, 'the outline comes off with the drop');
    assert.equal(byId('theme-editor-id').value, imported.id, 'a dropped theme file loads like a picked one');
    assert.equal(previews.at(-1).tokens['syntax.keyword'], '#eeeeee', 'the dropped tokens preview');
    assert.equal(calls.at(-1).url, '/themes?validate_only=true', 'a drop reuses the import check');

    // A file that is not a theme says so without painting anything.
    const before = previews.at(-1);
    panel.fire('drop', {dataTransfer: filesDrag({size: 9, text: async () => 'not json'})});
    await settle();
    assert.equal(previews.at(-1), before, 'a broken drop never paints');
    assert.ok((body.find(node => (node.className || '').includes('theme-editor-notice')).textContent || '')
      .includes('Drop a valid theme JSON file.'), 'the notice names the drop');

    // The picker's size cap governs drops too.
    panel.fire('drop', {dataTransfer: filesDrag({size: 40 * 1024, text: async () => '{}'})});
    await settle();
    assert.ok((body.find(node => (node.className || '').includes('theme-editor-notice')).textContent || '')
      .includes('at most 32 KiB'), 'an oversized drop is refused before parsing');

    await button('Cancel').click();
    await settle();
    assert.equal(closes, 5);
  }

  console.log('Theme editor: editable palette, live validation, preview, import, export, share string, Simple/Advanced modes, drag-and-drop and Save passed');

