// Custom CSS (#178): the preference gate, the one appended link and safe mode.
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import vm from 'node:vm';

const SOURCE = readFileSync(new URL('../src/kova/web_assets/custom_css.js', import.meta.url), 'utf8');
const tick = () => new Promise(resolve => setImmediate(resolve));
const settle = async () => { for (let i = 0; i < 6; i += 1) await tick(); };

class Node {
  constructor(tag) {
    this.tagName = String(tag).toUpperCase();
    this.children = [];
    this.parentNode = null;
    this.attrs = {};
    this.listeners = {};
  }
  setAttribute(key, value) { this.attrs[key] = String(value); }
  addEventListener(type, fn) { (this.listeners[type] ||= []).push(fn); }
  appendChild(node) { node.parentNode = this; this.children.push(node); return node; }
  remove() {
    if (!this.parentNode) return;
    const at = this.parentNode.children.indexOf(this);
    if (at >= 0) this.parentNode.children.splice(at, 1);
    this.parentNode = null;
  }
}

function boot({search = '', server = {custom_css: 'off'}, status = 200} = {}) {
  const head = new Node('head');
  head.appendChild(new Node('link'));
  const ids = new Map();
  const calls = [];
  const events = [];
  const document = {
    head,
    documentElement: new Node('html'),
    createElement: tag => new Node(tag),
    getElementById: id => ids.get(id) || null,
    dispatchEvent: event => events.push(event.detail),
  };
  // A real browser indexes by id; mirror the one attribute the module uses.
  const setAttribute = Node.prototype.setAttribute;
  Node.prototype.setAttribute = function (key, value) {
    setAttribute.call(this, key, value);
    if (key === 'id') ids.set(value, this);
  };
  const fetch = (url, options = {}) => {
    const method = options.method || 'GET';
    calls.push({url, method, body: options.body ? JSON.parse(options.body) : null});
    if (method === 'PUT') return Promise.resolve({ok: true, json: async () => server});
    return Promise.resolve({ok: status === 200, status,
      json: async () => (status === 200 ? server : {detail: 'signed out'})});
  };
  const sandbox = {
    document,
    location: {search},
    URLSearchParams,
    CustomEvent: class { constructor(type, init) { this.type = type; this.detail = init.detail; } },
    fetch,
    window: {},
  };
  vm.runInNewContext(SOURCE, sandbox);
  const api = vm.runInNewContext('window.KovaCustomCss', sandbox);
  return {api, head, calls, events, sandbox,
    link: () => head.children.find(node => node.attrs.id === 'custom-css') || null,
    puts: () => calls.filter(call => call.method === 'PUT').map(call => call.body)};
}

// Off by default: the preference is off, so nothing is fetched or appended.
{
  const page = boot({server: {custom_css: 'off'}});
  await settle();
  assert.equal(page.api.enabled(), false);
  assert.equal(page.api.loaded(), false);
  assert.equal(page.link(), null, 'off loads no stylesheet');
}

// On: one link, appended after the packaged sheets in <head>.
{
  const page = boot({server: {custom_css: 'on'}});
  await settle();
  assert.equal(page.api.enabled(), true);
  assert.equal(page.api.loaded(), true);
  const link = page.link();
  assert.ok(link, 'on appends /app/custom.css');
  assert.equal(link.attrs.href, '/app/custom.css');
  assert.equal(link.attrs.rel, 'stylesheet');
  assert.equal(page.head.children[page.head.children.length - 1], link,
    'custom.css comes last so it can override a token');
  assert.equal(page.head.children.filter(node => node.attrs.id === 'custom-css').length, 1,
    'sync never appends a second copy');
}

// Safe mode skips the sheet even while the setting is on.
{
  const page = boot({search: '?safe=1', server: {custom_css: 'on'}});
  await settle();
  assert.equal(page.api.safe(), true);
  assert.equal(page.api.enabled(), true, 'the saved choice is still shown');
  assert.equal(page.link(), null, 'safe mode loads no user stylesheet');
}

// The switch saves the shared preference and adds or removes the link at once.
{
  const page = boot({server: {custom_css: 'on'}});
  await settle();
  page.api.set(false);
  assert.deepEqual(page.puts(), [{custom_css: 'off'}]);
  assert.equal(page.api.loaded(), false);
  assert.equal(page.link(), null);

  page.api.set(true);
  assert.deepEqual(page.puts(), [{custom_css: 'off'}, {custom_css: 'on'}]);
  assert.ok(page.link(), 'turning it back on loads it again');
  assert.equal(page.head.children[page.head.children.length - 1], page.link());
}

// A 401 before login changes nothing; app.js syncs again after signing in.
{
  const page = boot({server: {custom_css: 'on'}, status: 401});
  await settle();
  assert.equal(page.api.enabled(), false, 'a signed-out page keeps the current state');
  assert.equal(page.link(), null);
}

console.log('web_custom_css.mjs: ok');
