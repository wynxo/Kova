// panes.js (#182): side-panel tabs, toggling, the icon rail and moving focus between panes.
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import vm from 'node:vm';

let active = null;
class Element {
  constructor(tag) {
    this.tagName = tag; this.children = []; this.parent = null; this.attrs = {}; this.text = '';
    this.hidden = false; this.listeners = {}; this.disabled = false; this.style = {setProperty() {}};
    this._class = new Set();
    this.classList = {
      add: name => this._class.add(name), remove: name => this._class.delete(name),
      contains: name => this._class.has(name),
      toggle: (name, force) => ((force ?? !this._class.has(name)) ? this._class.add(name) : this._class.delete(name)),
    };
  }
  get className() { return [...this._class].join(' '); }
  set className(value) { this._class = new Set(String(value).split(' ').filter(Boolean)); }
  get id() { return this.attrs.id || ''; }
  set textContent(value) { this.text = String(value); this.children = []; }
  get textContent() { return this.text + this.children.map(child => child.textContent).join(''); }
  get isConnected() { let node = this; while (node.parent) node = node.parent; return node === document.documentElement; }
  get lastElementChild() { return this.children.at(-1) || null; }
  append(...nodes) { for (const node of nodes) { node.parent?.children.splice(node.parent.children.indexOf(node), 1); node.parent = this; this.children.push(node); } }
  replaceChildren(...nodes) { for (const node of this.children) node.parent = null; this.children = []; this.text = ''; this.append(...nodes); }
  remove() { if (!this.parent) return; this.parent.children.splice(this.parent.children.indexOf(this), 1); this.parent = null; }
  setAttribute(key, value) { this.attrs[key] = String(value); }
  getAttribute(key) { return key in this.attrs ? this.attrs[key] : null; }
  hasAttribute(key) { return key in this.attrs; }
  removeAttribute(key) { delete this.attrs[key]; }
  get attributes() { return Object.entries(this.attrs).map(([name, value]) => ({name, value})); }
  addEventListener(type, fn) { (this.listeners[type] ||= []).push(fn); }
  fire(type, event = {}) {
    const full = {target: this, preventDefault() { this.defaultPrevented = true; }, stopPropagation() {}, ...event};
    for (let node = this; node; node = node.parent) for (const fn of node.listeners[type] || []) fn(full);
    return full;
  }
  click() { this.fire('click', {button: 0}); }
  focus() { active = this; }
  contains(node) { for (let item = node; item; item = item.parent) if (item === this) return true; return false; }
  all() { return [this, ...this.children.flatMap(child => child.all())]; }
  querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }
  querySelectorAll(selector) {
    const tests = selector.split(',').map(part => part.trim().split(':')[0]);
    return this.all().filter(node => node !== this && tests.some(test =>
      test.startsWith('.') ? test.slice(1).split('.').every(name => node.classList.contains(name)) : node.tagName === test));
  }
  getClientRects() { return this.isShown() ? [{}] : []; }
  isShown() { for (let node = this; node; node = node.parent) if (node.hidden) return false; return this.isConnected; }
  scrollIntoView() {}
}
const html = new Element('html'), body = new Element('body');
html.append(body);
const byId = {};
function make(tag, id, parent, cls) { const node = new Element(tag); node.setAttribute('id', id); if (cls) node.className = cls; parent.append(node); byId[id] = node; return node; }
const sidebar = make('aside', 'sidebar', body);
make('button', 'new-chat', sidebar);
make('input', 'search', sidebar);
make('button', 'open-settings', sidebar);
const main = new Element('main'); body.append(main);
make('button', 'menu', main);
const prompt = make('textarea', 'prompt', main);

