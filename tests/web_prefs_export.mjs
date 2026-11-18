// Exercise the Settings > General export/import preferences controls (#212)
// with an offline DOM and API boundary: download shape, pre-apply summary,
// refusal of bad files, and that Apply saves through PUT /preferences.
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import vm from 'node:vm';

class Element {
  constructor(tag = 'div') {
    this.tagName = tag.toUpperCase();
    this.children = []; this.parentNode = null; this.className = ''; this.id = '';
    this.value = ''; this.hidden = false; this.disabled = false; this.open = false;
    this.attrs = {}; this.dataset = {}; this.style = {}; this.listeners = {};
    this.tabIndex = -1; this._text = ''; this.clicked = 0; this.files = null;
    this.href = ''; this.download = ''; this.type = ''; this.accept = '';
    // String-based, so a class set as an attribute survives add/remove.
    this.classList = {
      add: (...items) => {
        const cur = this.className.split(' ').filter(Boolean);
        for (const item of items) if (!cur.includes(item)) cur.push(item);
        this.className = cur.join(' ');
      },
      remove: (...items) => {
        this.className = this.className.split(' ').filter(part => part && !items.includes(part)).join(' ');
      },
      contains: name => this.className.split(' ').includes(name),
    };
  }
  set textContent(text) { this._text = String(text); this.children = []; }
  get textContent() { return this._text + this.children.map(node => node.textContent).join(''); }
  append(...nodes) {
    for (const node of nodes) {
      const child = typeof node === 'string' ? document.createTextNode(node) : node;
      child.parentNode = this; this.children.push(child);
    }
  }
  replaceChildren(...nodes) { this._text = ''; this.children = []; this.append(...nodes); }
  remove() { if (this.parentNode) this.parentNode.children.splice(this.parentNode.children.indexOf(this), 1); }
  setAttribute(key, value) {
    this.attrs[key] = String(value);
    if (key === 'id') this.id = String(value);
    if (key === 'hidden') this.hidden = true;
    if (key === 'tabindex') this.tabIndex = Number(value);
  }
  getAttribute(key) { return this.attrs[key]; }
  addEventListener(type, fn) { (this.listeners[type] ||= []).push(fn); }
  dispatch(type, event = {}) { for (const fn of this.listeners[type] || []) fn({preventDefault() {}, ...event}); }
  click() { this.clicked += 1; this.dispatch('click', {}); }
  focus() { document.activeElement = null; }
  querySelectorAll(selector) {
    const parts = selector.split(',').map(part => part.trim());
    return walk(this).filter(node => node !== this && parts.some(part => {
      if (part === '[tabindex="0"]') return node.tabIndex === 0;
      return node.tagName === part.toUpperCase();
    }));
  }
}

function walk(node) { return [node, ...node.children.flatMap(walk)]; }
const byClass = (node, cls) => walk(node).filter(n => n.className.split(' ').includes(cls));

const nodes = new Map();
const links = [];
const body = new Element('body');
const document = {
  body, activeElement: null, documentElement: {dataset: {theme: 'dark'}},
  createElement: tag => {
    const node = new Element(tag);
    if (String(tag).toLowerCase() === 'a') links.push(node);
    return node;
  },
  createTextNode: text => { const node = new Element('#text'); node.textContent = text; return node; },
  getElementById(id) { return nodes.get(id) || walk(body).find(n => n.id === id) || null; },
};
function launch(id, tag = 'button') {
  const node = new Element(tag); node.id = id; nodes.set(id, node); body.append(node); return node;
}

for (const id of ['open-settings', 'theme', 'open-shortcuts', 'install-settings', 'open-sounds', 'connect-phone',
  'add-provider', 'open-model-settings', 'open-privacy', 'open-memory', 'open-reminders', 'open-extensions',
  'open-mcp', 'disconnect']) launch(id);
const autostart = launch('settings', 'details');
autostart.append(new Element('summary'), new Element('input'), new Element('p'), new Element('button'));

// One browser session: the shared preferences, the installed themes, and the calls.
let current = {theme: 'system', sidebar: 'open', notifications: 'off'};
const puts = [];
const blobs = [];
async function fetch(url, options = {}) {
  if (url === '/preferences' && !options.method) {
    return {ok: true, json: async () => ({...current})};
  }
  if (url === '/preferences' && options.method === 'PUT') {
    const patch = JSON.parse(options.body);
    puts.push(patch);
    current = {...current, ...patch};
    return {ok: true, json: async () => ({...current})};
  }
  if (url === '/themes') {
    return {ok: true, json: async () => ([
      {id: 'kova-dark', source: 'built-in', appearance: 'dark'},
      {id: 'midnight-teal', source: 'file', appearance: 'dark'},
    ])};
  }
  return {ok: false, json: async () => ({})};
}
class Blob {
  constructor(parts) { this.text = parts.join(''); }
}
const BlobUrls = {
  createObjectURL: blob => { blobs.push(blob); return `blob:${blobs.length}`; },
  revokeObjectURL: () => {},
};

const reasoning = {value: 'collapsed', mode() { return this.value; }, setMode(next) { this.value = next; }};
const themeEngine = {value: 'system', sets: [],
  choice() { return this.value; },
  choices() { return [{id: 'system', name: 'System'}, {id: 'light', name: 'Light'}, {id: 'dark', name: 'Dark'}]; },
  set(value) { this.sets.push(value); this.value = value; },
  sync() {}, refresh() {},
};
const typography = {textSizeValue: 'default', densityValue: 'default', fontValue: 'inter',
  textSize() { return this.textSizeValue; }, density() { return this.densityValue; },
  font() { return this.fontValue; }, setTextSize(v) { this.textSizeValue = v; },
  setDensity(v) { this.densityValue = v; }, setFont(v) { this.fontValue = v; }};
