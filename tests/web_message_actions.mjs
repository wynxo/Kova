// Message actions (#188): copy reports Copied, and details show only the
// usage the server sent. Runs the real module with no browser or packages.
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import vm from 'node:vm';

class Element {
  constructor(tag = 'div') {
    this.tagName = tag; this.children = []; this.parentNode = null;
    this.className = ''; this.listeners = {}; this.attrs = {}; this.open = false;
  }
  set textContent(text) { this._text = text; this.children = []; }
  get textContent() { return (this._text || '') + this.children.map(n => n.textContent).join(''); }
  append(...nodes) { for (const n of nodes) { if (n && typeof n === 'object') n.parentNode = this; this.children.push(n); } }
  setAttribute(key, value) { this.attrs[key] = String(value); }
  getAttribute(key) { return this.attrs[key]; }
  addEventListener(type, fn) { this.listeners[type] = fn; }
  focus() { focused = this; }
  get lastElementChild() { return this.children.at(-1); }
}
let focused = null;
const written = [];
const sandbox = {
  document: {
    createElement: tag => new Element(tag),
    createTextNode: text => { const e = new Element('#text'); e.textContent = text; return e; },
    addEventListener() {},
    querySelectorAll: () => [],
  },
  navigator: {clipboard: {writeText: async text => { written.push(text); }}},
  setTimeout: fn => { timers.push(fn); return timers.length; },
  clearTimeout() {},
  console,
};
const timers = [];
sandbox.globalThis = sandbox;
sandbox.window = sandbox;
vm.runInNewContext(readFileSync(new URL('../src/kova/web_assets/message-actions.js', import.meta.url), 'utf8'), sandbox);
const {bar, detailRows} = sandbox.KovaMessageActions;
const byClass = (node, cls) => node.children.find(child => child.className === cls || child.className?.split(' ').includes(cls));
const walk = node => [node, ...node.children.flatMap(child => child.className === '#text' ? [] : walk(child))];

// Copy keeps the Markdown the reader actually has, then says Copied briefly.
{
  const note = [];
  const actions = bar({role: 'assistant', text: () => '# Title\n\nBody', usage: null,
                       onError: message => note.push(message)});
  const copy = byClass(actions, 'message-action-copy');
  assert.equal(copy.getAttribute('aria-label'), 'Copy as Markdown');
  await copy.onclick();
  assert.deepEqual(written, ['# Title\n\nBody']);
  assert.equal(copy.getAttribute('aria-label'), 'Copied');
  const status = byClass(actions, 'message-action-note');
  assert.equal(status.getAttribute('role'), 'status');
  assert.equal(status.textContent, 'Copied');
  timers.splice(0).forEach(fn => fn());
  assert.equal(status.textContent, '');
  assert.equal(copy.getAttribute('aria-label'), 'Copy as Markdown');
}

// A failed clipboard write says what to do; it never claims Copied.
{
  const errors = [];
  sandbox.navigator.clipboard = {};
  const actions = bar({role: 'user', text: 'mine', onError: message => errors.push(message)});
  await byClass(actions, 'message-action-copy').onclick();
  assert.equal(errors.length, 1);
  assert.match(errors[0], /Select the text/);
  assert.equal(byClass(actions, 'message-action-note').textContent, '');
}

// Details read model, tokens, time and tool count from the usage sent.
{
  const actions = bar({role: 'assistant', text: 'ok', toolCount: 3,
    usage: {model: 'local-model', prompt_tokens: 1200, completion_tokens: 340, estimated: true,
            ms_first_token: 800, ms_total: 4200, tokens_per_second: 38, local: true}});
  const card = byClass(actions, 'message-details');
  const values = walk(card).map(node => node.textContent);
  assert.ok(values.includes('Model') && values.includes('local-model'));
  assert.ok(values.includes('Tokens') && values.includes('~1.2k in · ~340 out'));
  assert.ok(values.includes('Time') && values.includes('first token 0.8 s · 4.2 s total'));
  assert.ok(values.includes('Tools') && values.includes('3'));
  assert.ok(values.includes('Speed') && values.includes('38 tok/s'));
  assert.ok(values.includes('Note'));
  // The disclosure is native: its summary is the keyboard target.
  assert.equal(byClass(card, 'message-details-toggle').tagName, 'summary');
}

// No usage and no tools means no empty Details button, but copy still works.
{
  const actions = bar({role: 'assistant', text: 'plain'});
  assert.equal(byClass(actions, 'message-details'), undefined);
  assert.ok(byClass(actions, 'message-action-copy'));
}

// Retry and edit are offered only when app.js says they are possible.
{
  let retried = 0, edited = 0;
  const assistant = bar({role: 'assistant', text: 'x', onRetry: () => { retried += 1; }});
  const user = bar({role: 'user', text: 'y', onEdit: () => { edited += 1; }});
  byClass(assistant, 'message-action-retry').onclick();
  byClass(user, 'message-action-edit').onclick();
  assert.equal(retried, 1);
  assert.equal(edited, 1);
  assert.equal(bar({role: 'assistant', text: 'x'}).children.filter(n => n.className === 'message-action-retry message-action').length, 0);
}

assert.equal(detailRows({usage: null, model: '', toolCount: null}).length, 0);
const partialUsage = detailRows({usage: {completion_tokens: 17, cost_usd: 0}});
assert.deepEqual(Array.from(partialUsage, row => Array.from(row)),
  [['Tokens', '17 out'], ['Cost', '~$0.00']]);

console.log('message actions: ok');

{
  const actions = bar({role: 'assistant', text: '**hello**', plainText: () => 'hello',
    usage: {provider: 'Demo provider'}});
  sandbox.navigator.clipboard = {writeText: async text => written.push(text)};
  const plain = byClass(actions, 'message-action-copy-plain');
  assert.equal(plain.getAttribute('aria-label'), 'Copy as plain text');
  await plain.onclick();
  assert.equal(written.at(-1), 'hello');
  assert.equal(sandbox.KovaMessageActions.timestamp('2026-01-02T03:04:05Z').getAttribute('datetime'),
    '2026-01-02T03:04:05.000Z');
  assert.ok(detailRows({usage: {provider: 'Demo provider'}}).some(([key, value]) =>
    key === 'Provider' && value === 'Demo provider'));
}
