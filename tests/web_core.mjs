// DOM-level behavior checks, with no browser/CDN/Node package dependencies.
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import vm from 'node:vm';

class Element {
  constructor(tag = 'div') {
    this.tagName = tag; this.children = []; this.parentNode = null;
    this.className = ''; this.value = ''; this.disabled = false; this.hidden = false;
    this.listeners = {}; this.attrs = {}; this.dataset = {}; this.style = {};
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
  addEventListener(type, fn) { this.listeners[type] = fn; }
  querySelectorAll() { return []; }
  focus() {}
  get lastElementChild() { return this.children.at(-1); }
}
const elements = new Map();
function $(id) { if (!elements.has(id)) elements.set(id, new Element()); return elements.get(id); }
for (const id of ['connection', 'theme', 'web']) $(id).append(new Element('span'));
$('mode').value = 'work';
new Element().append($('connection')); const footer = new Element(), actions = new Element(); footer.append(actions); actions.append($('disconnect'));
const requests = [];
const sandbox = {
  document: {listeners: {}, getElementById: $, createElement: tag => new Element(tag), createTextNode: text => { const e = new Element('#text'); e.textContent = text; return e; },
    querySelectorAll: () => [], addEventListener(type, fn) { this.listeners[type] = fn; }, body: new Element(), documentElement: new Element()},
  window: {listeners: {}, addEventListener(type, fn) { this.listeners[type] = fn; }}, location: {hash: ''},
  performance, URLSearchParams, TextDecoder, console,
  requestAnimationFrame: fn => { fn(); }, setTimeout() {}, clearTimeout() {}, setInterval() {},
  localStorage: {getItem() {return 'dark';}, setItem() {}},
  fetch: async (path, options) => {
    requests.push({path, body: options.body ? JSON.parse(options.body) : null});
    const payloads = {
      '/chats': [],
      // The capability contract the server declares, and the catalog with it.
      '/capabilities': {provider: 'openai', capabilities: ['api_key', 'tools'],
                        offered: {chat: ['reasoning'], work: ['api_key', 'approvals', 'tools']}},
      '/providers/catalog': {harness_available: true, providers: [
        {id: 'openai', name: 'OpenAI', key_url: 'https://platform.openai.com/api-keys',
         capabilities: ['api_key', 'tools']},
        {id: 'chatgpt', name: 'ChatGPT subscription', key_url: '', capabilities: ['browser_login', 'reasoning', 'tools']},
        {id: 'codex', name: 'Installed Codex login', key_url: '', capabilities: ['harness_login', 'reasoning', 'tools']},
        {id: 'custom', name: 'Custom provider', key_url: '', base_url_hint: 'https://example.com/v1',
         capabilities: ['api_key', 'custom_headers', 'editable_base_url', 'reasoning', 'tools']},
      ]},
    };
    return {ok: true, json: async () => (path in payloads ? payloads[path] : {ok: true})};
  },
};
let source = readFileSync(new URL('../src/kova/web_assets/app.js', import.meta.url), 'utf8');
source = source.replace('controls(); start();', 'globalThis.test = {state, controls, turnView, setChat, renderModels, closeProvider, showProviderModels, readEvents, openChat, newChat, send, applyCapabilities, providerKindChanged};');
vm.runInNewContext(source, sandbox);
const {state, controls, turnView, setChat, applyCapabilities, providerKindChanged} = sandbox.test;
const walk = node => [node, ...node.children.flatMap(walk)];
const byClass = (node, cls) => walk(node).filter(n => n.className.split(' ').includes(cls));
const buttons = node => walk(node).filter(n => n.tagName === 'button');
const tick = () => new Promise(resolve => setImmediate(resolve));
// The controls follow the declared contract: the provider's capabilities
// intersected with the mode's, never a hardcoded mode name.
const openai = {provider: 'openai', capabilities: ['api_key', 'tools'],
                offered: {chat: ['reasoning'], work: ['api_key', 'approvals', 'tools']}};
applyCapabilities(openai);
state.connected = true; state.chat = {id: 'chat-1', cwd: '/project'};
controls(); assert.equal($('permission').disabled, false);
assert.equal($('cwd').disabled, false);
$('mode').value = 'chat'; controls(); assert.equal($('cwd').disabled, true);
assert.equal($('permission').disabled, true);
$('mode').value = 'work'; applyCapabilities({offered: {chat: [], work: []}}); controls();
assert.equal($('permission').disabled, true, 'a provider that declares nothing is offered nothing');
assert.equal($('cwd').disabled, true);
applyCapabilities(openai); controls(); assert.equal($('cwd').disabled, false);
$('mode').value = 'work'; state.busy = true; controls(); assert.equal($('permission').disabled, true);
assert.equal($('send').hidden, true, 'no provider declares steering, so send is withheld mid-reply');
state.busy = false;

for (const decision of ['allow', 'allow_session', 'allow_always', 'deny']) {
  const body = new Element(), view = turnView(body);
  view.accept({type: 'approval', id: 'approval-1', tool: 'run_command', args: {command: '<script>literal command</script>'}, risk: 'normal', summary: 'short summary'});
  const card = byClass(body, 'approval-card')[0];
  assert.ok(card.textContent.includes('<script>literal command</script>'));
  assert.ok(card.textContent.includes('/project'));
  const input = walk(card).find(n => n.tagName === 'input'); input.value = 'Read instead';
  buttons(card)[['allow', 'allow_session', 'allow_always', 'deny'].indexOf(decision)].onclick();
  await tick(); await tick();
  assert.deepEqual(requests.at(-1), {path: '/chats/chat-1/approvals/approval-1', body: {decision, message: 'Read instead'}});
  view.accept({type: 'approval_resolved', id: 'approval-1', decision});
  assert.equal(card.textContent.includes('Denied'), decision === 'deny');
  assert.ok(buttons(card).every(b => b.disabled));
  assert.equal(input.disabled, true);
  view.finish();
}
const body = new Element(), view = turnView(body);
view.accept({type: 'approval', id: 'danger', risk: 'destructive', summary: 'rm file'});
const dangerous = byClass(body, 'approval-card')[0];
assert.equal(buttons(dangerous)[1].disabled, false);
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

// Provider grouping is real DOM structure, not a label prefix on a flat list.
sandbox.test.renderModels([
  {name:'home/a',model:'alpha',provider_name:'Home'},
  {name:'cloud/b',model:'beta',provider_name:'Cloud'},
  {name:'home/c',model:'gamma',provider_name:'Home'},
]);
assert.deepEqual($('model').children.map(g => [g.tagName,g.label,g.children.length]),
                 [['optgroup','Home',2],['optgroup','Cloud',1]]);
assert.equal($('model').children[0].children[1].value, 'home/c');
$('provider-dialog').close = () => {};
$('provider-key').value = 'never-save-this'; $('provider-headers').value = '{"X-Secret":"private"}';
sandbox.test.closeProvider();
assert.equal($('provider-key').value, ''); assert.equal($('provider-headers').value, '');
sandbox.test.showProviderModels(['alpha','beta']);
assert.equal($('provider-models').children.length, 2);
$('provider-filter').value = 'beta'; $('provider-filter').oninput();
assert.equal($('provider-models').children[0].hidden, true);
assert.equal($('provider-models').children[1].hidden, false);
const choices = $('provider-models').children.map(label => label.children[0]);
choices[0].checked = false; choices[1].checked = true; choices[1].onchange();
assert.equal($('provider-active').children.length, 1);
assert.equal($('provider-active').children[0].value, 'beta');
assert.equal($('provider-next').disabled, false);
console.log('Provider grouping, filtering, selection and secret cleanup passed');

// The setup form shows exactly the fields each kind declares, so a provider
// is never asked for something it cannot use.
$('add-provider').onclick();
await tick(); await tick(); await tick();
$('provider-kind').value = 'openai'; providerKindChanged();
assert.equal($('provider-key-row').hidden, false);
assert.equal($('provider-url-row').hidden, true);
assert.equal($('provider-headers-row').hidden, true);
assert.equal($('provider-consent-row').hidden, true);
assert.equal($('provider-next').textContent, 'List models');
$('provider-kind').value = 'chatgpt'; providerKindChanged();
assert.equal($('provider-key-row').hidden, true, 'a browser sign-in has no key field');
assert.equal($('provider-next').textContent, 'Continue with ChatGPT');
$('provider-kind').value = 'codex'; providerKindChanged();
assert.equal($('provider-key-row').hidden, true);
assert.equal($('provider-consent-row').hidden, false, 'reusing an installed login asks for consent');
$('provider-kind').value = 'custom'; providerKindChanged();
assert.equal($('provider-url-row').hidden, false);
assert.equal($('provider-url').placeholder, 'https://example.com/v1');
assert.equal($('provider-headers-row').hidden, false);
assert.equal($('provider-key-row').hidden, false);
console.log('Provider setup fields follow declared capabilities passed');

// Byte-by-byte delivery covers partial UTF-8, CRLF boundaries and multiple frames.
const wire = new TextEncoder().encode('event: text\r\ndata: {"type":"text","text":"hé🔥"}\r\n\r\ndata: {"type":"done"}\n\n');
function responseFor(bytes) {
  let index = 0;
  return {body: {getReader: () => ({read: async () => index < bytes.length ? {value: bytes.slice(index, ++index), done: false} : {done: true}, releaseLock() {}})}};
}
const received = [];
await sandbox.test.readEvents(responseFor(wire), event => received.push(event));
assert.equal(received.length, 2); assert.equal(received[0].text, 'hé🔥');
await assert.rejects(sandbox.test.readEvents(responseFor(new TextEncoder().encode('data: {"type":"text"}')), () => {}), /mid-frame/);
await assert.rejects(sandbox.test.readEvents(responseFor(new TextEncoder().encode('data: not-json\n\n')), () => {}));

// A slow chat response must not overwrite a newer navigation (or a blank chat).
const oldFetch = sandbox.fetch;
let resolveFirst;
sandbox.fetch = path => path === '/chats/slow' ? new Promise(resolve => { resolveFirst = resolve; }) : Promise.resolve({ok: true, json: async () => ({id: 'fast', messages: []})});
await sandbox.test.newChat();
const first = sandbox.test.openChat('slow'); await sandbox.test.openChat('fast');
resolveFirst({ok: true, json: async () => ({id:'slow', messages:[]})}); await first;
assert.equal(state.chat.id, 'fast');
const second = sandbox.test.openChat('slow'); await sandbox.test.newChat();
resolveFirst({ok:true,json:async()=>({id:'slow',messages:[]})}); await second;
assert.equal(state.chat, null); sandbox.fetch = oldFetch;
console.log('SSE chunking, Unicode, truncation and chat navigation races passed');

// Composition, Shift+Enter, and normal submission use actual registered handlers.
let submits = 0, prevented = 0;
$('composer').requestSubmit = () => submits++;
for (const extra of [{shiftKey:true}, {isComposing:true}, {keyCode:229}]) {
  $('prompt').listeners.keydown({key:'Enter', preventDefault(){prevented++;}, ...extra});
}
assert.equal(submits, 0); assert.equal(prevented, 0);
$('prompt').listeners.keydown({key:'Enter', preventDefault(){prevented++;}});
assert.equal(submits, 1); assert.equal(prevented, 1);
// Upward user scrolling stays paused even within the old 100px threshold.
const area = $('scroll-area'); area.scrollHeight = 1000; area.clientHeight = 500; area.scrollTop = 450;
area.listeners.wheel({deltaY:-1}); area.listeners.scroll();
const followBody = new Element(), following = turnView(followBody);
following.accept({type:'tool_call',name:'test'}); assert.equal(area.scrollTop,450);
area.scrollTop = 500; area.listeners.scroll(); following.finish(); assert.equal(area.scrollTop,1000);
console.log('IME, keyboard and user-controlled auto-scroll passed');
