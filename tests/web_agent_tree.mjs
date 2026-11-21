// The live tree of parallel tool calls (#192): built from task events with parent ids.
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import vm from 'node:vm';

let focused = null;
class Element {
  constructor(tag) {
    this.tagName = tag; this.children = []; this.attrs = {}; this.text = ''; this.hidden = false;
    this.className = ''; this.listeners = {};
    this.classList = {
      toggle: (name, force) => {
        const names = new Set(this.className.split(' ').filter(Boolean));
        const add = force ?? !names.has(name); if (add) names.add(name); else names.delete(name);
        this.className = [...names].join(' ');
      },
      contains: name => this.className.split(' ').includes(name),
    };
  }
  set textContent(value) { this.text = String(value); this.children = []; }
  get textContent() { return this.text + this.children.map(child => child.textContent).join(''); }
  append(...nodes) { for (const node of nodes) { node.parent = this; this.children.push(node); } }
  setAttribute(key, value) { this.attrs[key] = String(value); }
  getAttribute(key) { return this.attrs[key]; }
  removeAttribute(key) { delete this.attrs[key]; }
  addEventListener(type, fn) { this.listeners[type] = fn; }
  focus() { focused = this; }
  remove() { this.parent.children.splice(this.parent.children.indexOf(this), 1); }
}
const all = node => [node, ...node.children.flatMap(all)];
const byRole = (node, role) => all(node).filter(child => child.attrs.role === role);
const byClass = (node, cls) => all(node).filter(child => child.className.split(' ').includes(cls));
const key = (tree, target, name) => {
  let prevented = false;
  tree.listeners.keydown({key: name, target, preventDefault: () => { prevented = true; }});
  return prevented;
};

let now = 0;
const timers = [];
const context = {
  performance: {now: () => now},
  document: {createElement: tag => new Element(tag)},
  KovaDiff: {file: () => new Element('div')},
  setInterval: fn => { timers.push(fn); return timers.length; },
  clearInterval: id => { timers[id - 1] = null; },
};
vm.createContext(context);
const source = name => readFileSync(new URL(`../src/kova/web_assets/${name}`, import.meta.url), 'utf8');
vm.runInContext(source('tool-cards.js') + '\n' + source('agent-tree.js') + '\nglobalThis.tree = KovaAgentTree;', context);

// ---------------------------------------------------------------- the router
const inserted = [];
const turn = context.tree.turn({insert: node => inserted.push(node), label: name => `L:${name}`, hint: args => args?.path || ''});
assert.equal(turn.accept({type: 'tool_call', id: 'solo', name: 'run_command'}), false,
  'an ordinary tool call is not the tree\'s');
assert.equal(turn.accept({type: 'text', text: 'hi'}), false);

assert.equal(turn.accept({type: 'task_start', id: 'g', parent: null, kind: 'parallel', model: 'local-model', total: 3}), true);
assert.equal(inserted.length, 1, 'the group start inserts one tree');
const tree = inserted[0];
assert.equal(tree.attrs.role, 'tree');
for (const [id, name, path] of [['a', 'read_file', 'src/app.py'], ['b', 'search_files', ''], ['c', 'web_fetch', '']]) {
  assert.equal(turn.accept({type: 'task_start', id, parent: 'g', kind: 'tool', name, args: {path}, model: 'local-model', status: 'queued'}), true);
  assert.equal(turn.accept({type: 'tool_call', id, name, args: {path}}), true, 'a grouped call\'s tool_call belongs to its node');
}
const items = byRole(tree, 'treeitem');
assert.equal(items.length, 4, 'one root and one item per call');
const [root, a, b] = items;
assert.equal(root.attrs['aria-level'], '1');
assert.equal(a.attrs['aria-level'], '2');
assert.equal(root.attrs['aria-expanded'], 'true', 'the tree opens while the turn runs');
assert.equal(a.attrs['aria-expanded'], 'false', 'a task starts folded');
assert.equal(root.attrs.tabindex, '0', 'one tab stop');
assert.ok(items.slice(1).every(item => item.attrs.tabindex === '-1'));
assert.equal(byRole(tree, 'group').filter(g => g.className === 'agent-tree-children').length, 4, 'each item owns a group');
assert.match(root.textContent, /Running 3 tools in parallel/);
assert.match(root.textContent, /local-model/, 'the root shows the model');
assert.match(a.textContent, /L:read_file/, 'a task shows its label');
assert.match(a.textContent, /src\/app\.py/, 'and the hint for its arguments');
assert.match(a.textContent, /local-model/, 'and its model');
assert.match(a.textContent, /Waiting/, 'a queued task says so');