const storage = {};
const document = {
  body, documentElement: html,
  get activeElement() { return active; },
  createElement: tag => new Element(tag),
  getElementById: id => byId[id] || null,
  querySelector: selector => (selector === 'main' ? main : null),
  querySelectorAll: () => [],
};
let wide = true;
let opened = [];
const toasts = [];
const window = {
  innerWidth: 1600, innerHeight: 900,
  matchMedia: () => ({get matches() { return wide; }, addEventListener() {}}),
  addEventListener() {}, setInterval: () => 0, clearInterval() {}, focus() {},
  open: (...args) => { opened.push(args); return null; },   // a blocked pop-up
  KovaToast: {show: (message, options) => toasts.push({message, options})},
};
const context = {
  document, window,
  localStorage: {getItem: key => storage[key] ?? null, setItem: (key, value) => { storage[key] = String(value); }, removeItem: key => { delete storage[key]; }},
  getComputedStyle: node => ({
    getPropertyValue: name => ({'--pane-chat-min': '360px', '--pane-sidebar-min': '200px', '--pane-sidebar-max': '480px',
      '--sidebar-w': '272px', '--pane-panel-w': '480px', '--pane-panel-min': '320px', '--pane-panel-max': '960px',
      '--pane-rail-w': '48px'})[name] || '',
    visibility: node?.hidden ? 'hidden' : 'visible',
  }),
};
vm.createContext(context);
vm.runInContext(readFileSync(new URL('../src/kova/web_assets/panes.js', import.meta.url), 'utf8'), context);
const {KovaPanel, KovaPanes} = window;
const panel = byId['side-panel'] || body.all().find(node => node.classList.contains('side-panel'));
const byClass = (node, cls) => node.all().filter(child => child.classList.contains(cls));
const tabs = () => byClass(panel, 'side-panel-tab');
const views = () => byClass(panel, 'side-panel-tabpanel');

// Opening shows a tab and focuses its content; the opener gets focus back later.
const opener = make('button', 'opener', main);
opener.focus();
const first = new Element('div');
KovaPanel.open({title: 'main.py · 150 lines', node: first, key: 'code:1'});
assert.equal(KovaPanel.isOpen(), true);
assert.ok(body.classList.contains('panel-open'));
const tablist = panel.querySelector('.side-panel-tabs');
assert.equal(tablist.attrs.role, 'tablist');
assert.equal(tabs().length, 1);
assert.equal(tabs()[0].attrs.role, 'tab');
assert.equal(tabs()[0].attrs['aria-selected'], 'true');
assert.equal(tabs()[0].attrs['aria-controls'], views()[0].attrs.id);
assert.equal(views()[0].attrs.role, 'tabpanel');
assert.equal(active, views()[0], 'the new tab’s content has focus');

// A second item adds a tab; the same key refreshes its tab instead of adding one.
let closedOld = 0;
KovaPanel.open({title: 'Diff · a.txt', node: new Element('div'), key: 'diff:a', onClose: () => closedOld++});
assert.equal(tabs().length, 2);
assert.equal(tabs()[1].attrs['aria-selected'], 'true');
KovaPanel.open({title: 'Diff · a.txt', node: new Element('section'), key: 'diff:a'});
assert.equal(tabs().length, 2, 'one tab per key');
assert.equal(closedOld, 1, 'the replaced content is told it closed');
assert.equal(views()[1].children[0].tagName, 'section');

// A background tab does not take the front or the focus.
active = opener;
KovaPanel.open({title: 'Diff · b.txt', node: new Element('div'), key: 'diff:b', background: true});
assert.equal(tabs().length, 3);
assert.equal(tabs()[2].attrs['aria-selected'], 'false');
assert.equal(views()[2].hidden, true);
assert.equal(active, opener);

// Arrow keys move between tabs (and wrap); Delete closes the focused one.
tabs()[1].fire('keydown', {key: 'ArrowRight'});
assert.equal(tabs()[2].attrs['aria-selected'], 'true');
assert.equal(active, tabs()[2]);
tabs()[2].fire('keydown', {key: 'ArrowRight'});
assert.equal(active, tabs()[0], 'ArrowRight wraps to the first tab');
tabs()[0].fire('keydown', {key: 'End'});
assert.equal(active, tabs()[2]);
assert.equal(tabs().filter(tab => tab.attrs.tabindex === '0').length, 1, 'only the selected tab is in the Tab order');
tabs()[2].fire('keydown', {key: 'Delete'});
assert.equal(tabs().length, 2);
assert.equal(tabs()[1].attrs['aria-selected'], 'true', 'the neighbour takes over');
assert.equal(active, tabs()[1]);

