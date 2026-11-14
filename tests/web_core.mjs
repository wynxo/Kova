// DOM-level behavior checks, with no browser/CDN/Node package dependencies.
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import vm from 'node:vm';

let focusedElement = null;
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
  focus() { focusedElement = this; }
  showModal() {}
  close() {}
  get lastElementChild() { return this.children.at(-1); }
}
const elements = new Map();
function $(id) { if (!elements.has(id)) elements.set(id, new Element()); return elements.get(id); }
for (const id of ['connection', 'theme', 'web']) $(id).append(new Element('span'));
$('mode').value = 'work';
new Element().append($('connection')); const footer = new Element(), actions = new Element(); footer.append(actions); actions.append($('disconnect'));
const requests = [];
const uploads = [];
let pendingApprovals = [];
const sandbox = {
  document: {listeners: {}, getElementById: $, createElement: tag => new Element(tag), createTextNode: text => { const e = new Element('#text'); e.textContent = text; return e; },
    querySelectorAll: () => [], addEventListener(type, fn) { this.listeners[type] = fn; }, body: new Element(), documentElement: new Element(),
    get activeElement() { return focusedElement; }},
  window: {listeners: {}, addEventListener(type, fn) { this.listeners[type] = fn; }}, location: {hash: ''},
  performance, URLSearchParams, TextDecoder, console, FormData, File,
  requestAnimationFrame: fn => { fn(); }, setTimeout() {}, clearTimeout() {}, setInterval() {},
  localStorage: {getItem() {return 'dark';}, setItem() {}},
  fetch: async (path, options = {}) => {
    const form = options.body instanceof FormData ? options.body : null;
    const body = !form && options.body ? JSON.parse(options.body) : null;
    requests.push({path, body});
    if (form) uploads.push({path, file: form.get('file')});
    if (path === '/chats' && options.method === 'POST') {
      return {ok: true, json: async () => ({id: 'new-chat', title: null, mode: body?.mode || 'work',
        permission: 'manual', cwd: '/project', web: true, messages: [], plan: []})};
    }
    if (path === '/chats/new-chat/attachments' && form) {
      const file = form.get('file');
      return {ok: true, json: async () => ({id: 'uploaded', chat_id: 'new-chat', name: file.name,
        mime: file.type || 'text/plain', size: file.size, pages: null})};
    }
    const payloads = {
      '/chats': [],
      // The capability contract the server declares, and the catalog with it.
      '/capabilities': {provider: 'openai', capabilities: ['api_key', 'tools'],
                        offered: {chat: ['reasoning'], work: ['api_key', 'approvals', 'tools']}},
      '/capabilities?model=vision-model': {provider: 'local', capabilities: ['api_key', 'reasoning', 'tools', 'vision'],
                        offered: {chat: ['api_key', 'reasoning', 'vision'], work: ['api_key', 'approvals', 'reasoning', 'tools', 'vision']}},
      '/capabilities?model=text-model': {provider: 'local', capabilities: ['api_key', 'reasoning', 'tools'],
                        offered: {chat: ['api_key', 'reasoning'], work: ['api_key', 'approvals', 'reasoning', 'tools']}},
      '/providers/catalog': {harness_available: true, providers: [
        {id: 'openai', name: 'OpenAI', key_url: 'https://platform.openai.com/api-keys',
         capabilities: ['api_key', 'tools']},
        {id: 'chatgpt', name: 'ChatGPT subscription', key_url: '', capabilities: ['browser_login', 'reasoning', 'tools']},
        {id: 'codex', name: 'Installed Codex login', key_url: '', capabilities: ['harness_login', 'reasoning', 'tools']},
        {id: 'custom', name: 'Custom provider', key_url: '', base_url_hint: 'https://example.com/v1',
         capabilities: ['api_key', 'custom_headers', 'editable_base_url', 'reasoning', 'tools']},
      ]},
    };
    if (path.endsWith('/compact')) return {ok: true, json: async () => ({type: 'compact_skipped', message: 'Nothing to compact'})};
    if (path.endsWith('/fork')) return {ok: true, json: async () => ({id: 'blocked'})};
    if (path.endsWith('/checkpoints')) return {ok: true, json: async () => [{id: 7, files: [{path: 'notes.txt', status: 'modified'}]}]};
    // A chat whose turn is blocked on an approval that was asked before a reload.
    if (path === '/chats/blocked') return {ok: true, json: async () => ({id: 'blocked', cwd: '/project', state: 'blocked-on-approval', messages: [{role: 'user', content: 'clean up'}]})};
    if (path === '/chats/blocked/approvals') return {ok: true, json: async () => pendingApprovals};
    if (path.endsWith('/queue') && (options.method || 'GET') === 'GET') return {ok: true, json: async () => [{id: 9, text: 'later'}]};
    if (path.endsWith('/queue') && options.method === 'POST') return {ok: true, json: async () => ({id: 10, text: body.text})};
    if (path === '/models' && options.method === 'PATCH') return {ok: true, json: async () => ({name: body.name, vision: body.vision})};
    return {ok: true, json: async () => (path in payloads ? payloads[path] : {ok: true})};
  },
};
let source = readFileSync(new URL('../src/kova/web_assets/app.js', import.meta.url), 'utf8');
source = source.replace('controls(); start();', 'globalThis.test = {parseCommand, dispatchCommand, commandPicker, commands, state, controls, turnView, setChat, toolSummary, toolHint, toolLabel, toggleSidebar, stop, renderModels, closeProvider, showProviderModels, readEvents, openChat, newChat, send, queueMessage, steerMessage, loadQueue, applyCapabilities, refreshCapabilities, openModelSettings, providerKindChanged, attachFiles, pendingAttachments, renderAttachments, applyShare, readWorkerShare};');
vm.runInNewContext(source, sandbox);
const {state, controls, turnView, setChat, queueMessage, steerMessage, loadQueue, applyCapabilities, providerKindChanged} = sandbox.test;
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
  // The card is one stop, is announced, and jumps focus to its first decision.
  assert.equal(card.getAttribute('role'), 'group');
  assert.ok($('announce').textContent.includes('Kova needs approval: run command'), $('announce').textContent);
  assert.ok($('announce').textContent.includes('/project'));
  assert.equal(sandbox.document.activeElement, buttons(card)[0], 'focus lands on the first button');
  const input = walk(card).find(n => n.tagName === 'input'); input.value = 'Read instead';
  buttons(card)[['allow', 'allow_session', 'allow_always', 'deny'].indexOf(decision)].onclick();
  await tick(); await tick();
  assert.deepEqual(requests.at(-1), {path: '/chats/chat-1/approvals/approval-1', body: {decision, message: 'Read instead'}});
  assert.equal(sandbox.document.activeElement, $('prompt'), 'focus returns to the composer');
  view.accept({type: 'approval_resolved', id: 'approval-1', decision});
  assert.equal(card.textContent.includes('Denied'), decision === 'deny');
  assert.ok(buttons(card).every(b => b.disabled));
  assert.equal(input.disabled, true);
  view.finish();
}
for (const options of [[], ['A', 'B']]) {
  const questionBody = new Element(), questionView = turnView(questionBody);
  questionView.accept({type: 'question', id: 'q1', args: {question: 'Which?', options}});
  const card = byClass(questionBody, 'approval-card')[0];
  assert.ok(card.textContent.includes('Which?'));
  assert.ok($('announce').textContent.includes('Kova asked a question: Which?'));
  const expected = options.length ? buttons(card)[0] : walk(card).find(n => n.tagName === 'input');
  assert.equal(sandbox.document.activeElement, expected, 'the answer control takes focus');
  if (!options.length) walk(card).find(n => n.tagName === 'input').value = 'typed answer';
  buttons(card)[options.length ? 1 : 0].onclick();
  await tick(); await tick();
  assert.deepEqual(requests.at(-1), {path: '/chats/chat-1/approvals/q1', body: {decision: 'allow', message: options.length ? 'B' : 'typed answer'}});
  assert.equal(sandbox.document.activeElement, $('prompt'), 'focus returns to the composer');
  questionView.accept({type: 'question_resolved', id: 'q1'});
  assert.ok(buttons(card).every(b => b.disabled));
  questionView.finish();
}

