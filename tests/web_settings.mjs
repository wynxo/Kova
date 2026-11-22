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
const triggers = ['open-settings', 'theme', 'open-shortcuts', 'install-settings', 'open-sounds', 'connect-phone', 'zen-enter',
  'add-provider', 'open-model-settings', 'open-privacy', 'open-memory', 'open-reminders', 'open-extensions',
  'open-mcp', 'disconnect'];
for (const id of triggers) launch(id);
const autostart = launch('settings', 'details');
autostart.append(new Element('summary'), new Element('input'), new Element('p'), new Element('button'));

const reasoning = {value: 'collapsed',
  mode() { return this.value; },
  setMode(next) { this.value = next; }};
// A stand-in for theme.js: the choice, the picker's options and set().
const themeEngine = {value: 'system', sets: [],
  installed: [{id: 'midnight-teal', name: 'Midnight Teal', appearance: 'dark'}],
  choice() { return this.value; },
  choices() {
    return [{id: 'system', name: 'System', appearance: 'dark'}, {id: 'light', name: 'Light', appearance: 'light'},
      {id: 'dark', name: 'Dark', appearance: 'dark'}, ...this.installed.map(theme => ({...theme, source: 'file'}))];
  },
  set(value) { this.sets.push(value); this.value = value; }};
// A stand-in for typography.js: the three choices it applies onto <html>.
const typography = {textSizeValue: 'default', densityValue: 'default', fontValue: 'inter',
  textSize() { return this.textSizeValue; },
  density() { return this.densityValue; },
  font() { return this.fontValue; },
  setTextSize(next) { this.textSizeValue = next; },
  setDensity(next) { this.densityValue = next; },
  setFont(next) { this.fontValue = next; }};
const motion = {value: 'full',
  mode() { return this.value; },
  setMode(next) { this.value = next; }};
let returnFromThemeEditor;
const themeEditor = {open(onClose) { returnFromThemeEditor = onClose; }, dismiss() { return true; }};
let galleryOpener;
const themeGallery = {open(button) { galleryOpener = button; }};
// A stand-in for thinking.js: the choice the activity mark animates with.
const thinking = {value: 'thunderstorm',
  choice() { return this.value; },
  setChoice(next) { this.value = next; }};
const window = {KovaThemeEditor: themeEditor, KovaThemeGallery: themeGallery, KovaReasoning: reasoning, KovaTheme: themeEngine, KovaTypography: typography, KovaMotion: motion, KovaThinking: thinking};
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
const SECTIONS = ['General', 'Chat', 'Models and providers', 'Memory', 'Reminders', 'Extensions and MCP',
  'Privacy', 'Phone', 'Install and notifications'];
const section = label => SECTIONS.indexOf(label);
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
assert.equal(document.activeElement, tabs.at(-1), 'End moves to the last section');
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
const browse = walk(themeRow).find(node => node.textContent === 'Browse themes…');
browse.click();
assert.equal(galleryOpener, browse, 'General launches the gallery and supplies its focus return target');
assert.equal(view.hidden, false, 'the gallery previews over Settings without changing its state');
const customize = walk(themeRow).find(node => node.textContent === 'Customize…');
customize.click();
assert.equal(view.hidden, true, 'the real app becomes visible behind the editor');
returnFromThemeEditor();
assert.equal(view.hidden, false, 'the editor returns to General');
assert.equal(document.activeElement, customize, 'focus returns to Customize');
search.value = 'theme';
search.dispatch('input', {});

assert.match($('settings-search-status').textContent, /17 settings match/);
assert.equal($('settings-no-results').hidden, true);

// A keyword that belongs to one row narrows to that row.
search.value = 'chatgpt';
search.dispatch('input', {});
assert.equal(tabs.filter(tab => !tab.hidden).length, 1);
assert.equal(tabs[section('Models and providers')].hidden, false, 'the matching Models section stays');
assert.match($('settings-search-status').textContent, /1 setting matches/);

search.value = 'theme';
search.dispatch('input', {});

search.value = 'privacy';
search.dispatch('input', {});
assert.equal(tabs.filter(tab => !tab.hidden).length, 1);
assert.equal(tabs[section('Privacy')].hidden, false);
assert.equal(panels[section('Privacy')].hidden, false, 'the matching section becomes active');
assert.equal(tabs[section('Privacy')].getAttribute('aria-selected'), 'true');