// Progress, completion in any order, a failure.
turn.accept({type: 'task_progress', id: 'a', parent: 'g', status: 'running'});
turn.accept({type: 'task_progress', id: 'b', parent: 'g', status: 'running'});
now = 400;
assert.match(a.textContent, /Running/);
timers.filter(Boolean).forEach(fn => fn());
assert.match(a.textContent, /0\.4s/, 'elapsed time ticks while it runs');
assert.equal(turn.accept({type: 'task_done', id: 'b', parent: 'g', ok: true, summary: '<b>3 matches</b>', elapsed_ms: 300}), true);
assert.match(b.textContent, /Done/);
assert.equal(byClass(root, 'agent-tree-hint')[0].textContent, 'L:search_files: Result received');
assert.equal(byClass(b, 'agent-tree-result')[0].tagName, 'blockquote');
assert.equal(byClass(b, 'agent-tree-result')[0].textContent, '<b>3 matches</b>');
assert.match(b.textContent, /0\.3s/, 'the server\'s elapsed time is shown');
turn.accept({type: 'task_progress', id: 'g', parent: null, done: 1, total: 3, failed: 0});
turn.accept({type: 'task_error', id: 'a', parent: 'g', message: 'Error: nope', elapsed_ms: 500});
assert.match(a.textContent, /Failed/);
assert.equal(byClass(a, 'agent-tree-result')[0].textContent, 'Error: nope');
assert.ok(a.classList.contains('failed'));
turn.accept({type: 'task_progress', id: 'g', parent: null, done: 2, total: 3, failed: 1});
assert.match(root.textContent, /1 failed/);

// A grouped result fills that node's tool row, matched by id.
assert.equal(turn.accept({type: 'tool_result', id: 'b', name: 'search_files', ok: true, output: '3 matches'}), true);
const bCard = byClass(b, 'tool-card')[0];
assert.ok(bCard, 'the task expands to the same tool card component');
assert.match(bCard.textContent, /3 matches/);
assert.doesNotMatch(byClass(a, 'tool-card')[0].textContent, /3 matches/, 'a result never lands on another call');

// ---------------------------------------------------------------- keyboard
assert.equal(key(tree, root, 'ArrowDown'), true);
assert.equal(focused, a, 'Down moves to the first task');
assert.equal(a.attrs.tabindex, '0'); assert.equal(root.attrs.tabindex, '-1');
key(tree, a, 'ArrowRight');
assert.equal(a.attrs['aria-expanded'], 'true', 'Right opens a task');
key(tree, a, 'ArrowLeft');
assert.equal(a.attrs['aria-expanded'], 'false', 'Left closes it');
key(tree, a, 'ArrowLeft');
assert.equal(focused, root, 'Left on a closed task steps out to its parent');
key(tree, root, 'End');
assert.equal(focused, items[3], 'End jumps to the last visible item');
key(tree, items[3], 'Home');
assert.equal(focused, root);
key(tree, root, 'Enter');
assert.equal(root.attrs['aria-expanded'], 'false', 'Enter toggles');
key(tree, root, 'ArrowDown');
assert.equal(focused, root, 'a closed group hides its tasks from arrow keys');
key(tree, root, ' ');
assert.equal(root.attrs['aria-expanded'], 'true', 'Space toggles back');
assert.equal(key(tree, new Element('button'), 'ArrowDown'), false, 'keys inside a tool card are left alone');

