// Phone pairing in the web UI (#66): the computer shows a QR code, the phone
// claims it from the URL fragment. Runs the real app.js with no browser.
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import vm from 'node:vm';

class Element {
  constructor(tag = 'div') {
    this.tagName = tag; this.children = []; this.parentNode = null;
    this.className = ''; this.value = ''; this.disabled = false; this.hidden = false; this.title = '';
    this.listeners = {}; this.attrs = {}; this.dataset = {}; this.style = {};
    this.classList = {toggle: (name, force) => { if (force) this.className += ` ${name}`; }, contains: () => false};
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
  focus() {} close() { this.open = false; this.listeners.close?.(); }
  showModal() { this.open = true; }
  get lastElementChild() { return this.children.at(-1); }
}
const walk = node => [node, ...node.children.flatMap(walk)];
const tick = async () => { for (let i = 0; i < 30; i++) await new Promise(resolve => setImmediate(resolve)); };

function load({hostname, hash = '', routes}) {
  const elements = new Map(), requests = [], timers = [], body = new Element('body'), note = new Element();
  const $ = id => { if (!elements.has(id)) elements.set(id, new Element()); return elements.get(id); };
  for (const id of ['connection', 'theme', 'web']) $(id).append(new Element('span'));
  $('mode').value = 'work';
  new Element().append($('connection'));
  const sandbox = {
    document: {listeners: {}, getElementById: $, createElement: tag => new Element(tag),
      createTextNode: text => { const e = new Element('#text'); e.textContent = text; return e; },
      querySelectorAll: () => [], querySelector: selector => (selector === '.local-note div' ? note : null),
      addEventListener() {}, body, documentElement: new Element(), title: ''},
    window: {addEventListener() {}, confirm: () => true, history: {replaceState() {}}},
    location: {hash, hostname, pathname: '/app', search: ''},
    EventSource: class { addEventListener() {} close() {} },
    navigator: {clipboard: {writeText: async () => {}}},
    performance, URLSearchParams, TextDecoder, console, innerHeight: 800, innerWidth: 1200,
    requestAnimationFrame: fn => fn(), setTimeout: fn => { timers.push(fn); return timers.length; }, clearTimeout() {}, setInterval() {},
    localStorage: {getItem() { return 'dark'; }, setItem() {}},
    fetch: async (path, options = {}) => {
      const method = options.method || 'GET', payload = options.body ? JSON.parse(options.body) : null;
      requests.push({method, path, body: payload});
      const base = {'/chats': [], '/persona': {name: 'Kova', configured: true}, '/models': [{name: 'local', model: 'qwen'}],
                    '/health': {model: 'local', model_ok: true}, '/capabilities': {offered: {work: []}}};
      const handler = routes[`${method} ${path}`];
      const value = handler ? handler(payload) : base[path] ?? {ok: true};
      if (value?.ok === false && value.status) return value;
      return {ok: true, status: 200, json: async () => value};
    },
  };
  sandbox.window.location = sandbox.location;
  vm.runInNewContext(readFileSync(new URL('../src/kova/web_assets/app.js', import.meta.url), 'utf8'), sandbox);
  return {$, requests, body, note, sandbox, timers};
}

// ---------------------------------------------------------- on the computer
{
  let enabled = false, claimed = false, devices = [];
  const phone = () => ({enabled, listening: enabled, address: enabled ? 'http://192.168.1.20:7700' : null, error: null, devices});
  const page = load({hostname: '127.0.0.1', routes: {
    'GET /phone': phone,
    'POST /phone': body => { enabled = body.enabled; return phone(); },
    'POST /phone/pair': () => ({id: 'c1', url: 'http://192.168.1.20:7700/app#pair=SECRET', address: 'http://192.168.1.20:7700',
                                expires_in: 120, qr: 'data:image/svg+xml;base64,AAAA'}),
    'GET /phone/pair/c1': () => (claimed ? {status: 'paired', device: {name: 'iPhone'}} : {status: 'waiting', device: null}),
    'DELETE /devices/d1': () => { devices = []; return {ok: true, removed: 1}; },
  }});
  await tick();
  const {$, requests, body} = page;
  assert.equal($('connect-phone').hidden, false, 'the computer offers Connect your phone');
  $('connect-phone').onclick(); await tick();
  const dialog = body.children.find(node => node.className.includes('phone-dialog'));
  assert.ok(dialog.open);
  assert.ok(dialog.textContent.includes('never the internet'), 'phone access is explained before it is turned on');
  const turnOn = walk(dialog).find(n => n.tagName === 'button' && n.textContent === 'Turn on phone access');
  turnOn.onclick(); await tick();
  assert.ok(requests.some(r => r.method === 'POST' && r.path === '/phone' && r.body.enabled === true));
  const qr = walk(dialog).find(n => n.tagName === 'img' && n.className === 'phone-code');
  assert.equal(qr.src, 'data:image/svg+xml;base64,AAAA');
  assert.ok(dialog.textContent.includes('Waiting for your phone… 2:00'));
  assert.ok(dialog.textContent.includes('http://192.168.1.20:7700'));
  assert.ok(!dialog.textContent.includes('SECRET'), 'the one-use secret is only inside the QR code');
  // Closing the dialog cancels a code nobody used.
  dialog.close(); await tick();
  assert.ok(requests.some(r => r.method === 'DELETE' && r.path === '/phone/pair/c1'), 'closing cancels a waiting code');
  $('connect-phone').onclick(); await tick();
  assert.ok(dialog.textContent.includes('Waiting for your phone'));
  // The phone claims the code; the next poll shows it.
  claimed = true; devices = [{id: 'd1', name: 'iPhone', created: '2026-11-14T10:00:00+00:00', last_seen: '2026-11-14T10:00:00+00:00'}];
  for (const fn of page.timers.splice(0)) fn();
  await tick();
  assert.ok(dialog.textContent.includes('Paired iPhone'), dialog.textContent);
  assert.ok(dialog.textContent.includes('Paired devices'));
  const unpair = walk(dialog).find(n => n.tagName === 'button' && n.attrs['aria-label'] === 'Unpair iPhone');
  unpair.onclick(); await tick();
  assert.ok(requests.some(r => r.method === 'DELETE' && r.path === '/devices/d1'));
  const off = walk(dialog).find(n => n.tagName === 'button' && n.textContent === 'Turn off phone access');
  off.onclick(); await tick();
  assert.ok(requests.some(r => r.method === 'POST' && r.path === '/phone' && r.body.enabled === false));
  console.log('Connect your phone dialog passed');
}

// -------------------------------------------------------------- on the phone
{
  const page = load({hostname: '192.168.1.20', hash: '#pair=SECRET', routes: {}});
  await tick();
  const {$, requests, note} = page;
  const claim = requests.find(r => r.path === '/app/pair');
  assert.deepEqual(claim, {method: 'POST', path: '/app/pair', body: {secret: 'SECRET'}});
  assert.ok(requests.indexOf(claim) < requests.findIndex(r => r.path === '/chats'), 'pair before loading chats');
  assert.equal($('connect-phone').hidden, true, 'a phone cannot pair other phones');
  assert.equal($('disconnect').title, 'Unpair this phone');
  assert.ok(note.textContent.includes('Connected over Wi-Fi'));
  assert.ok($('toasts').textContent.includes('now paired'));
  console.log('Phone claims its pairing code passed');
}

// A used or expired code says what to do next.
{
  const page = load({hostname: '192.168.1.20', hash: '#pair=OLD', routes: {
    'POST /app/pair': () => ({ok: false, status: 401, json: async () => ({detail: 'This pairing code expired or was already used. Make a new one on your computer and scan it again.'})}),
  }});
  await tick();
  assert.ok(page.$('banner').textContent.includes('expired or was already used'), page.$('banner').textContent);
  assert.equal(page.$('connection').lastElementChild.textContent, 'Disconnected');
  console.log('Expired pairing code message passed');
}
