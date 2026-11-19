/* Parallel work as a live tree (#192).

   When the model asks for several read-only tools in one step, the server runs
   them at once and streams task events with parent ids: task_start (the group,
   then one per call), task_progress, task_done and task_error. This file turns
   those into one tree in the transcript:

     Running 3 tools in parallel · local-model · 1.2s
       ├ Read file  src/app.py    local-model  Done     0.4s
       ├ Search     "TODO"        local-model  Running  1.2s
       └ Web fetch  example.com   local-model  Waiting

   Each node shows its label, model, status and elapsed time, and expands to its
   tool row (the same KovaToolCards component as every other tool call). The tree
   opens while the turn runs and folds when it ends, unless the reader opened or
   closed it first.

   Keyboard: it is an ARIA tree (role tree / treeitem / group, aria-expanded,
   aria-level) with one tab stop. Up and Down move, Right opens or steps in, Left
   closes or steps out, Home and End jump, Enter and Space toggle. Tool output is
   untrusted text and only ever lands in textContent. */
const KovaAgentTree = (() => {
  const STATUS = {queued: 'Waiting', running: 'Running', ok: 'Done', failed: 'Failed', interrupted: 'Interrupted'};
  let nextId = 0;
  function node(tag, cls, text) {
    const result = document.createElement(tag);
    if (cls) result.className = cls;
    if (text !== undefined) result.textContent = text;
    return result;
  }
  function markClass(status) {
    if (status === 'ok') return 'icon i-check tool-card-mark ok';
    if (status === 'failed' || status === 'interrupted') return 'icon i-triangle-alert tool-card-mark failed';
    if (status === 'running') return 'tool-card-mark running';
    return 'tool-card-mark unknown';
  }
  function titleCase(name) {
    const words = String(name || 'tool').replaceAll('_', ' ');
    return words.charAt(0).toUpperCase() + words.slice(1);
  }
  function duration(ms) {
    if (ms == null) return '';
    if (typeof KovaToolCards !== 'undefined') return KovaToolCards.duration(ms);
    return `${(Math.max(0, ms) / 1000).toFixed(1)}s`;
  }

  // One tree item: a row with its own disclosure, and a group for its children.
  function item(level, uid) {
    const element = node('div', 'agent-tree-node');
    element.setAttribute('role', 'treeitem');
    element.setAttribute('aria-level', String(level));
    element.setAttribute('aria-expanded', 'false');
    element.setAttribute('tabindex', '-1');
    const row = node('div', 'agent-tree-row');
    const mark = node('span', markClass('queued'));
    mark.setAttribute('aria-hidden', 'true');
    const label = node('span', 'agent-tree-label');
    label.setAttribute('id', `${uid}-label`);
    const hint = node('span', 'agent-tree-hint');
    const model = node('span', 'agent-tree-model');
    const status = node('span', 'agent-tree-status');
    status.setAttribute('id', `${uid}-status`);
    const time = node('span', 'agent-tree-time');
    const chevron = node('span', 'icon i-chevron-right agent-tree-chevron');
    chevron.setAttribute('aria-hidden', 'true');
    row.append(mark, label, hint, model, status, time, chevron);
    const children = node('div', 'agent-tree-children');
    children.setAttribute('role', 'group');
    children.hidden = true;
    element.setAttribute('aria-labelledby', `${uid}-label ${uid}-status`);
    element.append(row, children);
    const entry = {element, row, mark, label, hint, model, status, time, children,
                   state: 'queued', start: null, end: null, touched: false};
    entry.open = (open, byUser = false) => {
      if (byUser) entry.touched = true;
      children.hidden = !open;
      element.setAttribute('aria-expanded', String(open));
      element.classList.toggle('open', open);
    };
    entry.paint = (now) => {
      mark.className = markClass(entry.state);
      status.textContent = STATUS[entry.state] || '';
      element.classList.toggle('failed', entry.state === 'failed' || entry.state === 'interrupted');
      time.textContent = entry.start == null ? '' : duration((entry.end ?? now()) - entry.start);
    };
    return entry;
  }

  function create({label = titleCase, hint = () => '', model = '', now = () => performance.now(),
                   every = (fn, ms) => setInterval(fn, ms), stop = id => clearInterval(id)} = {}) {
    const uid = `agent-tree-${++nextId}`;
    const tree = node('div', 'agent-tree');
    tree.setAttribute('role', 'tree');
    tree.setAttribute('aria-label', 'Parallel tasks');
    const root = item(1, `${uid}-root`);
    root.element.setAttribute('tabindex', '0');
    root.state = 'running'; root.start = now();
    root.model.textContent = model;
    const announcement = node('span', 'sr-only');
    announcement.setAttribute('role', 'status');
    tree.append(root.element, announcement);
    const tasks = new Map();
    let total = 0, done = 0, failed = 0, finished = false, timer = null;

    function rootLabel() {
      const count = `${total} tool${total === 1 ? '' : 's'}`;
      const verb = finished || done === total && total ? 'Ran' : 'Running';
      root.label.textContent = `${verb} ${count} in parallel${failed ? ` · ${failed} failed` : ''}`;
    }
    function paintAll() {
      root.paint(now);
      for (const task of tasks.values()) task.paint(now);
    }
    function tick() {
      paintAll();
      const busy = !finished && (root.state === 'running' || [...tasks.values()].some(t => t.state === 'running'));
      if (!busy && timer != null) { stop(timer); timer = null; }
    }
    function startTimer() { if (timer == null && !finished) timer = every(tick, 1000); }
    function announce(text) { if (announcement.textContent !== text) announcement.textContent = text; }

    // Roving focus: exactly one tree item is in the tab order.
    function visible() {
      const out = [root];
      if (!root.children.hidden) for (const task of tasks.values()) out.push(task);
      return out;
    }
    function focus(entry) {
      for (const candidate of [root, ...tasks.values()]) candidate.element.setAttribute('tabindex', candidate === entry ? '0' : '-1');
      entry.element.focus?.();
    }
    function entryFor(element) {
      if (element === root.element) return root;
      return [...tasks.values()].find(task => task.element === element) || null;
    }
    tree.addEventListener?.('keydown', event => {
      const entry = entryFor(event.target);
      if (!entry) return; // keys inside a tool card belong to its own buttons
      const list = visible(), index = list.indexOf(entry);
      const expanded = !entry.children.hidden;
      let handled = true;
      if (event.key === 'ArrowDown') focus(list[Math.min(list.length - 1, index + 1)]);
      else if (event.key === 'ArrowUp') focus(list[Math.max(0, index - 1)]);
      else if (event.key === 'Home') focus(list[0]);
      else if (event.key === 'End') focus(list[list.length - 1]);
      else if (event.key === 'ArrowRight') {
        if (!expanded) entry.open(true, true);
        else if (entry === root && tasks.size) focus(list[1]);
      } else if (event.key === 'ArrowLeft') {
        if (expanded && entry !== root) entry.open(false, true);
        else if (entry !== root) focus(root);
        else if (expanded) entry.open(false, true);
      } else if (event.key === 'Enter' || event.key === ' ') entry.open(!expanded, true);
      else handled = false;
      if (handled) event.preventDefault();
    });
    function clickable(entry) {
      entry.row.onclick = () => { entry.open(entry.children.hidden, true); focus(entry); };
    }
    clickable(root);

    function add(event) {
      total += 1;
      const task = item(2, `${uid}-${tasks.size}`);
      task.name = event.name;
      task.label.textContent = label(event.name || event.label);
      task.hint.textContent = hint(event.args);
      task.model.textContent = event.model || model;
      task.state = event.status === 'running' ? 'running' : 'queued';
      if (task.state === 'running') task.start = now();
      // The call's own tool row, filled by its tool_call and tool_result.
      task.card = typeof KovaToolCards !== 'undefined' ? KovaToolCards.create({now}) : null;
      if (task.card) task.children.append(task.card.node);
      clickable(task);
      tasks.set(event.id, task);
      root.children.append(task.element);
      if (!root.touched) root.open(true);
      rootLabel(); task.paint(now); root.paint(now);
      startTimer();
      return task;
    }
    function update(event) {
      if (event.id === root.id || event.parent == null) {
        if (event.type === 'task_progress') {
          done = event.done ?? done; failed = event.failed ?? failed;
        } else if (event.type === 'task_done' || event.type === 'task_error') {
          root.end = now(); root.state = event.ok === false || event.type === 'task_error' ? 'failed' : 'ok';
          done = event.done ?? done; failed = event.failed ?? failed;
          if (event.elapsed_ms != null) root.end = root.start + event.elapsed_ms;
          announce(`${total} parallel tools finished${failed ? `, ${failed} failed` : ''}.`);
        }
        rootLabel(); root.paint(now);
        return;
      }
      const task = tasks.get(event.id);
      if (!task) return;
      if (event.type === 'task_progress' && event.status === 'running') {
        task.state = 'running'; task.start = now();
      } else if (event.type === 'task_done' || event.type === 'task_error') {
        if (task.start == null) task.start = now();
        task.end = event.elapsed_ms != null ? task.start + event.elapsed_ms : now();
        task.state = event.type === 'task_error' || event.ok === false ? 'failed' : 'ok';
        if (task.state === 'failed') announce(`${task.label.textContent} failed.`);
      }
      task.paint(now);
      startTimer();
    }
    function toolCall(event) {
      tasks.get(event.id)?.card?.add({...event, label: label(event.name), hint: hint(event.args)});
    }
    function toolResult(event) {
      tasks.get(event.id)?.card?.result(event);
    }
    function finish() {
      if (finished) return;
      finished = true;
      if (timer != null) { stop(timer); timer = null; }
      for (const task of tasks.values()) {
        if (task.state === 'queued' || task.state === 'running') {
          task.state = 'interrupted';
          if (task.start != null) task.end = now();
        }
        task.card?.finish();
        if (!task.touched) task.open(false);
      }
      if (root.state === 'running') { root.state = tasks.size && [...tasks.values()].every(t => t.state === 'ok') ? 'ok' : 'interrupted'; root.end = now(); }
      rootLabel(); paintAll();
      // A finished turn folds its tree away, unless the reader chose otherwise.
      if (!root.touched) root.open(false);
    }
    root.id = null;
    return {node: tree, add, update, toolCall, toolResult, finish,
            setId(id) { root.id = id; }, has: id => tasks.has(id)};
  }

  // One turn's router: app.js hands every stream event here first, and this
  // keeps the ones that belong to a tree (task events, and the tool_call and
  // tool_result of a grouped call).
  function turn({insert, label, hint, now} = {}) {
    const trees = new Map(), owner = new Map(), all = [];
    return {
      accept(event) {
        const kind = event?.type;
        if (kind === 'task_start' && event.parent == null) {
          const tree = create({label, hint, model: event.model || '', now});
          tree.setId(event.id);
          trees.set(event.id, tree); all.push(tree);
          insert?.(tree.node);
          return true;
        }
        if (kind === 'task_start') {
          const tree = trees.get(event.parent);
          if (!tree) return false;
          tree.add(event); owner.set(event.id, tree);
          return true;
        }
        if (kind === 'task_progress' || kind === 'task_done' || kind === 'task_error') {
          const tree = trees.get(event.id) || owner.get(event.id);
          tree?.update(event);
          return Boolean(tree);
        }
        if ((kind === 'tool_call' || kind === 'tool_result') && event.id && owner.has(event.id)) {
          const tree = owner.get(event.id);
          if (kind === 'tool_call') tree.toolCall(event); else tree.toolResult(event);
          return true;
        }
        return false;
      },
      finish() { for (const tree of all) tree.finish(); },
    };
  }
  return {create, turn};
})();
if (typeof window !== 'undefined') window.KovaAgentTree = KovaAgentTree;