// Escape hides the panel but keeps its tabs; the toggle brings them back.
const escape = views()[1].fire('keydown', {key: 'Escape'});
assert.ok(escape.defaultPrevented);
assert.equal(KovaPanel.isOpen(), false);
assert.equal(active, opener, 'focus goes back to what opened the panel');
assert.equal(tabs().length, 2);
assert.equal(KovaPanel.toggle(), true);
assert.equal(KovaPanel.isOpen(), true);
assert.equal(active, tabs()[1]);
assert.equal(KovaPanel.toggle(), true);
assert.equal(KovaPanel.isOpen(), false);

// F6 cycles sidebar → conversation → panel, and skips the panel while it is hidden.
prompt.focus();
KovaPanes.focusNext();
assert.ok(sidebar.contains(active), 'from the conversation to the sidebar (wraps)');
KovaPanes.focusNext();
assert.equal(active, prompt, 'the conversation pane lands in the message box');
KovaPanel.toggle();
prompt.focus();
KovaPanes.focusNext();
assert.ok(panel.contains(active), 'the open panel is a pane');
assert.equal(active, tabs().find(tab => tab.attrs['aria-selected'] === 'true'));
KovaPanes.focusPrevious();
assert.equal(active, prompt);

// Collapsing the sidebar leaves the icon rail, which is a pane of its own.
const rail = byClass(body, 'sidebar-rail')[0];
assert.equal(rail.tagName, 'nav');
assert.deepEqual(rail.children.filter(child => child.tagName === 'button').map(button => button.attrs['aria-label']),
                 ['Show sidebar', 'New chat', 'Search conversations', 'Settings']);
let menuClicks = 0, settingsClicks = 0;
byId.menu.addEventListener('click', () => { menuClicks++; body.classList.toggle('sidebar-collapsed'); });
byId['open-settings'].addEventListener('click', () => settingsClicks++);
body.classList.add('sidebar-collapsed');
sidebar.hidden = true;     // stands in for app.css hiding the collapsed sidebar
prompt.focus();
KovaPanes.focusPrevious();
assert.ok(rail.contains(active), 'the rail takes the sidebar’s place in the cycle');
assert.equal(active.attrs['aria-label'], 'Show sidebar');
rail.children.find(child => child.attrs['aria-label'] === 'Settings').click();
assert.equal(settingsClicks, 1);
rail.children.find(child => child.attrs['aria-label'] === 'Search conversations').click();
assert.equal(menuClicks, 1, 'search expands the sidebar first');
assert.equal(active, byId.search);

// A blocked pop-up keeps the tab in the panel and says what to do.
const before = tabs().length;
panel.querySelector('.side-panel-popout').click();
assert.equal(opened.length, 1);
assert.match(opened[0][1], /^kova-panel-\d+$/, 'the window name the desktop app allows');
assert.equal(tabs().length, before);
assert.match(toasts[0].message, /Allow pop-ups/);

// The close-tab button closes the selected tab; closing the last one hides the panel.
const closeTab = panel.querySelector('.side-panel-close-tab');
closeTab.focus();
closeTab.click();
closeTab.focus();
closeTab.click();
assert.equal(tabs().length, 0);
assert.equal(KovaPanel.isOpen(), false);
assert.equal(KovaPanel.toggle(), false, 'an empty panel has nothing to show');

// On a phone the panel is a modal sheet and there is nothing to pop out.
wide = false;
KovaPanel.open({title: 'Output', text: 'hello'});
assert.equal(panel.attrs.role, 'dialog');
assert.equal(panel.attrs['aria-modal'], 'true');
assert.ok(main.hasAttribute('inert') && rail.hasAttribute('inert'));
assert.equal(panel.querySelector('.side-panel-popout').hidden, true);
prompt.focus();
KovaPanes.focusNext();
assert.ok(panel.contains(active), 'focus cannot leave the sheet');
console.log('panes: tabs, toggle, rail, pane focus and pop-out refusal passed');
