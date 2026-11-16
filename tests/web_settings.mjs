// Exercise the real Settings view with an offline DOM and API boundary.
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import vm from 'node:vm';

class Element {
  constructor(tag = 'div') {
    this.tagName = tag.toUpperCase();
    this.children = []; this.parentNode = null; this.className = ''; this.id = '';
    this.value = ''; this.hidden = false; this.disabled = false; this.open = false;
    this.attrs = {}; this.dataset = {}; this.style = {}; this.listeners = {};
    this.tabIndex = -1; this._text = ''; this.clicked = 0;
    const names = new Set();
    this.classList = {
      add: (...items) => { for (const item of items) names.add(item); this.className = [...names].join(' '); },
      remove: (...items) => { for (const item of items) names.delete(item); this.className = [...names].join(' '); },
      contains: name => names.has(name) || this.className.split(' ').includes(name),
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
  focus() { document.activeElement = this; }
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
const body = new Element('body');
const document = {
  body, activeElement: null,
  createElement: tag => new Element(tag),
  createTextNode: text => { const node = new Element('#text'); node.textContent = text; return node; },
  getElementById(id) { return nodes.get(id) || walk(body).find(n => n.id === id) || null; },
};
function launch(id, tag = 'button') {
  const node = new Element(tag); node.id = id; nodes.set(id, node); body.append(node); return node;
}

// The controls app.js owns, and the old autostart card settings.js moves.
const triggers = ['open-settings', 'theme', 'open-shortcuts', 'install-settings', 'open-sounds', 'connect-phone',
  'add-provider', 'open-model-settings', 'open-privacy', 'open-memory', 'open-extensions', 'open-mcp'];
for (const id of triggers) launch(id);
const autostart = launch('settings', 'details');
autostart.append(new Element('summary'), new Element('input'), new Element('p'), new Element('button'));

const sandbox = {document, console, URLSearchParams};
vm.runInNewContext(readFileSync(new URL('../src/kova/web_assets/settings.js', import.meta.url), 'utf8'), sandbox);
const $ = id => document.getElementById(id);
const view = $('settings-page');
assert.ok(view, 'the settings view is created');
assert.equal(view.hidden, true, 'the view starts hidden');
assert.equal(view.getAttribute('role'), 'region');
assert.equal(view.getAttribute('aria-labelledby'), 'settings-title');

// Opening follows the sidebar trigger, labels the view, and lands on search.
const sidebar = $('open-settings');
document.activeElement = sidebar;
sidebar.click();
assert.equal(view.hidden, false);
assert.equal(document.activeElement, $('settings-search'), 'search takes focus');
assert.equal(view.getAttribute('aria-describedby'), 'settings-description');

const tabs = byClass(view, 'settings-tab');
const panels = byClass(view, 'settings-section');
assert.deepEqual(tabs.map(tab => tab.textContent.trim()),
  ['General', 'Models and providers', 'Privacy', 'Memory', 'Extensions', 'Startup']);
assert.equal(panels.length, tabs.length);
assert.equal(byClass(view, 'settings-nav')[0].getAttribute('role'), 'tablist');
for (const [index, tab] of tabs.entries()) {
  const panel = view.querySelectorAll('section')[index];
  assert.equal(tab.getAttribute('role'), 'tab');
  assert.equal(panel.getAttribute('role'), 'tabpanel');
  assert.equal(tab.getAttribute('aria-controls'), panel.id);
  assert.equal(panel.getAttribute('aria-labelledby'), tab.id);
}
assert.equal(panels.filter(panel => !panel.hidden).length, 1, 'one panel at a time');
assert.equal(panels[0].hidden, false, 'General is the default section');

// Tabs are operable with the arrow keys, not just the mouse.
const nav = byClass(view, 'settings-nav')[0];
document.activeElement = tabs[1];
nav.dispatch('keydown', {key: 'ArrowDown', preventDefault() {}});
assert.equal(document.activeElement, tabs[2], 'ArrowDown moves to the next section');
assert.equal(tabs[2].getAttribute('aria-selected'), 'true');
assert.equal(panels[2].hidden, false);
assert.equal(panels[1].hidden, true);
document.activeElement = tabs[0];
nav.dispatch('keydown', {key: 'End', preventDefault() {}});
assert.equal(document.activeElement, tabs[5], 'End moves to the last section');
nav.dispatch('keydown', {key: 'Home', preventDefault() {}});
assert.equal(document.activeElement, tabs[0], 'Home moves to the first section');

// Search filters sections and individual settings, and marks the match.
const search = $('settings-search');
search.value = 'theme';
search.dispatch('input', {});
assert.equal(tabs.filter(tab => !tab.hidden).length, 1, 'only General survives');
assert.equal(tabs[0].hidden, false);
const themeRow = byClass(panels[0], 'settings-row').find(row => row.textContent.includes('Theme'));
assert.ok(themeRow, 'the Theme setting is shown');
const title = byClass(themeRow, 'settings-row-title')[0];
const mark = walk(title).find(node => node.tagName === 'MARK');
assert.equal(mark?.textContent, 'Theme', 'the matched word is highlighted');
assert.match($('settings-search-status').textContent, /5 settings match/);
assert.equal($('settings-no-results').hidden, true);

// A keyword that belongs to one row narrows to that row.
search.value = 'image';
search.dispatch('input', {});
assert.equal(tabs.filter(tab => !tab.hidden).length, 1);
assert.equal(tabs[1].hidden, false, 'the matching Models section stays');
assert.match($('settings-search-status').textContent, /1 setting matches/);

search.value = 'theme';
search.dispatch('input', {});

search.value = 'privacy';
search.dispatch('input', {});
assert.equal(tabs.filter(tab => !tab.hidden).length, 1);
assert.equal(tabs[2].hidden, false);
assert.equal(panels[2].hidden, false, 'the matching section becomes active');
assert.equal(tabs[2].getAttribute('aria-selected'), 'true');

search.value = 'zzz';
search.dispatch('input', {});
assert.equal(tabs.filter(tab => !tab.hidden).length, 0);
assert.equal($('settings-no-results').hidden, false);
assert.match($('settings-search-status').textContent, /No settings match/);
assert.ok(panels.every(panel => panel.hidden), 'no section is left open');

search.value = '';
search.dispatch('input', {});
assert.equal(tabs.filter(tab => !tab.hidden).length, 6, 'clearing restores every section');
assert.equal($('settings-no-results').hidden, true);
assert.equal($('settings-search-status').textContent, '');

// A row runs the control app.js already bound; nothing is reimplemented here.
tabs[3].click();
const memoryRow = byClass(panels[3], 'settings-row').find(row => row.textContent.includes('Memory notes'));
const openMemory = byClass(memoryRow, 'settings-open')[0];
assert.ok(openMemory, 'the Memory row has an action');
openMemory.click();
assert.equal($('open-memory').clicked, 1);

// The existing autostart card is hosted, open, and keeps its own ids.
const hosted = $('settings');
assert.equal(hosted.open, true);
assert.ok(hosted.classList.contains('settings-autostart'));
assert.ok(byClass(panels[5], 'settings-host')[0].children.includes(hosted));
assert.ok(panels[5].textContent.includes('Start Kova when I sign in'));

// Escape closes the view and returns focus to the sidebar trigger.
view.dispatch('keydown', {key: 'Escape', preventDefault() {}});
assert.equal(view.hidden, true);
assert.equal(document.activeElement, sidebar, 'focus returns to the opener');
assert.equal(body.classList.contains('settings-open'), false);

console.log('Settings sections, search, hosting and keyboard navigation passed');
