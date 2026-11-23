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
const walk = node => [node, ...node.children.flatMap(child => child.className === '#text' ? [] : walk(child))];
// Actions past copy live inside the "More" menu, so look anywhere in the bar.
const byClass = (node, cls) => walk(node).slice(1).find(child => child.className === cls || child.className?.split(' ').includes(cls));

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

// At rest a reply shows copy and one icon-only "More" button, nothing else (no
// second copy, no "Details" or "Plain text" words); the rest is inside More.
{
  const actions = bar({role: 'assistant', text: 'x', plainText: () => 'x', usage: {prompt_tokens: 10},
    model: 'demo', onRetry: () => {}, onBranch: () => {}});
  const visible = actions.children.filter(child => child.className !== 'message-action-note');
  assert.deepEqual(visible.map(child => child.className.split(' ')[0]), ['message-action-copy', 'message-details']);
  const more = byClass(actions, 'message-more-toggle');
  assert.equal(more.tagName, 'summary');
  assert.equal(more.getAttribute('aria-label'), 'More actions and details');
  assert.ok(!walk(more).some(node => node.className === '#text' && node.textContent.trim()), 'More has no text label');
  assert.ok(byClass(actions, 'message-action-copy-plain') && byClass(actions, 'message-action-retry'));
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

// Retry and edit alternatives (#188): a quiet stepper flips this turn only,
// with the first and last attempt disabled where there is nowhere to go.
{
  const chosen = [];
  const actions = bar({role: 'assistant', text: 'x',
    alternatives: {turn_id: 'turn-1', index: 2, count: 3},
    onAlternative: index => chosen.push(index)});
  const group = byClass(actions, 'message-alternatives');
  assert.equal(group.getAttribute('role'), 'group');
  assert.equal(group.getAttribute('aria-label'), 'Message alternatives');
  const previous = byClass(group, 'message-alternative-step');
  const next = group.children.filter(child => child.className === 'message-alternative-step').at(-1);
  assert.equal(previous.disabled, false);
  assert.equal(next.disabled, false);
  assert.equal(previous.getAttribute('aria-label'), 'Show previous alternative');
  assert.equal(next.getAttribute('aria-label'), 'Show next alternative');
  previous.onclick();
  next.onclick();
  assert.deepEqual(chosen, [1, 3]);
  assert.equal(byClass(group, 'message-alternative-count').textContent, '2 / 3');

  const first = bar({role: 'assistant', text: 'x',
    alternatives: {turn_id: 'turn-1', index: 1, count: 2}, onAlternative: () => {}});
  const firstGroup = byClass(first, 'message-alternatives');
  const [firstPrevious] = firstGroup.children.filter(child => child.className === 'message-alternative-step');
  const [lastNext] = firstGroup.children.filter(child => child.className === 'message-alternative-step').slice(-1);
  assert.equal(firstPrevious.disabled, true);
  assert.equal(lastNext.disabled, false);

  const last = bar({role: 'assistant', text: 'x',
    alternatives: {turn_id: 'turn-1', index: 2, count: 2}, onAlternative: () => {}});
  const lastGroup = byClass(last, 'message-alternatives');
  const [lastPrevious] = lastGroup.children.filter(child => child.className === 'message-alternative-step');
  const [lastNextAgain] = lastGroup.children.filter(child => child.className === 'message-alternative-step').slice(-1);
  assert.equal(lastPrevious.disabled, false);
  assert.equal(lastNextAgain.disabled, true);

  assert.equal(byClass(bar({role: 'assistant', text: 'x',
    alternatives: {turn_id: 'turn-1', index: 1, count: 1}, onAlternative: () => {}}), 'message-alternatives'), undefined);
  assert.equal(byClass(bar({role: 'assistant', text: 'x',
    alternatives: {turn_id: 'turn-1', index: 1, count: 2}}), 'message-alternatives'), undefined);
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
