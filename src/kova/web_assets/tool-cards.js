/* Tool transcripts are untrusted text. Keep their DOM separate from Markdown.

   One turn's consecutive tool calls fold into one row: "Ran 4 tools · 3.2s" with
   a status mark (running / ok / failed). The row opens by itself while the turn
   runs and folds once it finishes, unless the reader opened it first (#186).
   Each call is a row with its name, a short argument summary, its duration and
   its own disclosure for the arguments and output; long output stays clipped
   behind "Show all". Failed calls are repeated above the fold, so a failure is
   never hidden by a collapsed group. */
const KovaToolCards = (() => {
  const LIMIT = 6000;
  const STATUS = {
    running: 'Running',
    ok: 'Done',
    failed: 'Failed',
    unknown: 'Recorded',
  };
  let nextId = 0;
  function node(tag, cls, text) {
    const result = document.createElement(tag);
    result.className = cls;
    if (text !== undefined) result.textContent = text;
    return result;
  }
  function markClass(status) {
    if (status === 'ok') return 'icon i-check tool-card-mark ok';
    if (status === 'failed') return 'icon i-triangle-alert tool-card-mark failed';
    if (status === 'running') return 'tool-card-mark running';
    return 'tool-card-mark unknown';
  }
  function paintMark(mark, status) {
    mark.className = markClass(status);
  }
  function titleCase(name) {
    const words = String(name || 'tool').replaceAll('_', ' ');
    return words.charAt(0).toUpperCase() + words.slice(1);
  }
  function lineCount(text) { return text ? text.split('\n').length : 0; }
  function duration(ms) {
    if (ms == null) return 'timing unavailable';
    const seconds = Math.max(0, ms) / 1000;
    if (seconds < 60) return `${seconds.toFixed(1)}s`;
    const whole = Math.round(seconds);
    return `${Math.floor(whole / 60)}m ${String(whole % 60).padStart(2, '0')}s`;
  }
  function create({now = () => performance.now(), saved = false} = {}) {
    const uid = `tool-card-${++nextId}`;
    const card = node('div', 'tool-card');
    card.setAttribute('role', 'group');
    card.setAttribute('aria-label', 'Tool calls');
    const summary = node('button', 'tool-card-summary');
    summary.type = 'button';
    summary.setAttribute('aria-expanded', 'false');
    summary.setAttribute('aria-controls', `${uid}-rows`);
    const summaryMark = node('span', markClass(saved ? 'unknown' : 'running'));
    summaryMark.setAttribute('aria-hidden', 'true');
    const summaryText = node('span', 'tool-card-summary-text', 'Tool calls');
    const summaryChevron = node('span', 'icon i-chevron-right tool-card-chevron');
    summaryChevron.setAttribute('aria-hidden', 'true');
    summary.append(summaryMark, summaryText, summaryChevron);
    const rows = node('div', 'tool-card-rows');
    rows.setAttribute('id', `${uid}-rows`);
    rows.hidden = true;
    const failures = node('div', 'tool-card-failures');
    const announcement = node('span', 'sr-only');
    announcement.setAttribute('role', 'status');
    card.append(summary, rows, failures, announcement);

    const tools = [];
    let expanded = false, touched = false;

    function show(open, byUser = false) {
      if (byUser) touched = true;
      expanded = open;
      rows.hidden = !open;
      summary.setAttribute('aria-expanded', String(open));
      card.classList.toggle('open', open);
    }
    summary.onclick = () => show(!expanded, true);

    function groupStatus() {
      if (tools.some(tool => tool.status === 'running')) return 'running';
      if (tools.some(tool => tool.status === 'failed')) return 'failed';
      return tools.every(tool => tool.status === 'ok') ? 'ok' : 'unknown';
    }
    function announce(text) {
      if (announcement.textContent !== text) announcement.textContent = text;
    }
    function refresh() {
      if (!tools.length) return;
      const status = groupStatus();
      const failed = tools.filter(tool => tool.status === 'failed').length;
      const elapsed = saved ? null : tools.reduce((total, tool) => total + (tool.end ?? now()) - tool.start, 0);
      const count = `${tools.length} tool${tools.length === 1 ? '' : 's'}`;
      const verb = status === 'running' ? 'Running' : 'Ran';
      paintMark(summaryMark, status);
      summaryText.textContent = `${verb} ${count} · ${duration(elapsed)}${failed ? ` · ${failed} failed` : ''}`;
    }
    function add(event) {
      const index = tools.length;
      const step = node('div', 'tool-card-step');
      const title = node('button', 'tool-card-title');
      title.type = 'button';
      title.setAttribute('aria-expanded', 'false');
      title.setAttribute('aria-controls', `${uid}-step-${index}`);
      const mark = node('span', markClass(saved ? 'unknown' : 'running'));
      mark.setAttribute('aria-hidden', 'true');
      const label = node('span', 'tool-card-name', event.label || titleCase(event.name));
      const status = node('span', 'tool-card-status sr-only', saved ? 'Recorded' : 'Running');
      const time = node('span', 'tool-card-time', saved ? duration(null) : '…');
      const chevron = node('span', 'icon i-chevron-right tool-card-chevron');
      chevron.setAttribute('aria-hidden', 'true');
      title.append(mark, label);
      if (event.hint) title.append(node('span', 'tool-card-hint', event.hint));
      title.append(status, time, chevron);

      const body = node('div', 'tool-card-body');
      body.setAttribute('id', `${uid}-step-${index}`);
      body.hidden = true;
      title.onclick = () => {
        const open = body.hidden;
        body.hidden = !open;
        title.setAttribute('aria-expanded', String(open));
        step.classList.toggle('open', open);
      };
      const args = typeof event.args === 'string' ? event.args : JSON.stringify(event.args ?? {}, null, 2);
      if (args && args !== '{}' && args !== 'null') body.append(node('pre', 'tool-card-input', args));

      step.append(title, body);
      const tool = {name: event.name, id: event.id, start: now(), end: null,
                    status: saved ? 'unknown' : 'running', body, title, statusNode: status, time, step, mark};
      tools.push(tool); rows.append(step);
      if (!saved && !touched) show(true);
      refresh();
      return tool;
    }
    function addOutput(body, text, name) {
      const pre = node('pre', 'tool-card-output', text.slice(0, LIMIT));
      body.append(pre);
      if (text.length <= LIMIT) return;
      const more = node('div', 'tool-card-more');
      const all = node('button', 'tool-card-all');
      all.type = 'button';
      all.setAttribute('aria-expanded', 'false');
      const paintAll = open => {
        const lines = lineCount(text);
        all.textContent = open ? 'Show less' : `Show all (${lines} line${lines === 1 ? '' : 's'})`;
        all.setAttribute('aria-expanded', String(open));
      };
      paintAll(false);
      let open = false;
      all.onclick = () => {
        open = !open;
        pre.textContent = open ? text : text.slice(0, LIMIT);
        paintAll(open);
      };
      more.append(all);
      // The whole output can also be read beside the conversation (panes.js, #182).
      const panel = globalThis.KovaPanel;
      if (panel?.open) {
        const side = node('button', 'tool-card-all', 'Open in panel');
        side.type = 'button';
        side.setAttribute('aria-label', `Open the output of ${name || 'this tool'} in the side panel`);
        side.onclick = () => panel.open({title: `${name || 'Tool'} output`, node: node('pre', 'side-panel-output', text), returnFocus: side});
        more.append(side);
      }
      body.append(more);
    }
    function result(event) {
      const tool = tools.find(item => item.end === null && (event.id && item.id ? event.id === item.id : (!event.name || item.name === event.name)));
      if (!tool) return;
      tool.end = now(); tool.status = event.ok === false ? 'failed' : event.ok === true ? 'ok' : 'unknown';
      tool.step.classList.toggle('failed', tool.status === 'failed');
      paintMark(tool.mark, tool.status);
      tool.statusNode.textContent = STATUS[tool.status];
      tool.time.textContent = duration(saved ? null : tool.end - tool.start);
      const output = String(event.output ?? event.summary ?? '');
      if (output) addOutput(tool.body, output, tool.name);
      if (event.file_change) tool.body.append(KovaDiff.file(event.file_change));
      if (tool.status === 'failed') {
        failures.append(node('p', 'tool-card-error', `${tool.name}: ${output.slice(0, 300) || 'Tool failed.'}`));
        announce(`${tool.name} failed.`);
      }
      refresh();
    }
    function finish() {
      if (!tools.length) return;
      for (const tool of tools.filter(item => item.end === null)) {
        tool.end = now(); tool.status = 'failed';
        tool.step.classList.toggle('failed', true);
        paintMark(tool.mark, 'failed');
        tool.statusNode.textContent = 'Interrupted';
        tool.time.textContent = duration(saved ? null : tool.end - tool.start);
        failures.append(node('p', 'tool-card-error', `${tool.name}: Interrupted`));
      }
      refresh();
      if (saved) return;
      announce(summaryText.textContent);
      // A turn that ends folds its tools away, unless the reader opened them.
      if (!touched) show(false);
    }
    return {node: card, add, result, finish};
  }
  return {create, duration};
})();
