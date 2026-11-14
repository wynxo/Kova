// Execute the real worker against a small Cache API stand-in.
import vm from 'node:vm';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
const listeners = {};
const stores = new Map([['kova-shell-v1', new Map()], ['kova-share-v1', new Map()]]);
const shell = stores.get('kova-shell-v1');
let online = true, focused = false, notices = [];
const keyFor = (name, entry) => {
  const raw = typeof entry === 'string' ? entry : entry.url;
  const url = new URL(raw, 'https://kova.test');
  return name === 'kova-shell-v1' ? url.pathname : url.href;
};
const makeCache = name => {
  if (!stores.has(name)) stores.set(name, new Map());
  const map = stores.get(name);
  return {
    addAll: async paths => paths.forEach(path => map.set(keyFor(name, path), {path})),
    put: async (key, value) => map.set(keyFor(name, key), value),
    match: async key => map.get(keyFor(name, key)),
    delete: async key => map.delete(keyFor(name, key)),
    keys: async () => [...map.keys()].map(key => ({url: new URL(key, 'https://kova.test').href})),
  };
};
const caches = {
  open: async name => makeCache(name),
  keys: async () => [...stores.keys()],
  delete: async name => stores.delete(name),
  match: async request => shell.get(new URL(request.url).pathname),
};
vm.runInNewContext(readFileSync(new URL('../src/kova/web_assets/sw.js', import.meta.url), 'utf8'), {
  self: {location: {origin: 'https://kova.test'}, addEventListener: (name, fn) => {listeners[name] = fn;}, registration: {showNotification: async (...args) => notices.push(args)}, clients: {claim: async () => {}, matchAll: async () => [{focused}]}},
  caches, URL, Response, FormData, crypto: globalThis.crypto,
  fetch: async request => { if (!online) throw Error('offline'); return {url: request.url}; },
});
let pending;
listeners.install({waitUntil: promise => {pending = promise;}}); await pending;
assert.ok(shell.has('/app'));
for (const path of shell.keys()) assert.ok(path === '/app' || path.startsWith('/app/assets/'));
online = false;
for (const path of ['/app', '/app/assets/app.js', '/app/assets/app.css']) {
  listeners.fetch({request: {method: 'GET', url: 'https://kova.test' + path}, respondWith: promise => {pending = promise;}});
  assert.equal((await pending).path, path);
}
for (const path of ['/chats', '/events', '/persona', '/app/share/token', '/app/login', '/app/assets/app.js?secret=x']) {
  let intercepted = false;
  listeners.fetch({request: {method: 'GET', url: 'https://kova.test' + path}, respondWith() {intercepted = true;}});
  assert.equal(intercepted, false, path);
}
console.log('Worker install, offline shell and API cache exclusion passed');

// A share-sheet POST is staged locally, not turned into a base64 text draft.
const form = new FormData();
form.append('title', 'Title');
form.append('text', 'hello');
form.append('url', 'https://example.com');
form.append('files', new File(['hello'], 'note.txt', {type: 'text/plain'}));
listeners.fetch({request: {method: 'POST', url: 'https://kova.test/app/share', formData: async () => form},
                respondWith: promise => {pending = promise;}});
let response = await pending;
assert.equal(response.status, 303);
const shareURL = new URL(response.headers.get('location'));
const token = new URLSearchParams(shareURL.hash.slice(1)).get('share');
assert.ok(token, 'the share fragment carries a one-use token');
const shares = stores.get('kova-share-v1');
const root = `https://kova.test/app/share/${token}`;
let manifest = JSON.parse(await (await shares.get(root + '/manifest')).text());
assert.equal(manifest.text, 'Title\n\nhello\n\nhttps://example.com');
assert.equal(manifest.files.length, 1);
assert.equal(manifest.files[0].name, 'note.txt');
assert.equal(await (await shares.get(root + '/file/0')).text(), 'hello');
assert.ok(!JSON.stringify(manifest).includes('aGVsbG8='));

// Text/link-only shares keep working and stage no files.
const textForm = new FormData();
textForm.append('text', 'Only a link');
textForm.append('url', 'https://example.com');
listeners.fetch({request: {method: 'POST', url: 'https://kova.test/app/share', formData: async () => textForm},
                respondWith: promise => {pending = promise;}});
response = await pending;
assert.equal(response.status, 303);
const textToken = new URLSearchParams(new URL(response.headers.get('location')).hash.slice(1)).get('share');
manifest = JSON.parse(await (await shares.get(`https://kova.test/app/share/${textToken}/manifest`)).text());
assert.equal(manifest.text, 'Only a link\n\nhttps://example.com');
assert.deepEqual(manifest.files, []);

// The worker enforces the same total size and per-turn file count limits.
const before = shares.size;
const big = new FormData();
big.append('files', new File([new Uint8Array(2 * 1024 * 1024 + 1)], 'big.bin'));
listeners.fetch({request: {method: 'POST', url: 'https://kova.test/app/share', formData: async () => big},
                respondWith: promise => {pending = promise;}});
response = await pending;
assert.equal(response.status, 413);
assert.equal((await response.text()).includes('2 MB'), true);
const many = new FormData();
for (let i = 0; i < 4; i += 1) many.append('files', new File(['x'], `note${i}.txt`));
listeners.fetch({request: {method: 'POST', url: 'https://kova.test/app/share', formData: async () => many},
                respondWith: promise => {pending = promise;}});
response = await pending;
assert.equal(response.status, 413);
assert.equal(shares.size, before, 'rejected shares never enter the cache');
console.log('Worker share staging, text/link shares and limits passed');

listeners.message({data: {type: 'reminder', text: 'Check kettle', id: 1}, waitUntil: promise => {pending = promise;}}); await pending;
assert.equal(notices[0][1].body, 'Check kettle');
focused = true;
listeners.message({data: {type: 'reminder', text: 'Check kettle', id: 1}, waitUntil: promise => {pending = promise;}}); await pending;
assert.equal(notices.length, 1, 'focused pages suppress OS notifications');
console.log('Existing reminder shape and focused-client suppression passed');
