// Execute the real worker against a small Cache API stand-in.
import vm from 'node:vm';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
const listeners = {}, stored = new Map();
let online = true, focused = false, notices = [];
const caches = {
  open: async () => ({addAll: async paths => paths.forEach(path => stored.set(path, {path}))}),
  keys: async () => ['kova-shell-v1'], delete: async () => true,
  match: async request => stored.get(new URL(request.url).pathname),
};
vm.runInNewContext(readFileSync(new URL('../src/kova/web_assets/sw.js', import.meta.url), 'utf8'), {
  self: {location: {origin: 'https://kova.test'}, addEventListener: (name, fn) => {listeners[name] = fn;}, registration: {showNotification: async (...args) => notices.push(args)}, clients: {claim: async () => {}, matchAll: async () => [{focused}]}},
  caches, URL, Response, fetch: async request => { if (!online) throw Error('offline'); return {url: request.url}; },
});
let pending;
listeners.install({waitUntil: promise => {pending = promise;}}); await pending;
assert.ok(stored.has('/app'));
for (const path of stored.keys()) assert.ok(path === '/app' || path.startsWith('/app/assets/'));
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

listeners.message({data: {type: 'reminder', text: 'Check kettle', id: 1}, waitUntil: promise => {pending = promise;}}); await pending;
assert.equal(notices[0][1].body, 'Check kettle');
focused = true;
listeners.message({data: {type: 'reminder', text: 'Check kettle', id: 1}, waitUntil: promise => {pending = promise;}}); await pending;
assert.equal(notices.length, 1, 'focused pages suppress OS notifications');
console.log('Existing reminder shape and focused-client suppression passed');
