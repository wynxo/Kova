// Quick ask overlay (#71): the /app?quick=1 branch of app.js, with no browser.
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import vm from 'node:vm';

let focusedElement = null;
const descendants = node => node.children.flatMap(child => [child, ...descendants(child)]);
class Element {
  constructor(tag = 'div') {
    this.tagName = tag; this.children = []; this.parentNode = null;
    this.className = ''; this.value = ''; this.disabled = false; this.hidden = false;
    this.listeners = {}; this.attrs = {}; this.dataset = {}; this.style = {};
    this.classList = {
      toggle: (name, force) => {
        const names = new Set(this.className.split(' ').filter(Boolean));
        const add = force ?? !names.has(name); if (add) names.add(name); else names.delete(name);
        this.className = [...names].join(' ');
      },
      add: name => { const names = new Set(this.className.split(' ').filter(Boolean)); names.add(name); this.className = [...names].join(' '); },
      remove: name => { const names = new Set(this.className.split(' ').filter(Boolean)); names.delete(name); this.className = [...names].join(' '); },
      contains: name => this.className.split(' ').includes(name)};
  }
  set textContent(text) { this._text = text; this.children = []; }
  get textContent() { return (this._text || '') + this.children.map(n => n.textContent).join(''); }
  append(...nodes) { for (const n of nodes) { n.parentNode = this; this.children.push(n); } }
  replaceChildren(...nodes) { this._text = ''; this.children = []; this.append(...nodes); }
  insertBefore(node, before) { const i = this.children.indexOf(before); node.parentNode = this; this.children.splice(i < 0 ? this.children.length : i, 0, node); }
  remove() { if (this.parentNode) this.parentNode.children.splice(this.parentNode.children.indexOf(this), 1); }
  replaceWith(node) { const parent = this.parentNode, i = parent.children.indexOf(this); parent.children[i] = node; node.parentNode = parent; }
  setAttribute(key, value) { this.attrs[key] = String(value); }
  getAttribute(key) { return this.attrs[key]; }
  removeAttribute(key) { delete this.attrs[key]; }
  addEventListener(type, fn) { this.listeners[type] = fn; }
  dispatch(type, event = {}) { this.listeners[type]?.(event); }
  select() {}
  querySelectorAll(selector) {
    // The quick overlay only asks for classes; anything else stays absent, as
    // it was before, so the wider app's startup is unchanged.
    if (typeof selector !== 'string' || !selector.startsWith('.')) return [];
    const name = selector.slice(1);
    return descendants(this).filter(node => String(node.className || '').split(' ').includes(name));
  }
  querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }
  closest() { return null; }
  getBoundingClientRect() { return {top: 0, bottom: 0, left: 0, right: 0, width: 0}; }
  scrollIntoView() {}
  focus() { focusedElement = this; }
  showModal() {}
  close() {}
  get lastElementChild() { return this.children.at(-1); }
  get options() { return this.children.flatMap(child => child.tagName === 'optgroup' ? child.children : [child]); }
}
const elements = new Map();
function $(id) { if (!elements.has(id)) elements.set(id, new Element()); return elements.get(id); }
for (const id of ['theme', 'web']) $(id).append(new Element('span'));
$('mode').value = 'work';
const footer = new Element(), actions = new Element(); footer.append(actions); actions.append($('disconnect'));
const requests = [], prefs = new Map();
let defaultModel = '';
const quickCalls = {selection: null, openChat: null, openChatHandler: null, hide: 0};
const streamDone = () => {
  let consumed = false;
  const wire = new TextEncoder().encode('data: {"type": "done"}\n\n');
  return {ok: true, body: {getReader: () => ({read: async () => consumed ? {done: true} : (consumed = true, {done: false, value: wire}), releaseLock() {}})}};
};
const sandbox = {
  document: {listeners: {}, getElementById: $, createElement: tag => new Element(tag), createTextNode: text => { const e = new Element('#text'); e.textContent = text; return e; },
    querySelectorAll: () => [], addEventListener(type, fn) { this.listeners[type] = fn; }, body: new Element(), documentElement: new Element(),
    get activeElement() { return focusedElement; }},
  window: {
    listeners: {}, addEventListener(type, fn) { this.listeners[type] = fn; },
    kovaQuick: {
      hide: () => { quickCalls.hide += 1; },
      openChat: id => { quickCalls.openChat = id; },
      onSelection: cb => { quickCalls.selection = cb; },
      onOpenChat: cb => { quickCalls.openChatHandler = cb; },
    },
  },
  location: {hash: '', search: '?quick=1'},
  performance, URLSearchParams, TextDecoder, AbortController, console, FormData, File, innerWidth: 1200, innerHeight: 800,
  requestAnimationFrame: fn => { fn(); }, setTimeout() {}, clearTimeout() {}, setInterval() {},
  localStorage: {
    getItem: key => prefs.get(key) ?? (key === 'kova-theme' ? 'dark' : null),
    setItem: (key, value) => { prefs.set(key, String(value)); },
    removeItem: key => { prefs.delete(key); },
  },
  fetch: async (path, options = {}) => {
    const form = options.body instanceof FormData ? options.body : null;
    const body = !form && options.body ? JSON.parse(options.body) : null;
    requests.push({path, body});
    if (path === '/chats' && options.method === 'POST') {
      return {ok: true, json: async () => ({id: 'new-chat', title: body?.title || null, mode: body?.mode || 'work',
        permission: 'manual', cwd: '/project', web: true, messages: [], plan: [], ephemeral: !!body?.ephemeral,
        model: body?.model ?? null, reasoning_effort: body?.reasoning_effort ?? null})};
    }
    if (path === '/health') return {ok: true, json: async () => ({model: defaultModel, model_ok: true})};
    if (path.startsWith('/chats/new-chat/messages') && options.method === 'POST') return streamDone();
    if (path === '/chats/new-chat') return {ok: true, json: async () => ({id: 'new-chat', cwd: '/project', messages: []})};
    const payloads = { '/chats': [], '/projects': [], '/models': [], '/capabilities': {offered: {chat: [], work: []}},
      '/mcp': [], '/extensions/plugins': [], '/preferences': {} };
    return {ok: true, json: async () => (path in payloads ? payloads[path] : {ok: true})};
  },
};
let source = readFileSync(new URL('../src/kova/web_assets/app.js', import.meta.url), 'utf8');
source = source.replace('controls(); start();', 'globalThis.test = {state, createComposerChat, renderQuickSelection, renderQuickControls, send, QUICK_TITLE_PREFIX};');
vm.runInNewContext(readFileSync(new URL('../src/kova/web_assets/palette.js', import.meta.url), 'utf8'), sandbox);
vm.runInNewContext(readFileSync(new URL('../src/kova/web_assets/shortcuts.js', import.meta.url), 'utf8'), sandbox);
vm.runInNewContext(readFileSync(new URL('../src/kova/web_assets/chat_list.js', import.meta.url), 'utf8') + '\n' +
  readFileSync(new URL('../src/kova/web_assets/tool-cards.js', import.meta.url), 'utf8') + '\n' +
  readFileSync(new URL('../src/kova/web_assets/reasoning.js', import.meta.url), 'utf8') + '\n' +
  readFileSync(new URL('../src/kova/web_assets/drafts.js', import.meta.url), 'utf8') + '\n' +
  readFileSync(new URL('../src/kova/web_assets/projects.js', import.meta.url), 'utf8') + '\n' + source, sandbox);
