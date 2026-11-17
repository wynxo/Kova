/* Tool transcripts are untrusted text. Keep their DOM separate from Markdown. */
const KovaToolCards = (() => {
  const LIMIT = 6000;
  function node(tag, cls, text) {
    const result = document.createElement(tag);
    result.className = cls;
    if (text !== undefined) result.textContent = text;
    return result;
  }
  function duration(ms) { return ms == null ? 'timing unavailable' : `${(Math.max(0, ms) / 1000).toFixed(1)}s`; }
  function create({now = () => performance.now(), saved = false} = {}) {
    const card = node('section', 'tool-card'), group = node('details', 'tool-card-group');
    const summary = node('summary', 'tool-card-summary'), rows = node('div', 'tool-card-rows');
    const failures = node('div', 'tool-card-failures');
    const announcement = node('span', 'sr-only'); announcement.setAttribute('role', 'status');
    const tools = [];
    group.append(summary, rows); card.append(group, failures, announcement);
    function refresh() {
      const running = tools.filter(tool => tool.status === 'running').length;
      const failed = tools.filter(tool => tool.status === 'failed').length;
      const elapsed = saved ? null : tools.reduce((total, tool) => total + (tool.end ?? now()) - tool.start, 0);
      summary.textContent = `${running ? 'Running' : 'Ran'} ${tools.length} tool${tools.length === 1 ? '' : 's'} · ${duration(elapsed)}${failed ? ` · ${failed} failed` : ''}`;
    }
    function add(event) {
      const details = node('details', 'tool-card-step'), title = node('summary', 'tool-card-title');
      const label = node('span', '', event.label || (event.name || 'Tool').replaceAll('_', ' ').replace(/^./, letter => letter.toUpperCase())), status = node('span', 'tool-card-status');
      status.setAttribute('role', 'status');
      const time = node('span', 'tool-card-time');
      title.append(label, status, time);
      if (event.hint) title.append(node('span', 'tool-card-time', event.hint));
      details.append(title);
      const args = typeof event.args === 'string' ? event.args : JSON.stringify(event.args ?? {}, null, 2);
      details.append(node('pre', 'tool-card-input', args));
      const tool = {name: event.name, id: event.id, start: now(), end: null, status: saved ? 'unknown' : 'running', details, statusNode: status, time};
      status.textContent = saved ? 'Recorded' : 'Running';
      time.textContent = saved ? duration(null) : '…';
      tools.push(tool); rows.append(details); announcement.textContent = `${tool.name}: ${status.textContent}`; refresh();
      return tool;
    }
    function result(event) {
      const tool = tools.find(item => item.end === null && (event.id && item.id ? event.id === item.id : (!event.name || item.name === event.name)));
      if (!tool) return;
      tool.end = now(); tool.status = event.ok === false ? 'failed' : event.ok === true ? 'ok' : 'unknown';
      tool.details.classList.toggle('failed', tool.status === 'failed');
      tool.statusNode.textContent = tool.status === 'ok' ? '✓ OK' : tool.status === 'failed' ? '✗ Failed' : 'Recorded';
      announcement.textContent = `${tool.name}: ${tool.statusNode.textContent}`;
      tool.time.textContent = duration(saved ? null : tool.end - tool.start);
      const output = String(event.output ?? event.summary ?? '');
      const pre = node('pre', 'tool-card-output', output.slice(0, LIMIT)); tool.details.append(pre);
      if (output.length > LIMIT) {
        const all = node('button', 'tool-card-all', 'Show all'); all.type = 'button';
        all.onclick = () => { pre.textContent = output; all.remove(); }; tool.details.append(all);
      }
      if (event.file_change) tool.details.append(KovaDiff.file(event.file_change));
      if (tool.status === 'failed') failures.append(node('p', 'tool-card-error', `${tool.name}: ${output.slice(0, 300) || 'Tool failed.'}`));
      refresh();
    }
    function finish() {
      for (const tool of tools.filter(item => item.end === null)) {
        tool.end = now(); tool.status = 'failed'; tool.statusNode.textContent = 'Interrupted';
        tool.time.textContent = duration(saved ? null : tool.end - tool.start);
        failures.append(node('p', 'tool-card-error', `${tool.name}: Interrupted`));
      }
      refresh();
    }
    return {node: card, add, result, finish};
  }
  return {create, duration};
})();
