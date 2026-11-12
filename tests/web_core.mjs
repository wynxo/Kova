// DOM-level behavior checks, with no browser/CDN/Node package dependencies.
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import vm from 'node:vm';

class Element {
  constructor(tag = 'div') {
    this.tagName = tag; this.children = []; this.parentNode = null;
    this.className = ''; this.value = ''; this.disabled = false; this.hidden = false;
    this.attrs = {}; this.dataset = {}; this.style = {};
    this.classList = {toggle: (name, force) => {
      const names = new Set(this.className.split(' ').filter(Boolean));
      const add = force ?? !names.has(name); if (add) names.add(name); else names.delete(name);
      this.className = [...names].join(' ');
    }, contains: name => this.className.split(' ').includes(name)};
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
  addEventListener() {}
  querySelectorAll() { return []; }
  focus() {}
  get lastElementChild() { return this.children.at(-1); }
}
const elements = new Map();
function $(id) { if (!elements.has(id)) elements.set(id, new Element()); return elements.get(id); }
for (const id of ['connection', 'theme', 'web']) $(id).append(new Element('span'));
$('mode').value = 'work';
const requests = [];
const sandbox = {
  document: {getElementById: $, createElement: tag => new Element(tag), createTextNode: text => { const e = new Element('#text'); e.textContent = text; return e; },
    querySelectorAll: () => [], addEventListener() {}, body: new Element(), documentElement: new Element()},
  window: {addEventListener() {}}, location: {hash: ''},
  performance, URLSearchParams, TextDecoder, console,
  requestAnimationFrame: fn => { fn(); }, setTimeout() {}, setInterval() {},
  localStorage: {getItem() {return 'dark';}, setItem() {}},
  fetch: async (path, options) => {
    requests.push({path, body: options.body ? JSON.parse(options.body) : null});
    return {ok: true, json: async () => path === '/chats' ? [] : {ok: true}};
  },
};
let source = readFileSync(new URL('../src/kova/web_assets/app.js', import.meta.url), 'utf8');
source = source.replace('controls(); start();', 'globalThis.test = {state, controls, turnView, setChat};');
vm.runInNewContext(source, sandbox);
const {state, controls, turnView, setChat} = sandbox.test;
const walk = node => [node, ...node.children.flatMap(walk)];
const byClass = (node, cls) => walk(node).filter(n => n.className.split(' ').includes(cls));
const buttons = node => walk(node).filter(n => n.tagName === 'button');
const tick = () => new Promise(resolve => setImmediate(resolve));
state.connected = true; state.chat = {id: 'chat-1', cwd: '/project'};
controls(); assert.equal($('permission').disabled, false);
$('mode').value = 'chat'; controls(); assert.equal($('cwd').disabled, true);
$('mode').value = 'work'; state.busy = true; controls(); assert.equal($('permission').disabled, true);
state.busy = false;

for (const decision of ['allow', 'allow_session', 'deny']) {
  const body = new Element(), view = turnView(body);
  view.accept({type: 'approval', id: 'approval-1', tool: 'run_command', args: {command: '<script>literal command</script>'}, risk: 'normal', summary: 'short summary'});
  const card = byClass(body, 'approval-card')[0];
  assert.ok(card.textContent.includes('<script>literal command</script>'));
  assert.ok(card.textContent.includes('/project'));
  const input = walk(card).find(n => n.tagName === 'input'); input.value = 'Read instead';
  buttons(card)[['allow', 'allow_session', 'deny'].indexOf(decision)].onclick();
  await tick(); await tick();
  assert.deepEqual(requests.at(-1), {path: '/chats/chat-1/approvals/approval-1', body: {decision, message: 'Read instead'}});
  assert.ok(buttons(card).every(b => b.disabled));
  assert.equal(input.disabled, true);
  view.finish();
}
const body = new Element(), view = turnView(body);
view.accept({type: 'approval', id: 'danger', risk: 'destructive', summary: 'rm file'});
const dangerous = byClass(body, 'approval-card')[0];
assert.equal(buttons(dangerous)[1].disabled, true);
assert.equal(buttons(dangerous)[0].disabled, false);
view.accept({type: 'approval_resolved', id: 'danger', decision: 'deny', message: 'Approval timed out'});
assert.ok(dangerous.textContent.includes('Approval timed out'));
assert.ok(buttons(dangerous).every(b => b.disabled));
view.accept({type: 'plan', items: [{text: 'Inspect', status: 'in_progress'}, {text: 'Edit', status: 'pending'}]});
assert.equal(byClass(body, 'plan-in_progress').length, 1);
view.accept({type: 'plan', items: [{text: 'Inspect', status: 'done'}, {text: 'Edit', status: 'done'}]});
assert.equal(byClass(body, 'plan-view').length, 1);
assert.equal(byClass(body, 'plan-done').length, 2);
view.finish();
const pending = new Element(), stopped = turnView(pending);
stopped.accept({type: 'approval', id: 'stopped', risk: 'normal', summary: 'echo x'});
stopped.finish();
assert.ok(byClass(pending, 'approval-card')[0].textContent.includes('No longer pending'));

setChat({id: 'saved', permission: 'auto', cwd: '/saved', plan: [{text: 'Saved plan', status: 'done'}], messages: []});
assert.equal($('permission').value, 'auto'); assert.equal($('cwd').value, '/saved');
assert.equal(byClass($('messages'), 'plan-done').length, 1);
$('permission').value = 'full'; $('permission').onchange(); await tick(); await tick();
assert.ok(requests.some(r => r.path === '/chats/saved' && r.body?.permission === 'full'));
$('cwd').value = '/other'; $('cwd').onchange(); await tick(); await tick();
assert.ok(requests.some(r => r.path === '/chats/saved' && r.body?.cwd === '/other'));
console.log('Frontend approval, plan, saved settings, and control behavior passed');
