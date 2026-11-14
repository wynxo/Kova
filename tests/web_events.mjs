// The /events stream: a dead or closed stream is noticed, reported honestly and
// reconnected with backoff (#93). Runs the real app.js with no browser or packages.
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import vm from 'node:vm';

class Element {
  constructor(tag = 'div') {
    this.tagName = tag; this.children = []; this.parentNode = null;
    this.className = ''; this.value = ''; this.disabled = false; this.hidden = false;
    this.listeners = {}; this.attrs = {}; this.dataset = {}; this.style = {};
    this.classList = {toggle: () => {}, contains: () => false};
  }
  set textContent(text) { this._text = text; this.children = []; }
  get textContent() { return (this._text || '') + this.children.map(n => n.textContent).join(''); }
  append(...nodes) { for (const n of nodes) { n.parentNode = this; this.children.push(n); } }
  replaceChildren(...nodes) { this._text = ''; this.children = []; this.append(...nodes); }
  insertBefore(node) { this.append(node); }
  remove() { if (this.parentNode) this.parentNode.children.splice(this.parentNode.children.indexOf(this), 1); }
  setAttribute(key, value) { this.attrs[key] = String(value); }
  getAttribute(key) { return this.attrs[key]; }
  addEventListener(type, fn) { this.listeners[type] = fn; }
  querySelectorAll() { return []; }
  focus() {} showModal() {} close() {}
  get lastElementChild() { return this.children.at(-1); }
}
const elements = new Map();
const $ = id => { if (!elements.has(id)) elements.set(id, new Element()); return elements.get(id); };
for (const id of ['connection', 'theme', 'web']) $(id).append(new Element('span'));
$('mode').value = 'work';
new Element().append($('connection'));

class FakeEventSource {
  static CONNECTING = 0; static OPEN = 1; static CLOSED = 2;
  constructor(url) { this.url = url; this.readyState = 0; this.listeners = {}; this.closed = false; sources.push(this); }
  addEventListener(type, fn) { this.listeners[type] = fn; }
  emit(type, data) { if (type === 'open') this.readyState = 1; this.listeners[type]?.({data: JSON.stringify(data ?? {})}); }
  fail(readyState) { this.readyState = readyState; this.onerror?.(); }
  close() { this.closed = true; this.readyState = 2; }
}
const sources = [], timers = [], intervals = [], requests = [];
let clock = 1_000_000, chatsStatus = 200, unreachable = false;
class FakeDate extends Date { static now() { return clock; } }
const sandbox = {
  document: {listeners: {}, getElementById: $, createElement: tag => new Element(tag),
    createTextNode: text => { const e = new Element('#text'); e.textContent = text; return e; },
    querySelectorAll: () => [], addEventListener(type, fn) { this.listeners[type] = fn; },
    body: new Element(), documentElement: new Element(), visibilityState: 'visible', title: ''},
  window: {listeners: {}, addEventListener(type, fn) { this.listeners[type] = fn; }, history: {replaceState() {}}},
  location: {hash: '', pathname: '/app', search: ''},
  EventSource: FakeEventSource, Date: FakeDate,
  performance, URLSearchParams, TextDecoder, console, innerHeight: 800, innerWidth: 1200,
  requestAnimationFrame: fn => fn(),
  setTimeout: (fn, delay) => { timers.push({fn, delay}); return timers.length; }, clearTimeout() {},
  setInterval: fn => { intervals.push(fn); },
  localStorage: {getItem() { return 'dark'; }, setItem() {}},
  fetch: async (path, options = {}) => {
    requests.push(path);
    if (unreachable) throw new TypeError('Failed to fetch');
    if (path === '/chats' && chatsStatus !== 200) return {ok: false, status: chatsStatus, json: async () => ({detail: 'Open kova-web to connect this window.'})};
    const payloads = {
      '/chats': [], '/persona': {name: 'Kova', configured: true},
      '/models': [{name: 'local', model: 'qwen'}], '/health': {model: 'local', model_ok: true},
      '/capabilities': {offered: {work: ['approvals', 'tools']}},
    };
    return {ok: true, status: 200, json: async () => payloads[path] ?? {ok: true}};
  },
};
sandbox.window.document = sandbox.document;
vm.runInNewContext(readFileSync(new URL('../src/kova/web_assets/app.js', import.meta.url), 'utf8'), sandbox);
const tick = async () => { for (let i = 0; i < 20; i++) await new Promise(resolve => setImmediate(resolve)); };
const pill = () => $('connection').lastElementChild.textContent;
const runTimers = async () => { const due = timers.splice(0); for (const t of due) t.fn(); await tick(); };
const reconnectTimers = () => timers.filter(t => t.delay >= 1000 && t.delay <= 30000);

await tick();
assert.equal(sources.length, 1, 'start() opens the live stream');
assert.equal(sources[0].url, '/events');
assert.equal(pill(), 'Model connected');
sources[0].emit('open'); sources[0].emit('ping');

// A reminder arrives as a toast.
sources[0].emit('reminder', {text: 'Stretch'});
assert.ok($('toasts').textContent.includes('Stretch'));

// 1. Network blip: the browser retries by itself (CONNECTING); we only say so, then resync.
sources[0].fail(FakeEventSource.CONNECTING);
assert.equal(pill(), 'Reconnecting…');
assert.ok($('banner').textContent.includes('Reconnecting'));
assert.equal(reconnectTimers().length, 0, 'no second connection while the browser retries');
// The 30 s health poll must not paint "Model connected" over a dead stream.
for (const fn of intervals) fn(); await tick();
assert.equal(pill(), 'Reconnecting…');
const before = requests.length;
sources[0].emit('open'); await tick();
assert.ok(requests.slice(before).includes('/chats'), 'reconnecting resyncs the chat list');
assert.equal(pill(), 'Model connected');
assert.equal($('banner').hidden, true);
timers.length = 0;

// 2. Server unreachable after the stream closed: retry with growing backoff.
unreachable = true;
sources[0].fail(FakeEventSource.CLOSED);
assert.deepEqual(reconnectTimers().map(t => t.delay), [1000]);
await runTimers();
assert.deepEqual(reconnectTimers().map(t => t.delay), [2000]);
await runTimers();
assert.deepEqual(reconnectTimers().map(t => t.delay), [4000]);
assert.equal(sources.length, 1, 'no stream is opened while the server is down');
unreachable = false;
await runTimers();
assert.equal(sources.length, 2, 'the stream reopens once the server answers');
sources[1].emit('open'); await tick();
assert.equal(pill(), 'Model connected');
timers.length = 0;

// 3. A stream that stops pinging (a phone that slept) is replaced.
sources[1].emit('ping');
clock += 50_000;
sandbox.document.listeners.visibilitychange(); await tick();
assert.equal(pill(), 'Reconnecting…');
assert.ok(sources[1].closed);
await runTimers();
assert.equal(sources.length, 3);
sources[2].emit('open'); sources[2].emit('ping'); await tick();
timers.length = 0;

// 4. Server restarted: the stream gets a 401 and closes, and the session is gone.
chatsStatus = 401;
sources[2].fail(FakeEventSource.CLOSED);
await runTimers();
assert.equal(pill(), 'Disconnected');
assert.ok($('banner').textContent.includes('Kova restarted'), $('banner').textContent);
assert.equal(sources.length, 3, 'a signed-out window does not keep reconnecting');
await runTimers();
assert.equal(sources.length, 3);
console.log('Live event stream reconnect, backoff, staleness and restart passed');