// Reloading the page loses the turn's stream, so a still-pending approval is
// rebuilt from GET /chats/{id}/approvals and can be answered from the new card.
{
  const {openChat} = sandbox.test;
  pendingApprovals = [{type: 'approval', id: 'lost-1', tool: 'run_command', args: {command: 'rm -rf build'}, risk: 'destructive', summary: 'rm -rf build'},
                      {type: 'question', id: 'lost-q', tool: 'ask_user', args: {question: 'Keep logs?', options: ['Yes', 'No']}, risk: 'normal', summary: 'ask_user: '}];
  $('messages').replaceChildren();
  await openChat('blocked'); await tick(); await tick();
  const view = byClass($('messages'), 'pending-view')[0];
  assert.ok(view, 'a pending approval is shown again after a reload');
  assert.ok(view.textContent.includes('rm -rf build') && view.textContent.includes('Keep logs?'));
  assert.ok(byClass($('messages'), 'background-turn')[0].textContent.includes('paused until you answer'));
  const [approval, question] = byClass(view, 'approval-card');
  buttons(approval)[3].onclick(); await tick(); await tick();
  assert.deepEqual(requests.at(-1), {path: '/chats/blocked/approvals/lost-1', body: {decision: 'deny', message: null}});
  assert.ok(approval.textContent.includes('Denied'));
  buttons(question)[0].onclick(); await tick(); await tick();
  assert.deepEqual(requests.at(-1), {path: '/chats/blocked/approvals/lost-q', body: {decision: 'allow', message: 'Yes'}});
  // A chat with nothing pending gets no card.
  pendingApprovals = [];
  await openChat('blocked'); await tick(); await tick();
  assert.equal(byClass($('messages'), 'pending-view').length, 0);
  state.chat = {id: 'chat-1', cwd: '/project'};
  console.log('Pending approvals recovered after reload passed');
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

// How a turn ended is announced once, in the live region.
$('announce').replaceChildren();
const ending = turnView(new Element());
ending.accept({type: 'done'});
assert.equal($('announce').textContent, 'Reply finished.');
ending.accept({type: 'stopped'});
assert.equal($('announce').textContent, 'Reply finished.Stopped.');
ending.accept({type: 'error', message: 'model offline'});
assert.ok($('announce').textContent.endsWith('Error: model offline'), $('announce').textContent);

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

// Per-turn change review: the card, its diff preview, and its restore call.
state.chat = {id: 'chat-1', cwd: '/project'};
const reviewBody = new Element(), reviewView = turnView(reviewBody);
reviewView.accept({type: 'review', checkpoint_id: 7, label: 'fix the notes',
                   files: [{path: 'notes.txt', status: 'modified', additions: 1, deletions: 1}]});
const changes = byClass(reviewBody, 'changes-card')[0];
assert.ok(changes.textContent.includes('Changed in this turn'));
assert.ok(changes.textContent.includes('fix the notes'));
assert.equal(byClass(changes, 'status-modified').length, 1);
assert.ok(changes.textContent.includes('+1 −1'));
const file = byClass(changes, 'change-file')[0];
file.open = true; file.listeners.toggle(); await tick(); await tick();
assert.ok(requests.some(r => r.path === '/chats/chat-1/checkpoints/7/diff?path=notes.txt'));
assert.equal(byClass(file, 'diff').length, 1);
buttons(changes)[0].onclick(); await tick(); await tick();
const restore = byClass(sandbox.document.body, 'restore-dialog').at(-1);
assert.ok(restore.textContent.includes('Restore to change set 7?'));
assert.ok(restore.textContent.includes('notes.txt'));
buttons(restore)[1].onclick(); await tick(); await tick();
assert.ok(requests.some(r => r.path === '/chats/chat-1/checkpoints/7/restore' && r.body === null));
reviewView.finish();
console.log('Per-turn change review, preview and restore passed');

// Queue vs steer: both are explicit while a reply runs, and the queue survives a reload.
state.chat = {id: 'chat-1', cwd: '/project'};
state.busy = true; $('prompt').value = 'later please'; controls();
assert.equal($('send').hidden, true);
assert.equal($('queue').hidden, false); assert.equal($('steer').hidden, false);
assert.equal($('queue').disabled, false); assert.equal($('steer').disabled, false);
await queueMessage('later please'); await tick();
assert.ok(requests.some(r => r.path === '/chats/chat-1/queue' && r.body.text === 'later please'));
assert.equal($('queue-strip').hidden, false);
assert.ok($('queue-strip').textContent.includes('later please'));
assert.ok($('queue-strip').textContent.includes('Queued'));
$('prompt').value = 'change course';
await steerMessage(); await tick();
assert.ok(requests.some(r => r.path === '/chats/chat-1/steer' && r.body.text === 'change course'));
await loadQueue(); await tick();
assert.deepEqual(state.queued.map(row => row.text), ['later']);
assert.ok($('queue-strip').textContent.includes('later'));
const steerBody = new Element(), steerView = turnView(steerBody);
steerView.accept({type: 'steer', text: 'change course'});
assert.ok(byClass(steerBody, 'turn-status')[0].textContent.includes('Steering: change course'));
steerView.finish();
state.queued = []; state.busy = false; controls();
console.log('Queue and steer passed');

// Chat feel: one live indicator at a time, readable tool rows, message actions and keys.
sandbox.marked = {parse: text => text}; sandbox.DOMPurify = {sanitize: html => html};
const copied = []; sandbox.navigator = {clipboard: {writeText: async text => { copied.push(text); }}};
const {toolSummary, toolHint, toolLabel} = sandbox.test;
assert.equal(toolLabel('run_command'), 'Run command');
assert.equal(toolHint({command: 'ls  -la\n'}), 'ls -la');
assert.equal(toolHint('{"path": "notes.txt"}'), 'notes.txt', 'saved calls carry their arguments as JSON text');
assert.equal(toolHint('not json'), '');
assert.equal(toolSummary({ok: false, name: 'run_command', summary: '{"ok": false, "err', output: '{"ok": false, "error": "exit  status 1"}'}), 'exit status 1');
assert.equal(toolSummary({ok: true, name: 'web_search', output: '[{"title": "a"}, {"title": "b"}]'}), '2 results');
assert.equal(toolSummary({ok: true, name: 'read_file', summary: 'plain text', output: 'plain text'}), 'plain text');
assert.equal(toolSummary({ok: true, name: 'update_plan', output: '{}'}), 'Plan updated');

state.chat = {id: 'chat-1', cwd: '/project'};
const article = new Element('article'), liveBody = new Element(); article.append(liveBody);
const live = turnView(liveBody);
const activityRow = byClass(liveBody, 'activity')[0];
assert.equal(Boolean(activityRow.hidden), false, 'a fresh turn shows that Kova is thinking');
live.accept({type: 'reasoning', text: 'Let me think.'});
assert.equal(activityRow.hidden, true, 'the reasoning line is the only indicator while it streams');
assert.ok(byClass(liveBody, 'thinking')[0].classList.contains('live'));
live.accept({type: 'tool_call', name: 'run_command', args: {command: 'npm test'}});
assert.equal(byClass(liveBody, 'thinking')[0].classList.contains('live'), false);
const liveTool = byClass(liveBody, 'tool-row')[0];
assert.ok(liveTool.textContent.includes('Run command') && liveTool.textContent.includes('npm test'));
live.accept({type: 'tool_result', name: 'run_command', ok: false, summary: '{"ok": false', output: '{"ok": false, "error": "2 tests failed"}'});
assert.ok(liveTool.textContent.includes('2 tests failed'), 'a failed tool says why');
assert.ok(liveTool.classList.contains('failed'));
assert.equal(byClass(liveBody, 'tool-output').length, 1, 'the full result opens under the row');
assert.ok(activityRow.textContent.includes('Working'));
live.accept({type: 'text', text: 'All '}); live.accept({type: 'text', text: 'done.'});
assert.ok(byClass(liveBody, 'markdown')[0].classList.contains('live'));
live.finish();
assert.equal(byClass(liveBody, 'markdown')[0].classList.contains('live'), false);
const replyActions = byClass(article, 'message-actions')[0];
buttons(replyActions)[0].onclick(); await tick();
assert.deepEqual(copied, ['All done.'], 'the reply copies as the text the model wrote');

// A saved turn is one message, however many rounds it took.
setChat({id: 'saved-turn', messages: [
  {role: 'user', content: 'Read my notes'},
  {role: 'assistant', content: 'Looking.', tool_calls: [{function: {name: 'read_file', arguments: '{"path": "notes.txt"}'}}]},
  {role: 'tool', content: '...'},
  {role: 'assistant', content: 'They are short.'},
]});
assert.equal(byClass($('messages'), 'assistant').length, 1);
assert.ok(byClass($('messages'), 'tool-row')[0].textContent.includes('notes.txt'));
const savedActions = byClass($('messages'), 'message-actions');
assert.equal(savedActions.length, 2, 'your message and the reply each get actions');
buttons(savedActions[1])[0].onclick(); await tick();
assert.equal(copied.at(-1), 'Looking.\n\nThey are short.');
buttons(savedActions[0])[1].onclick();
assert.equal($('prompt').value, 'Read my notes', 'edit brings your words back to the box');

// Up in an empty box recalls the last message; it never clobbers a draft.
$('prompt').value = 'a draft'; $('prompt').listeners.keydown({key: 'ArrowUp', preventDefault() {}});
assert.equal($('prompt').value, 'a draft');
$('prompt').value = ''; $('prompt').listeners.keydown({key: 'ArrowUp', preventDefault() {}});
assert.equal($('prompt').value, 'Read my notes');

// Queue and Steer appear only when there is something to send; Escape stops the reply.
state.busy = true; $('prompt').value = ''; controls();
assert.equal($('stop').hidden, false);
assert.equal($('queue').hidden, true); assert.equal($('steer').hidden, true);
$('prompt').value = 'wait'; controls();
assert.equal($('queue').hidden, false); assert.equal($('steer').hidden, false);
const keys = sandbox.document.listeners.keydown; let handled = 0;
keys({key: 'Escape', target: $('prompt'), preventDefault() { handled++; }}); await tick(); await tick();
assert.equal(state.stopRequested, true); assert.equal(handled, 1);
state.busy = false; state.stopRequested = false; $('prompt').value = ''; controls();
keys({key: 'Escape', target: $('prompt'), preventDefault() { handled++; }});
assert.equal(handled, 1, 'Escape is left alone when nothing is running');
let focused = 0; $('prompt').focus = () => { focused++; };
keys({key: '/', target: {tagName: 'TEXTAREA'}, preventDefault() { handled++; }});
assert.equal(focused, 0, 'a slash typed into a field stays a slash');
keys({key: '/', target: {tagName: 'BODY'}, preventDefault() { handled++; }});
assert.equal(focused, 1); assert.equal(handled, 2);
let opened = 0; $('shortcuts-dialog').showModal = () => { opened++; };
keys({key: '/', ctrlKey: true, target: $('prompt'), preventDefault() {}});
keys({key: '?', target: {tagName: 'BODY'}, preventDefault() {}});
assert.equal(opened, 2);
// The drawer on a phone; without matchMedia the harness is the narrow layout.
sandbox.test.toggleSidebar();
assert.ok(sandbox.document.body.classList.contains('sidebar-open'));
assert.equal($('menu').getAttribute('aria-expanded'), 'true');
keys({key: 'Escape', target: {tagName: 'BODY'}, preventDefault() {}});
assert.equal(sandbox.document.body.classList.contains('sidebar-open'), false);
console.log('Live indicators, tool rows, message actions and keyboard shortcuts passed');

// Known commands are intercepted, including invalid arguments; paths and multiline text are not.
for (const name of Object.keys(sandbox.test.commands)) {
  assert.equal(sandbox.test.parseCommand(`/${name}`).name, name);
  assert.equal(sandbox.test.parseCommand(`/${name} bad argument`).name, name);
}
for (const text of ['/unknown', '/home/you/project', '/compact\nhello', '/fork\rhello']) {
  assert.equal(sandbox.test.parseCommand(text), null);
}
state.connected = true; state.busy = false;
for (const name of ['help', 'diff', 'restore', 'changes', 'cd', 'steer']) {
  const before = requests.length;
  await sandbox.test.dispatchCommand({name, args: ''});
  assert.equal(requests.length, before, `${name} never calls the model`);
}
await assert.rejects(sandbox.test.dispatchCommand({name: 'fork', args: 'bad'}), /Use \/fork/);
await assert.rejects(sandbox.test.dispatchCommand({name: 'mode', args: 'bad'}), /Usage/);
$('prompt').value = '/com'; sandbox.test.commandPicker();
assert.equal($('command-picker').children.length, 1);
assert.equal($('command-picker').children[0].textContent, '/compact');
console.log('Slash command catalog, dispatch guards and picker passed');

state.chat = {id: 'blocked'};
await sandbox.test.dispatchCommand({name: 'compact', args: ''});
assert.equal(requests.at(-1).path, '/chats/blocked/compact');
assert.ok($('toasts').textContent.includes('Nothing to compact'));
await sandbox.test.dispatchCommand({name: 'fork', args: ''});
assert.ok(requests.some(r => r.path === '/chats/blocked/fork'));
state.busy = true;
const beforeCommands = requests.length;
$('prompt').value = '/compact';
await sandbox.test.send({preventDefault() {}});
assert.equal(requests.length, beforeCommands, 'busy commands are never queued');
state.busy = false;

// Composer attachment input paths and model declarations.
state.connected = true; state.busy = false; state.chat = {id: 'attachment-chat'};
applyCapabilities({offered: {work: []}});
$('mode').value = 'work'; controls();
assert.equal($('attach').classList.contains('text-only'), true);
assert.equal(typeof $('composer').listeners.drop, 'function');
assert.equal(typeof $('prompt').listeners.paste, 'function');
sandbox.test.pendingAttachments.push({id: 'a', chat_id: 'attachment-chat', name: 'demo.txt', size: 1024, pages: 2});
sandbox.test.renderAttachments();
assert.match($('attachment-chips').textContent, /demo.txt.*1.0 KB.*2 pages.*ready/);
assert.equal($('attachment-note').hidden, false);
state.chat = {id: 'other-chat'}; sandbox.test.renderAttachments();
assert.equal($('attachment-chips').textContent, '');
// The composer asks for the selected model's contract, not only the active one.
state.connected = true; state.busy = false; state.chat = {id: 'attachment-chat'};
state.models = [
  {name: 'vision-model', model: 'vision', provider_name: 'Local', context: 32768, vision: true, base_url: 'http://127.0.0.1:8080/v1'},
  {name: 'text-model', model: 'text', provider_name: 'Local', context: 32768, vision: false, base_url: 'http://127.0.0.1:8080/v1'},
];
$('model').value = 'vision-model'; $('model').onchange();
await tick(); await tick(); await tick();
assert.ok(requests.some(r => r.path === '/capabilities?model=vision-model'));
assert.equal($('attach').classList.contains('text-only'), false);
$('model').value = 'text-model'; $('model').onchange();
await tick(); await tick(); await tick();
assert.ok(requests.some(r => r.path === '/capabilities?model=text-model'));
assert.equal($('attach').classList.contains('text-only'), true);
const beforeDrop = requests.length, beforeToasts = $('toasts').children.length;
await sandbox.test.attachFiles([{type: 'image/png', name: 'shot.png', size: 16}]);
await tick();
assert.equal(requests.length, beforeDrop, 'a non-vision model refuses the image before uploading');
assert.equal($('toasts').children.length, beforeToasts + 1);
assert.match($('toasts').textContent, /Turn on Vision for it in Model settings/);
// Model settings exposes the same flag and saves it through the API.
$('model').value = 'vision-model';
await sandbox.test.openModelSettings();
const settings = sandbox.document.body.children.at(-1);
assert.ok(settings.textContent.includes('Model settings'));
assert.ok(settings.textContent.includes('Vision: this model can read images'));
const vision = walk(settings).find(node => node.tagName === 'input');
assert.equal(vision.checked, true);
vision.checked = false;
buttons(settings).find(button => button.textContent === 'Save').onclick();
await tick(); await tick(); await tick();
assert.ok(requests.some(r => r.path === '/models' && r.body?.name === 'vision-model' && r.body?.vision === false));
settings.remove();
console.log('Attachment chips, input handlers, selected-model gating and Model settings passed');

// Share-sheet files become native composer attachments, never base64 prose.
state.connected = true; state.busy = false; state.chat = null;
sandbox.test.pendingAttachments.length = 0;
applyCapabilities({offered: {work: ['vision']}}); $('mode').value = 'work'; controls();
const beforeShare = requests.length;
await sandbox.test.applyShare({text: 'Handle this', files: [new File(['hello'], 'note.txt', {type: 'text/plain'})]});
assert.equal($('prompt').value, 'Handle this');
assert.equal(uploads.at(-1).file.name, 'note.txt', 'shared files upload through the attachment API');
const uploaded = sandbox.test.pendingAttachments.at(-1);
assert.equal(uploaded.chat_id, 'new-chat');
assert.ok(!$('prompt').value.includes('Base64') && !$('prompt').value.includes('note.txt'));
assert.match($('attachment-chips').textContent, /note.txt/);

// A non-vision model refuses a shared image before any upload or chat is made.
applyCapabilities({offered: {work: []}}); state.chat = null;
sandbox.test.pendingAttachments.length = 0;
const beforeImage = requests.length, beforeVisionToast = $('toasts').children.length;
await sandbox.test.applyShare({text: '', files: [new File(['png'], 'shot.png', {type: 'image/png'})]});
await tick();
assert.equal(requests.length, beforeImage, 'no chat or upload request for a refused image');
assert.equal(sandbox.test.pendingAttachments.length, 0);
assert.equal($('toasts').children.length, beforeVisionToast + 1);
assert.match($('toasts').textContent, /Turn on Vision for it in Model settings/);

// The server fallback already saved files: the composer adopts those rows and
// deletes an image the selected model cannot read.
applyCapabilities({offered: {work: []}}); state.busy = false;
const rejectedChat = {id: 'rejected-chat', mode: 'work', permission: 'manual', cwd: '/project', web: true, messages: []};
const beforeRejected = requests.length;
await sandbox.test.applyShare({text: '', chat: rejectedChat,
  attachments: [{id: 'image-1', chat_id: 'rejected-chat', name: 'shot.png', mime: 'image/png', size: 3}]});
await tick();
assert.ok(requests.some(r => r.path === '/chats/rejected-chat/attachments/image-1'));
assert.ok(requests.some(r => r.path === '/chats/rejected-chat' && r.body === null),
          'an empty share chat is removed when every file was refused');
assert.equal(sandbox.test.pendingAttachments.length, 0);
assert.equal(state.chat, null);
applyCapabilities({offered: {work: ['vision']}});
const keptChat = {id: 'kept-chat', mode: 'work', permission: 'manual', cwd: '/project', web: true, messages: []};
await sandbox.test.applyShare({text: 'Look', chat: keptChat,
  attachments: [{id: 'file-1', chat_id: 'kept-chat', name: 'note.txt', mime: 'text/plain', size: 7},
                {id: 'image-2', chat_id: 'kept-chat', name: 'shot.png', mime: 'image/png', size: 3}]});
assert.equal(state.chat.id, 'kept-chat');
assert.equal($('prompt').value, 'Look');
assert.match($('attachment-chips').textContent, /note.txt/);
assert.match($('attachment-chips').textContent, /shot.png/);
assert.equal(sandbox.test.pendingAttachments.length, 2);

// A worker-staged share is read once and yields real File objects for the
// authenticated upload path; a second read finds nothing.
const workerFiles = new Map();
workerFiles.set('http://localhost/app/share/abc/manifest', new Response(JSON.stringify({
  created: Date.now(), text: 'From phone',
  files: [{name: 'photo.png', type: 'image/png', key: 'http://localhost/app/share/abc/file/0'}]})));
workerFiles.set('http://localhost/app/share/abc/file/0', new Response(new Blob([new Uint8Array([1, 2, 3])], {type: 'image/png'})));
sandbox.location.origin = 'http://localhost';
sandbox.caches = {open: async () => ({
  match: async key => workerFiles.get(key), delete: async key => workerFiles.delete(key)})};
const fromWorker = await sandbox.test.readWorkerShare('abc');
assert.equal(fromWorker.text, 'From phone');
assert.equal(fromWorker.files[0].name, 'photo.png');
assert.ok(fromWorker.files[0] instanceof File);
assert.equal(await sandbox.test.readWorkerShare('abc'), null, 'a worker share is consumed once');
console.log('Native file shares, vision rejection and worker handoff passed');

// Native selected-text drops must remain available to the textarea.
let textDropPrevented = false;
$('composer').listeners.drop({dataTransfer: {files: []}, preventDefault() { textDropPrevented = true; }});
assert.equal(textDropPrevented, false);
