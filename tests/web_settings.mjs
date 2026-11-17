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
  body, activeElement: null, documentElement: {dataset: {theme: 'dark'}},
  createElement: tag => new Element(tag),
  createTextNode: text => { const node = new Element('#text'); node.textContent = text; return node; },
  getElementById(id) { return nodes.get(id) || walk(body).find(n => n.id === id) || null; },
};
function launch(id, tag = 'button') {
  const node = new Element(tag); node.id = id; nodes.set(id, node); body.append(node); return node;
}

// The action targets app.js owns (index.html #settings-actions), and the
// startup card settings.js hosts. The sidebar keeps only #open-settings.
const triggers = ['open-settings', 'theme', 'open-shortcuts', 'install-settings', 'open-sounds', 'connect-phone',
  'add-provider', 'open-model-settings', 'open-privacy', 'open-memory', 'open-reminders', 'open-extensions',
  'open-mcp', 'disconnect'];
for (const id of triggers) launch(id);
const autostart = launch('settings', 'details');
autostart.append(new Element('summary'), new Element('input'), new Element('p'), new Element('button'));

const reasoning = {value: 'shown',
  shown() { return this.value === 'shown'; },
  setShown(visible) { this.value = visible ? 'shown' : 'hidden'; }};
const window = {KovaReasoning: reasoning};
const sandbox = {document, window, console, URLSearchParams};
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
const SECTIONS = ['General', 'Models and providers', 'Memory', 'Reminders', 'Extensions and MCP',
  'Privacy', 'Phone', 'Install and notifications'];
assert.deepEqual(tabs.map(tab => tab.textContent.trim()), SECTIONS);
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
assert.equal(document.activeElement, tabs[7], 'End moves to the last section');
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
assert.match($('settings-search-status').textContent, /7 settings match/);
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
assert.equal(tabs[5].hidden, false);
assert.equal(panels[5].hidden, false, 'the matching section becomes active');
assert.equal(tabs[5].getAttribute('aria-selected'), 'true');

// Every row that left the sidebar is findable by the word people would type.
for (const [word, section] of [['sounds', 'General'], ['sign out', 'General'], ['startup', 'General'],
  ['reasoning', 'General'], ['thinking', 'General'],
  ['provider', 'Models and providers'], ['remember', 'Memory'], ['reminders', 'Reminders'],
  ['mcp', 'Extensions and MCP'], ['plugins', 'Extensions and MCP'], ['local only', 'Privacy'],
  ['phone', 'Phone'], ['notifications', 'Install and notifications']]) {
  search.value = word; search.dispatch('input', {});
  const shownTabs = tabs.filter(tab => !tab.hidden).map(tab => tab.textContent.trim());
  assert.ok(shownTabs.includes(section), `'${word}' should find ${section}, found ${shownTabs}`);
}

search.value = 'zzz';
search.dispatch('input', {});
assert.equal(tabs.filter(tab => !tab.hidden).length, 0);
assert.equal($('settings-no-results').hidden, false);
assert.match($('settings-search-status').textContent, /No settings match/);
assert.ok(panels.every(panel => panel.hidden), 'no section is left open');

search.value = '';
search.dispatch('input', {});
assert.equal(tabs.filter(tab => !tab.hidden).length, 8, 'clearing restores every section');
assert.equal($('settings-no-results').hidden, true);
assert.equal($('settings-search-status').textContent, '');

// A row runs the control app.js already bound; nothing is reimplemented here.
tabs[2].click();
const memoryRow = byClass(panels[2], 'settings-row').find(row => row.textContent.includes('Memory notes'));
const openMemory = byClass(memoryRow, 'settings-action')[0];
assert.ok(openMemory, 'the Memory row has an action');
openMemory.click();
assert.equal($('open-memory').clicked, 1);

// Each moved sidebar row runs its own target from its section.
for (const [index, target] of [[3, 'open-reminders'], [4, 'open-extensions'], [4, 'open-mcp'], [5, 'open-privacy'],
  [6, 'connect-phone'], [7, 'install-settings'], [1, 'add-provider'], [0, 'open-sounds'], [0, 'disconnect']]) {
  const before = $(target).clicked;
  const actions = byClass(panels[index], 'settings-action');
  assert.ok(actions.length, `section ${SECTIONS[index]} has actions`);
  for (const button of actions) button.click();
  assert.equal($(target).clicked, before + 1, `${target} is reachable from ${SECTIONS[index]}`);
}

// Theme is a real Dark/Light radio group that flips app.js's theme control.
const radios = byClass(panels[0], 'settings-segment');
assert.deepEqual(radios.map(radio => radio.getAttribute('role')), ['radio', 'radio']);
assert.equal(radios[0].getAttribute('aria-checked'), 'true', 'dark is current');
const themeClicks = $('theme').clicked;
radios[0].click();
assert.equal($('theme').clicked, themeClicks, 'choosing the current theme does nothing');
radios[1].click();
assert.equal($('theme').clicked, themeClicks + 1, 'choosing Light asks app.js to switch');

// Reasoning is a real checkbox that flips the shared setting and reflects it
// back from the module, never a value duplicated in this file.
const reasoningRow = byClass(panels[0], 'settings-row').find(row => row.textContent.includes('Reasoning'));
assert.ok(reasoningRow, 'the Reasoning setting is in General');
const reasoningToggle = walk(reasoningRow).find(node => node.tagName === 'INPUT');
assert.equal(reasoningToggle.type, 'checkbox');
assert.equal(reasoningToggle.checked, true, 'reasoning is shown by default');
assert.equal(reasoningToggle.getAttribute('aria-label'), 'Show reasoning');
reasoningToggle.checked = false;
reasoningToggle.dispatch('change', {});
assert.equal(reasoning.value, 'hidden', 'unchecking hides reasoning');
assert.equal(reasoningToggle.checked, false);
reasoning.value = 'shown';
document.activeElement = sidebar;
$('open-settings').click();
assert.equal(reasoningToggle.checked, true, 'reopening Settings reflects the current value');

// Startup is hosted in General: open, keeping its own ids and handlers.
const hosted = $('settings');
assert.equal(hosted.open, true);
assert.ok(hosted.classList.contains('settings-hosted'));
assert.ok(walk(panels[0]).includes(hosted), 'startup lives in General');
assert.ok(panels[0].textContent.includes('Startup'));

// A target another script hides (Connect your phone, on a phone) hides its row.
view.dispatch('keydown', {key: 'Escape', preventDefault() {}});
$('connect-phone').hidden = true;
window.KovaSettings.open('general');
assert.equal(tabs[6].hidden, true, 'no Phone section on the phone itself');
$('connect-phone').hidden = false;
assert.equal(view.hidden, false, 'the palette can open Settings at a section');

// Escape closes the view and returns focus to the sidebar trigger.
view.dispatch('keydown', {key: 'Escape', preventDefault() {}});
assert.equal(view.hidden, true);
assert.equal(document.activeElement, sidebar, 'focus returns to the opener');
assert.equal(body.classList.contains('settings-open'), false);

console.log('Settings sections, search, hosting and keyboard navigation passed');