// ---------------------------------------------------------------- stopping one call
{
  const stops = [];
  let answer = {stopped: true};
  const stoppable = [];
  const stopTurn = context.tree.turn({insert: node => stoppable.push(node), label: name => `L:${name}`,
    stopTask: id => { stops.push(id); return answer instanceof Error ? Promise.reject(answer) : Promise.resolve(answer); }});
  const settle = () => new Promise(resolve => setImmediate(resolve));
  stopTurn.accept({type: 'task_start', id: 'sg', parent: null, model: 'm'});
  for (const id of ['s1', 's2', 's3']) stopTurn.accept({type: 'task_start', id, parent: 'sg', name: 'web_fetch', status: 'queued'});
  const [sRoot, s1, s2, s3] = byRole(stoppable[0], 'treeitem');
  const stopOf = item => byClass(item, 'agent-tree-stop')[0];
  const said = () => stoppable[0].children.at(-1).textContent;
  assert.equal(stopOf(sRoot).hidden, true, 'the group itself has no Stop');
  assert.equal(stopOf(s1).hidden, false, 'a waiting call can be stopped');
  assert.equal(stopOf(s1).attrs['aria-label'], 'Stop L:web_fetch');
  assert.equal(stopOf(s1).attrs.tabindex, '-1', 'the tree stays one tab stop');
  assert.equal(s1.attrs['aria-keyshortcuts'], 'Delete');

  // The pointer: the button asks once, says so, and the row does not toggle.
  let stopped = false;
  stopOf(s1).listeners.click({stopPropagation: () => { stopped = true; }});
  stopOf(s1).listeners.click({stopPropagation() {}});
  assert.equal(stopped, true, 'the click does not also toggle the row');
  assert.deepEqual(stops, ['s1'], 'a second click while stopping does nothing');
  assert.equal(stopOf(s1).text, 'Stopping…');
  assert.equal(stopOf(s1).attrs['aria-disabled'], 'true');
  assert.match(said(), /Stopping L:web_fetch/);
  await settle();
  // The server's task_error with stopped: true is what ends it, as Stopped (not Failed).
  stopTurn.accept({type: 'task_error', id: 's1', parent: 'sg', message: 'Error: stopped', stopped: true, elapsed_ms: 50});
  assert.match(s1.textContent, /Stopped/);
  assert.doesNotMatch(s1.textContent, /Failed/);
  assert.ok(s1.classList.contains('failed'), 'it is drawn like a failed call');
  assert.equal(stopOf(s1).hidden, true, 'a finished call has nothing left to stop');
  assert.equal(s1.attrs['aria-keyshortcuts'], undefined);
  assert.match(said(), /L:web_fetch stopped/);

  // The keyboard: Delete on the row.
  assert.equal(key(stoppable[0], s2, 'Delete'), true);
  assert.deepEqual(stops, ['s1', 's2']);
  assert.equal(key(stoppable[0], sRoot, 'Delete'), false, 'the group itself is not stopped from here');

  // Already finished on the server: the button comes back, nothing is shown as stopped.
  answer = {stopped: false};
  stopTurn.accept({type: 'task_done', id: 's3', parent: 'sg', ok: true, elapsed_ms: 5});
  assert.equal(stopOf(s3).hidden, true);
  // A failed request restores the button and says so.
  answer = new Error('offline');
  stopTurn.accept({type: 'task_start', id: 's4', parent: 'sg', name: 'web_search', status: 'running'});
  const s4 = byRole(stoppable[0], 'treeitem')[4];
  stopOf(s4).listeners.click({stopPropagation() {}});
  await settle();
  assert.equal(stopOf(s4).text, 'Stop');
  assert.equal(stopOf(s4).attrs['aria-disabled'], undefined);
  assert.match(said(), /Could not stop L:web_search/);
  // A turn with no stop handler (an older server, a phone without rights) shows no button.
  const plain = [];
  const none = context.tree.turn({insert: node => plain.push(node)});
  none.accept({type: 'task_start', id: 'pg', parent: null, model: 'm'});
  none.accept({type: 'task_start', id: 'p1', parent: 'pg', name: 'recall', status: 'running'});
  assert.equal(stopOf(byRole(plain[0], 'treeitem')[1]).hidden, true);
  assert.equal(key(plain[0], byRole(plain[0], 'treeitem')[1], 'Delete'), false);
}

// A completed group stops its own clock even while the parent turn is open.
{
  const stoppedTimers = [];
  const group = context.tree.create({
    now: () => now, every: () => 1, stop: id => stoppedTimers.push(id),
  });
  group.setId('clock');
  group.add({id: 'timed', name: 'recall', status: 'running'});
  group.update({type: 'task_done', id: 'timed', parent: 'clock', elapsed_ms: 250});
  group.update({type: 'task_done', id: 'clock', elapsed_ms: 600});
  assert.deepEqual(stoppedTimers, [1]);
  assert.equal(byClass(group.node, 'agent-tree-time')[0].textContent, '0.6s');
}

// ---------------------------------------------------------------- the end of the turn
const fresh = [];
const second = context.tree.turn({insert: node => fresh.push(node)});
second.accept({type: 'task_start', id: 'h', parent: null, model: 'm'});
second.accept({type: 'task_start', id: 'x', parent: 'h', name: 'recall'});
second.accept({type: 'task_start', id: 'y', parent: 'h', name: 'memory_list'});
second.accept({type: 'task_done', id: 'x', parent: 'h', ok: true, elapsed_ms: 10});
second.finish();
const [secondRoot, , y] = byRole(fresh[0], 'treeitem');
assert.equal(secondRoot.attrs['aria-expanded'], 'false', 'a finished turn folds the tree');
assert.match(y.textContent, /Interrupted/, 'a call that never finished says so');
assert.match(secondRoot.textContent, /Ran 2 tools in parallel/);

// A reader's own choice survives the fold: the first tree was toggled by hand.
turn.finish();
assert.equal(root.attrs['aria-expanded'], 'true', 'a tree the reader opened stays open');
// Multiple delegated children share one live root and keep distinct task rows.
{
  const inserted = [];
  const delegated = context.tree.turn({insert: node => inserted.push(node), hint: args => args.task});
  delegated.accept({type: 'task_start', id: 'dg', parent: null, kind: 'delegated', model: 'm', total: 2});
  for (const [id, task] of [['d1', 'First analysis'], ['d2', 'Second analysis']]) {
    delegated.accept({type: 'task_start', id, parent: 'dg', name: 'delegate_analysis', args: {task}});
    delegated.accept({type: 'task_progress', id, parent: 'dg', status: 'running'});
  }
  assert.equal(inserted.length, 1);
  const [group, first, second] = byRole(inserted[0], 'treeitem');
  assert.match(group.textContent, /Delegated 2 analysis tasks/);
  assert.match(first.textContent, /First analysis/);
  assert.match(second.textContent, /Second analysis/);
  assert.match(first.textContent, /Running/);
  assert.match(second.textContent, /Running/);
  delegated.finish();
}
console.log('agent tree ok');
