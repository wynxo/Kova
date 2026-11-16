// Exercise the real Privacy panel with an offline DOM and API boundary.
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import vm from 'node:vm';
class Element {
  constructor(tag) { this.tagName = tag; this.children = []; this.value = ''; }
  append(...nodes) { this.children.push(...nodes); }
  replaceChildren(...nodes) { this.children = [...nodes]; }
  setAttribute() {}
  showModal() { this.open = true; }
  close() { this.open = false; }
  click() { this.clicked = true; }
}
const nodes = new Map();
const $ = id => { if (!nodes.has(id)) nodes.set(id, new Element('div')); return nodes.get(id); };
const body = new Element('body');
const state = {localOnly: false, chats: [{id: 'chat-demo', title: '<not markup>'}, {id: 'incognito-demo', ephemeral: true}]};
const report = {local_only: true, requests: 1, bytes_out: 10, bytes_in: 30,
  groups: [{day: '2030-01-01', host: '<script>not-markup</script>', provider: 'demo', kind: 'model', local: false, requests: 1, bytes_out: 10, bytes_in: 30, tokens_in: null, tokens_out: null}],
  items: [{created: '2030-01-01T00:00:00Z', host: 'example.com', kind: 'model', status: 'complete', bytes_out: 10, bytes_in: 30, chat_id: 'chat-demo'}]};
const calls = []; let fail = false, exported;
const context = vm.createContext({$, state, document: {body},
  el(tag, cls, text) { const node = new Element(tag); node.textContent = text; return node; },
  iconButton: () => new Element('button'), sidebar() {}, controls() {}, safe: fn => fn(), health: async () => {},
  URLSearchParams, JSON, Blob,
  URL: {createObjectURL(blob) { exported = blob; return 'blob:demo'; }, revokeObjectURL() {}},
  write: (method, value) => ({method, body: JSON.stringify(value)}),
  json: async (path, options) => {
    calls.push({path, options}); if (fail) throw Error('Unavailable');
    if (options?.method === 'PATCH') { report.local_only = JSON.parse(options.body).local_only; return {local_only: report.local_only}; }
    return structuredClone(report);
  }});
const source = readFileSync('src/kova/web_assets/app.js', 'utf8');
vm.runInContext(source.slice(source.indexOf('  (function privacyPanel()'), source.indexOf('  // Phones get a drawer;')), context);
await $('open-privacy').onclick();
const dialog = body.children[0], [, label, , filters, summary, list] = dialog.children;
const [toggle] = label.children, [days, chat, refresh, download] = filters.children;
assert.equal(dialog.open, true);
assert.equal(chat.children.length, 2, 'Incognito is never listed in persistent reports');
assert.equal(toggle.checked, true);
assert.equal($('local-only-status').hidden, false);
assert.match(summary.textContent, /10 body bytes/);
assert.equal(list.children[0].children[0].textContent, '2030-01-01 · <script>not-markup</script>');
toggle.checked = false;
await toggle.onchange();
assert.equal(state.localOnly, false, 'The checkbox must persist through the real JSON request contract');
assert.equal($('local-only-status').hidden, true);
chat.value = 'chat-demo'; days.value = '7'; await chat.onchange();
assert.match(calls.at(-1).path, /days=7&chat_id=chat-demo/);
download.onclick(); assert.equal(JSON.parse(await exported.text()).requests, 1);
fail = true; toggle.checked = true; await toggle.onchange();
assert.equal(toggle.checked, false, 'Failed saves restore the last confirmed policy');
assert.match(summary.textContent, /not saved/);
await refresh.onclick(); assert.equal(download.disabled, true);
assert.match(summary.textContent, /Could not load/);