const motion = {value: 'full', mode() { return this.value; }, setMode(next) { this.value = next; }};
const window = {KovaReasoning: reasoning, KovaTheme: themeEngine, KovaTypography: typography, KovaMotion: motion};
const sandbox = {document, window, fetch, Blob, URL: BlobUrls, setTimeout, console, URLSearchParams};
vm.runInNewContext(readFileSync(new URL('../src/kova/web_assets/settings.js', import.meta.url), 'utf8'), sandbox);
const $ = id => document.getElementById(id);
const tick = async (rounds = 6) => { for (let i = 0; i < rounds; i++) await new Promise(r => setImmediate(r)); };

const KovaPrefs = window.KovaPrefs;
assert.ok(KovaPrefs, 'settings.js exposes window.KovaPrefs for the export/import rows');

// The document helpers round-trip, and refuse what the server would refuse.
const doc = KovaPrefs.buildPrefsDocument({theme: 'light'}, ['midnight-teal']);
assert.equal(doc.format, 'kova-ui-preferences');
assert.equal(doc.version, 1);
assert.equal(JSON.stringify(doc.preferences), JSON.stringify({theme: 'light'}));
assert.equal(JSON.stringify(doc.themes), JSON.stringify(['midnight-teal']));
const parsed = KovaPrefs.parsePrefsDocument(doc);
assert.equal(JSON.stringify(parsed.preferences), JSON.stringify({theme: 'light'}));
assert.equal(JSON.stringify(parsed.themes), JSON.stringify(['midnight-teal']));
assert.throws(() => KovaPrefs.parsePrefsDocument({format: 'other', version: 1, preferences: {}}),
  /Not a Kova UI preferences file/);
assert.throws(() => KovaPrefs.parsePrefsDocument({format: 'kova-ui-preferences', version: 99, preferences: {}}),
  /newer Kova/);
assert.throws(() => KovaPrefs.parsePrefsDocument({format: 'kova-ui-preferences', version: 1}),
  /no preferences/);
const summary = KovaPrefs.summarizePrefs({theme: 'system'}, {theme: 'light'}, ['midnight-teal'], ['midnight-teal', 'paper']);
assert.equal(JSON.stringify(summary.changes), JSON.stringify(['theme']));
assert.equal(JSON.stringify(summary.missing), JSON.stringify(['paper']));

// Both rows live in General, beside Startup and Sign out.
document.activeElement = $('open-settings');
$('open-settings').click();
const view = $('settings-page');
const general = byClass(view, 'settings-section')[0];
const rows = byClass(general, 'settings-row');
const exportRow = rows.find(row => row.textContent.includes('Export preferences'));
const importRow = rows.find(row => row.textContent.includes('Import preferences'));
assert.ok(exportRow, 'General has an Export preferences row');
assert.ok(importRow, 'General has an Import preferences row');
const exportButton = byClass(exportRow, 'settings-action').find(node => node.textContent === 'Export');
const importButton = byClass(importRow, 'settings-action').find(node => node.textContent === 'Import');
assert.ok(exportButton, 'export downloads a JSON file');
assert.ok(importButton, 'import opens a file picker');

// Export saves the shared document: format, version, preferences, theme names only.
exportButton.click();
await tick();
assert.ok(links.length, 'export offers a download');
assert.match(links.at(-1).download, /^Kova-ui-preferences-\d{4}-\d{2}-\d{2}\.json$/);
const exported = JSON.parse(blobs.at(-1).text);
assert.equal(exported.format, 'kova-ui-preferences');
assert.equal(exported.version, 1);
assert.equal(JSON.stringify(exported.preferences), JSON.stringify(current));
assert.equal(JSON.stringify(exported.themes), JSON.stringify(['midnight-teal']));
const exportStatus = byClass(exportRow, 'settings-row-help').at(-1).textContent;
assert.match(exportStatus, /Exported 3 preferences/);

// Import shows a short summary of what changes, then applies on Apply.
const importInput = walk(importRow).find(node => node.tagName === 'INPUT' && node.type === 'file');
assert.ok(importInput, 'import has a file picker');
const fileText = JSON.stringify({format: 'kova-ui-preferences', version: 1,
  preferences: {theme: 'dark', notifications: 'on'}, themes: ['midnight-teal', 'paper']});
importInput.files = [{text: async () => fileText}];
importInput.dispatch('change', {});
await tick();
const importStatus = () => byClass(importRow, 'settings-row-help').at(-1).textContent;
assert.match(importStatus(), /2 changes/, 'the summary counts what would change');
assert.match(importStatus(), /theme/, 'the summary names the preferences');
assert.match(importStatus(), /paper/, 'a missing theme is named before anything applies');
const beforePuts = puts.length;
const applyButton = byClass(importRow, 'settings-action').find(node => node.textContent === 'Apply');
assert.equal(applyButton.hidden, false, 'Apply appears once the summary is shown');
applyButton.click();
await tick();
assert.equal(puts.length, beforePuts + 1, 'Apply saves through PUT /preferences');
assert.equal(JSON.stringify(puts.at(-1)), JSON.stringify({theme: 'dark', notifications: 'on'}));
assert.match(importStatus(), /Applied 2 preferences/);

// A file from another app is refused with a clear message, changing nothing.
const putsBeforeBad = puts.length;
importInput.files = [{text: async () => JSON.stringify({format: 'other', version: 1, preferences: {}})}];
importInput.dispatch('change', {});
await tick();
assert.match(importStatus(), /Not a Kova UI preferences file/);
assert.match(importStatus(), /Nothing changed/);
assert.equal(puts.length, putsBeforeBad, 'a refused file is never applied');

console.log('Preferences export/import rows, summary and refusal passed');