const test = sandbox.test;
const tick = () => new Promise(resolve => setImmediate(resolve));

// The quick window carries the quick chrome marker and starts with its own
// actions hidden until there is something to open or copy.
assert.equal(sandbox.document.body.classList.contains('quick'), true, '?quick=1 marks the body');
assert.equal(typeof test.renderQuickSelection, 'function');
assert.equal(typeof test.createComposerChat, 'function');

// A quick ask chat is created with the [quick] title prefix (the same trick the
// CLI uses for [ask]), so it stays out of the normal sidebar history.
$('prompt').value = 'summarise this\nsecond line';
await test.createComposerChat();
assert.equal(requests.at(-1).path, '/chats');
assert.equal(requests.at(-1).body.title, '[quick] summarise this', 'the first line titles the chat');
assert.equal(requests.at(-1).body.mode, 'work');
console.log('Quick ask titles its chat with the [quick] prefix passed');

// The clipboard selection the shell hands over becomes a removable chip.
test.state.connected = true; test.state.busy = false; test.state.chat = null;
assert.equal(quickCalls.selection !== null, true, 'the page registers for selections');
quickCalls.selection('selected text');
assert.equal($('quick-selection').hidden, false, 'a selection shows the chip');
$('quick-selection-remove').onclick();
assert.equal($('quick-selection').hidden, true, 'removing the selection hides the chip');
console.log('Quick ask selection chip appears and is removable passed');