// Every row that left the sidebar is findable by the word people would type.
for (const [word, section] of [['sounds', 'General'], ['sign out', 'General'], ['startup', 'General'],
  ['reasoning', 'General'], ['thinking', 'General'], ['motion', 'General'],
  ['text size', 'General'], ['density', 'General'], ['font', 'General'],
  ['message style', 'Chat'], ['timestamps', 'Chat'], ['zen mode', 'Chat'],
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
assert.equal(tabs.filter(tab => !tab.hidden).length, SECTIONS.length, 'clearing restores every section');
assert.equal($('settings-no-results').hidden, true);
assert.equal($('settings-search-status').textContent, '');

// A row runs the control app.js already bound; nothing is reimplemented here.
tabs[section('Memory')].click();
const memoryRow = byClass(panels[section('Memory')], 'settings-row').find(row => row.textContent.includes('Memory notes'));
const openMemory = byClass(memoryRow, 'settings-action')[0];
assert.ok(openMemory, 'the Memory row has an action');
openMemory.click();
assert.equal($('open-memory').clicked, 1);

// Each moved sidebar row runs its own target from its section.
for (const [label, target] of [['Reminders', 'open-reminders'], ['Extensions and MCP', 'open-extensions'],
  ['Extensions and MCP', 'open-mcp'], ['Privacy', 'open-privacy'], ['Phone', 'connect-phone'],
  ['Install and notifications', 'install-settings'], ['Models and providers', 'add-provider'],
  ['General', 'open-sounds'], ['General', 'disconnect'], ['Chat', 'zen-enter']]) {
  const index = section(label);
  const before = $(target).clicked;
  const actions = byClass(panels[index], 'settings-action');
  assert.ok(actions.length, `section ${SECTIONS[index]} has actions`);
  for (const button of actions) button.click();
  assert.equal($(target).clicked, before + 1, `${target} is reachable from ${SECTIONS[index]}`);
}

// Theme is one radio group: System, Light, Dark, then installed theme files (#174).
const themeRadios = () => byClass(byClass(panels[0], 'settings-row').find(row => row.textContent.includes('Theme')), 'settings-segment');
let radios = themeRadios();
assert.deepEqual(radios.map(radio => radio.getAttribute('role')), ['radio', 'radio', 'radio', 'radio']);
assert.deepEqual(radios.map(radio => radio.textContent), ['System', 'Light', 'Dark', 'Midnight Teal']);
assert.equal(radios[0].getAttribute('aria-checked'), 'true', 'System is the default choice');
assert.equal(radios[0].tabIndex, 0, 'the checked radio is the tab stop');
assert.equal(radios[2].tabIndex, -1);
const themeClicks = $('theme').clicked;
radios[1].click();
assert.deepEqual(themeEngine.sets, ['light'], 'choosing Light sets it through theme.js');
assert.equal($('theme').clicked, themeClicks, 'the engine is used, not the old toggle');
assert.equal(radios[1].getAttribute('aria-checked'), 'true');
assert.equal(radios[0].getAttribute('aria-checked'), 'false');
radios[1].dispatch('keydown', {key: 'ArrowRight'});
assert.equal(themeEngine.value, 'dark', 'arrow keys move through the choices');
assert.equal(document.activeElement, radios[2], 'and take focus with them');
radios[3].dispatch('keydown', {key: 'ArrowRight'});
assert.equal(themeEngine.value, 'system', 'the arrows wrap around');
radios[3].click();
assert.equal(themeEngine.value, 'midnight-teal', 'an installed theme is a choice like any other');
// A theme file installed while Settings is open shows up the next time it syncs.
themeEngine.installed.push({id: 'paper', name: 'Paper', appearance: 'light'});
document.activeElement = sidebar;
$('open-settings').click();
radios = themeRadios();
assert.deepEqual(radios.map(radio => radio.textContent), ['System', 'Light', 'Dark', 'Midnight Teal', 'Paper']);
assert.equal(radios[3].getAttribute('aria-checked'), 'true', 'the redrawn group keeps the choice');
// A choice the picker does not list (a theme file missing here) leaves one tab stop.
themeEngine.value = 'not-installed';
$('open-settings').click();
radios = themeRadios();
assert.equal(radios.filter(radio => radio.getAttribute('aria-checked') === 'true').length, 0);
assert.equal(radios.filter(radio => radio.tabIndex === 0).length, 1, 'still reachable from the keyboard');
themeEngine.value = 'system';

// Reasoning is a three-way radio group that changes the shared setting and
// reflects it back from the module, never a value duplicated in this file.
const reasoningRow = byClass(panels[0], 'settings-row').find(row => row.textContent.includes('Reasoning'));
assert.ok(reasoningRow, 'the Reasoning setting is in General');
const reasoningGroup = byClass(reasoningRow, 'settings-segmented')[0];
assert.equal(reasoningGroup.getAttribute('role'), 'radiogroup');
assert.equal(reasoningGroup.getAttribute('aria-label'), 'Reasoning');
const reasoningRadios = byClass(reasoningRow, 'settings-segment');
assert.deepEqual(reasoningRadios.map(radio => radio.textContent), ['Collapsed', 'Expanded', 'Hidden']);
assert.deepEqual(reasoningRadios.map(radio => radio.getAttribute('role')), ['radio', 'radio', 'radio']);
assert.equal(reasoningRadios[0].getAttribute('aria-checked'), 'true', 'collapsed is the default');
reasoningRadios[1].click();
assert.equal(reasoning.value, 'expanded', 'Expanded opens each finished row');
assert.equal(reasoningRadios[1].getAttribute('aria-checked'), 'true');
assert.equal(reasoningRadios[0].getAttribute('aria-checked'), 'false');
reasoningRadios[2].click();
assert.equal(reasoning.value, 'hidden', 'Hidden drops the reasoning text');
// Arrow keys move through the three choices, like the theme group.
reasoningRadios[2].dispatch('keydown', {key: 'ArrowRight', preventDefault() {}});
assert.equal(reasoning.value, 'collapsed', 'ArrowRight wraps to the first choice');
assert.equal(document.activeElement, reasoningRadios[0], 'focus follows the choice');
reasoning.value = 'expanded';
document.activeElement = sidebar;
$('open-settings').click();
assert.equal(reasoningRadios[1].getAttribute('aria-checked'), 'true',
  'reopening Settings reflects the current value');
reasoning.value = 'collapsed';

// Motion is the same three-way group, applied by KovaMotion onto <html>.
const motionRow = byClass(panels[0], 'settings-row').find(row => row.textContent.includes('Motion'));
assert.ok(motionRow, 'the Motion setting is in General');
const motionGroup = byClass(motionRow, 'settings-segmented')[0];
assert.equal(motionGroup.getAttribute('role'), 'radiogroup');
assert.equal(motionGroup.getAttribute('aria-label'), 'Motion');
const motionRadios = byClass(motionRow, 'settings-segment');
assert.deepEqual(motionRadios.map(radio => radio.textContent), ['Full', 'Reduced', 'Off']);
assert.deepEqual(motionRadios.map(radio => radio.getAttribute('role')), ['radio', 'radio', 'radio']);
assert.equal(motionRadios[0].getAttribute('aria-checked'), 'true', 'Full is the default');
motionRadios[1].click();
assert.equal(motion.value, 'reduced', 'Reduced shortens the motion');
assert.equal(motionRadios[1].getAttribute('aria-checked'), 'true');
assert.equal(motionRadios[0].getAttribute('aria-checked'), 'false');
motionRadios[2].click();
assert.equal(motion.value, 'off', 'Off removes it');
motionRadios[2].dispatch('keydown', {key: 'ArrowRight', preventDefault() {}});
assert.equal(motion.value, 'full', 'arrow keys wrap to the first choice');
assert.equal(document.activeElement, motionRadios[0], 'focus follows the choice');
motion.value = 'reduced';
document.activeElement = sidebar;
$('open-settings').click();
assert.equal(motionRadios[1].getAttribute('aria-checked'), 'true',
  'reopening Settings reflects the current value');
motion.value = 'full';

// The thinking animation is the same group (#171): off, one named movement, or
// a different one each reply, applied by KovaThinking onto the activity mark.
const thinkingRow = byClass(panels[0], 'settings-row').find(row => row.textContent.includes('Thinking animation'));
assert.ok(thinkingRow, 'the Thinking animation setting is in General');
const thinkingGroup = byClass(thinkingRow, 'settings-segmented')[0];
assert.equal(thinkingGroup.getAttribute('role'), 'radiogroup');
assert.equal(thinkingGroup.getAttribute('aria-label'), 'Thinking animation');
const thinkingRadios = byClass(thinkingRow, 'settings-segment');
assert.deepEqual(thinkingRadios.map(radio => radio.getAttribute('role')),
  ['radio', 'radio', 'radio', 'radio', 'radio', 'radio', 'radio']);
assert.deepEqual(thinkingRadios.map(radio => radio.textContent),
  ['Off', 'Thunderstorm', 'Orbit', 'Collide', 'Breathe', 'Charge up', 'Random']);
assert.equal(thinkingRadios[1].getAttribute('aria-checked'), 'true', 'the bolt is the default');
assert.equal(thinkingRadios[1].tabIndex, 0, 'the checked radio is the tab stop');
thinkingRadios[2].click();
assert.equal(thinking.value, 'orbit', 'Orbit is applied through thinking.js');
assert.equal(thinkingRadios[2].getAttribute('aria-checked'), 'true');
assert.equal(thinkingRadios[1].getAttribute('aria-checked'), 'false');
thinkingRadios[0].click();
assert.equal(thinking.value, 'off', 'Off leaves the mark still');
thinkingRadios[6].click();
assert.equal(thinking.value, 'random', 'Random picks one per reply');
thinkingRadios[6].dispatch('keydown', {key: 'ArrowRight', preventDefault() {}});
assert.equal(thinking.value, 'off', 'arrow keys wrap to the first choice');
assert.equal(document.activeElement, thinkingRadios[0], 'focus follows the choice');
thinking.value = 'charge';
document.activeElement = sidebar;
$('open-settings').click();
assert.equal(thinkingRadios[5].getAttribute('aria-checked'), 'true',
  'reopening Settings reflects the current animation');
thinking.value = 'thunderstorm';

// Text size, density and font are the same three-way group, applied by
// KovaTypography onto <html> (#179). Each row reflects the module's value.
for (const [label, options, read, write, values, fallback] of [
  ['Text size', ['Small', 'Default', 'Large'], 'textSize', 'setTextSize', ['small', 'default', 'large'], 'default'],
  ['Density', ['Compact', 'Default', 'Comfortable'], 'density', 'setDensity', ['compact', 'default', 'comfortable'], 'default'],
  ['Font', ['Geologica', 'System', 'Monospace'], 'font', 'setFont', ['inter', 'system', 'mono'], 'inter']]) {
  const row = byClass(panels[0], 'settings-row').find(item =>
    byClass(item, 'settings-row-title')[0]?.textContent === label);
  assert.ok(row, `the ${label} setting is in General`);
  const group = byClass(row, 'settings-segmented')[0];
  assert.equal(group.getAttribute('role'), 'radiogroup');
  assert.equal(group.getAttribute('aria-label'), label);
  const radios = byClass(row, 'settings-segment');
  assert.deepEqual(radios.map(radio => radio.textContent), options);
  const checked = radios.findIndex(radio => radio.getAttribute('aria-checked') === 'true');
  assert.equal(radios[checked].dataset.value, fallback, `${label} starts at the shipped look`);
  assert.equal(radios[checked].tabIndex, 0, 'the checked radio is the tab stop');
  radios[2].click();
  assert.equal(typography[read](), values[2], `${label} is applied through typography.js`);
  assert.equal(radios[2].getAttribute('aria-checked'), 'true');
  assert.equal(radios[checked].getAttribute('aria-checked'), 'false');
  radios[2].dispatch('keydown', {key: 'ArrowRight', preventDefault() {}});
  assert.equal(typography[read](), values[0], `${label} arrow keys wrap to the first choice`);
  assert.equal(document.activeElement, radios[0], 'focus follows the choice');
  typography[write](values[1]);
  document.activeElement = sidebar;
  $('open-settings').click();
  assert.equal(radios[1].getAttribute('aria-checked'), 'true',
    `reopening Settings reflects the current ${label.toLowerCase()}`);
  typography[write](fallback);
}

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
assert.equal(tabs[section('Phone')].hidden, true, 'no Phone section on the phone itself');
$('connect-phone').hidden = false;
assert.equal(view.hidden, false, 'the palette can open Settings at a section');

// Escape closes the view and returns focus to the sidebar trigger.
view.dispatch('keydown', {key: 'Escape', preventDefault() {}});
assert.equal(view.hidden, true);
assert.equal(document.activeElement, sidebar, 'focus returns to the opener');
assert.equal(body.classList.contains('settings-open'), false);

console.log('Settings sections, search, hosting and keyboard navigation passed');