// Sending folds the selection into the outgoing message, never silently.
quickCalls.selection('the selected words');
$('prompt').value = 'explain this';
await test.send({preventDefault() {}});
await tick(); await tick();
const message = requests.find(r => r.path === '/chats/new-chat/messages');
assert.ok(message, 'the turn is posted');
assert.ok(message.body.text.includes('explain this'), message.body.text);
assert.ok(message.body.text.includes('<selection>\nthe selected words\n</selection>'),
  'the selection is delimited and included');
// The chat title uses the typed question, not the injected selection.
const created = [...requests].reverse().find(r => r.path === '/chats' && r.body?.title);
assert.equal(created.body.title, '[quick] explain this');
console.log('Quick ask includes the selection in the outgoing message passed');

// Copy uses the reply's own body and only says Copied once the clipboard write
// has actually succeeded. A failure keeps the neutral label and explains what
// to do instead, the same feedback the message actions use.
$('messages').replaceChildren();
const answer = new Element('article');
answer.className = 'message assistant';
const answerLabel = new Element('div'); answerLabel.className = 'message-label'; answerLabel.textContent = 'Kova';
const answerBody = new Element('div'); answerBody.className = 'message-body';
const answerMarkdown = new Element('div'); answerMarkdown.className = 'markdown'; answerMarkdown.textContent = 'The answer itself';
answerBody.append(answerMarkdown);
const answerActions = new Element('div'); answerActions.className = 'message-actions';
answerActions.textContent = 'Copied 1.2k in · 340 out';
answer.append(answerLabel, answerBody, answerActions);
$('messages').append(answer);
sandbox.document.querySelectorAll = selector => selector === '#messages .assistant'
  ? $('messages').children.filter(node => String(node.className || '').split(' ').includes('assistant')) : [];
test.renderQuickControls();
assert.equal($('quick-copy').hidden, false, 'an answer reveals Copy');

const copied = [];
let resolveWrite;
sandbox.navigator = {clipboard: {writeText: text => {
  copied.push(text);
  return new Promise(resolve => { resolveWrite = resolve; });
}}};
const pending = $('quick-copy').onclick();
assert.equal($('quick-copy').textContent, 'Copy', 'a write in flight does not claim Copied');
await tick();
assert.equal($('quick-copy').textContent, 'Copy', 'still Copy until the clipboard accepts it');
resolveWrite();
await pending;
assert.equal($('quick-copy').textContent, 'Copied', 'a successful write says Copied');
assert.deepEqual(copied, ['The answer itself'], 'only the reply body is copied, never the label or action bar');
console.log('Quick ask copies the reply body after the clipboard succeeds passed');

const toastsBefore = $('toasts').children.length;
sandbox.navigator = {clipboard: {writeText: async () => { throw new Error('denied'); }}};
await $('quick-copy').onclick();
assert.equal($('quick-copy').textContent, 'Copy', 'a rejected write never claims Copied');
assert.equal($('toasts').children.length, toastsBefore + 1, 'a rejected write explains the failure');
assert.match($('toasts').textContent, /Clipboard unavailable\. Select the text to copy it\./);

sandbox.navigator = {};
await $('quick-copy').onclick();
assert.equal($('quick-copy').textContent, 'Copy', 'a missing clipboard API never claims Copied');
assert.equal($('toasts').children.length, toastsBefore + 2, 'a missing clipboard API also explains the failure');
console.log('Quick ask reports clipboard failures without claiming Copied passed');
