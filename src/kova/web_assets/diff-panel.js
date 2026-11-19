/* One changed file's full diff in the side panel, with Undo or Keep per hunk (#190).
 *
 * The server splits the change into hunks (kova/hunks.py) and says, for each,
 * whether it is still in the file. Undo sends back the file hash this view was
 * built from, so the server refuses when the file moved on in the meantime.
 * File names and source are untrusted: everything here is text, never HTML.
 *
 *   KovaDiffPanel.open({chatId, checkpointId, path, request, returnFocus, onChange})
 *   KovaDiffPanel.render(view, handlers)   the diff alone, for tests
 */
const KovaDiffPanel = (() => {
  const CONTEXT = 3;          // unchanged lines kept on each side of a hunk
  const STATE_LABELS = {applied: '', kept: 'Kept', undone: 'Undone', changed: 'Edited since', missing: 'File removed'};
  const STATE_HINTS = {
    applied: 'still in the file',
    kept: 'kept',
    undone: 'undone',
    changed: 'edited since, so it cannot be undone here',
    missing: 'the file was removed',
  };

  function node(tag, cls, text) {
    const element = document.createElement(tag);
    if (cls) element.className = cls;
    if (text !== undefined) element.textContent = text;
    return element;
  }
  function button(label, cls, aria) {
    const element = node('button', `btn small ${cls}`, label);
    element.type = 'button';
    if (aria) element.setAttribute('aria-label', aria);
    return element;
  }
  const plural = (count, word) => `${count} ${word}${count === 1 ? '' : 's'}`;

  function row(kind, old, next, text) {
    const line = node('div', `file-diff-row file-diff-${kind}`);
    const sign = kind === 'add' ? '+' : kind === 'del' ? '-' : ' ';
    line.append(node('span', 'file-diff-number', old === '' ? '' : String(old)),
                node('span', 'file-diff-number', next === '' ? '' : String(next)),
                node('span', 'diff-sign', sign),
                node('code', 'file-diff-source', text));
    return line;
  }
  function sameRows(segment, from, to) {
    const rows = [];
    for (let index = from; index < to; index++) {
      rows.push(row('context', segment.old + index, segment.new + index, segment.lines[index]));
    }
    return rows;
  }

  // An unchanged run keeps CONTEXT lines next to each hunk; the middle folds
  // behind a button. The run at the top of the file only keeps its tail, the
  // one at the bottom only its head.
  function sameSegment(segment, first, last) {
    const count = segment.lines.length;
    const head = first ? 0 : CONTEXT, tail = last ? 0 : CONTEXT;
    const parts = [];
    if (count - head - tail < 2) return sameRows(segment, 0, count);
    parts.push(...sameRows(segment, 0, head));
    const hidden = count - head - tail;
    const more = node('button', 'diff-expand', `Show ${plural(hidden, 'more line')}`);
    more.type = 'button';
    more.setAttribute('aria-label', `Show ${plural(hidden, 'unchanged line')}, lines ${segment.new + head} to ${segment.new + head + hidden - 1}`);
    more.addEventListener('click', () => {
      const shown = node('div', 'diff-expanded');
      shown.setAttribute('tabindex', '-1');
      shown.setAttribute('aria-label', `${plural(hidden, 'unchanged line')}`);
      shown.append(...sameRows(segment, head, head + hidden));
      more.replaceWith(shown);
      shown.focus({preventScroll: true});
    });
    parts.push(more, ...sameRows(segment, count - tail, count));
    return parts;
  }

  function hunkSection(hunk, index, total, handlers) {
    const state = handlers.kept?.has(hunk.id) && hunk.state === 'applied' ? 'kept' : hunk.state;
    const section = node('section', `diff-hunk diff-hunk-${state}`);
    section.dataset.hunk = hunk.id;
    section.setAttribute('tabindex', '0');
    section.setAttribute('role', 'group');
    const where = hunk.added.length ? `line ${hunk.new}` : `after line ${Math.max(hunk.new - 1, 0)}`;
    const name = `Change ${index + 1} of ${total}`;
    section.setAttribute('aria-label', `${name}, ${where}: ${plural(hunk.removed.length, 'line')} removed, ${plural(hunk.added.length, 'line')} added, ${STATE_HINTS[state] || state}`);

    const head = node('div', 'diff-hunk-head');
    head.append(node('code', 'diff-hunk-header', hunk.header));
    if (STATE_LABELS[state]) head.append(node('span', `diff-hunk-state diff-state-${state}`, STATE_LABELS[state]));
    if (state === 'applied' && handlers.undo) {
      const actions = node('span', 'diff-hunk-actions');
      const keep = button('Keep', 'quiet', `Keep ${name.toLowerCase()}`);
      const undo = button('Undo', '', `Undo ${name.toLowerCase()}, ${where}`);
      keep.addEventListener('click', () => handlers.keep(hunk, index));
      undo.addEventListener('click', () => handlers.undo(hunk, index, [keep, undo]));
      actions.append(keep, undo);
      head.append(actions);
    }
    section.append(head);
    hunk.removed.forEach((text, offset) => section.append(row('del', hunk.old + offset, '', text)));
    hunk.added.forEach((text, offset) => section.append(row('add', '', hunk.new + offset, text)));
    return section;
  }

  // Up and Down move between hunks while one has focus; Tab still walks every button.
  function arrows(view) {
    view.addEventListener('keydown', (event) => {
      if (event.key !== 'ArrowDown' && event.key !== 'ArrowUp') return;
      const current = event.target;
      if (!current?.classList?.contains('diff-hunk')) return;
      const all = Array.from(view.querySelectorAll('.diff-hunk'));
      const next = all[all.indexOf(current) + (event.key === 'ArrowDown' ? 1 : -1)];
      if (!next) return;
      event.preventDefault();
      next.focus();
    });
  }

  function render(view, handlers = {}) {
    const body = node('div', 'file-diff diff-panel-diff');
    body.setAttribute('role', 'region');
    body.setAttribute('aria-label', `Diff of ${view.path}`);
    const segments = view.segments || [];
    const total = segments.filter(segment => segment.type === 'hunk').length;
    let index = 0;
    segments.forEach((segment, position) => {
      if (segment.type === 'hunk') body.append(hunkSection(segment, index++, total, handlers));
      else body.append(...sameSegment(segment, position === 0, position === segments.length - 1));
    });
    if (!total) body.append(node('p', 'diff-panel-note', 'No textual changes.'));
    arrows(body);
    return body;
  }

  function open(options) {
    const {chatId, checkpointId, path, request} = options;
    const base = `/chats/${encodeURIComponent(chatId)}/checkpoints/${encodeURIComponent(checkpointId)}`;
    const wrap = node('div', 'diff-panel');
    const meta = node('div', 'diff-panel-meta');
    const status = node('p', 'diff-panel-status');
    status.setAttribute('role', 'status');
    const content = node('div', 'diff-panel-content');
    wrap.append(meta, status, content);
    const kept = new Set();
    let view = null;

    const say = (text, error = false) => { status.textContent = text; status.classList.toggle('is-error', error); };
    function summary() {
      const counts = (view?.segments || []).filter(segment => segment.type === 'hunk');
      const open = counts.filter(hunk => hunk.state === 'applied' && !kept.has(hunk.id)).length;
      meta.replaceChildren(node('span', 'diff-panel-path', path),
                           node('span', 'change-stats', `+${view?.additions || 0} −${view?.deletions || 0}`),
                           node('span', 'diff-panel-count', `${plural(counts.length, 'change')}${open ? `, ${open} to review` : ''}`));
    }
    function retry(text) {
      const again = button('Reload diff', '', `Reload the diff of ${path}`);
      again.addEventListener('click', () => load());
      content.replaceChildren(node('p', 'diff-panel-note', text), again);
    }
    function draw(focusHunk) {
      summary();
      if (!view.supported) {
        content.replaceChildren(node('p', 'diff-panel-note', view.reason || 'This file has no line-by-line view.'));
        return;
      }
      content.replaceChildren(render(view, {kept, keep, undo}));
      if (focusHunk) content.querySelector(`[data-hunk="${CSS.escape(focusHunk)}"]`)?.focus();
    }
    async function load(focusHunk) {
      say('');
      content.replaceChildren(node('p', 'diff-panel-note', 'Loading the diff…'));
      try {
        view = await request(`${base}/hunks?path=${encodeURIComponent(path)}`);
        draw(focusHunk);
      } catch (error) {
        retry(error.message || 'The diff could not be loaded.');
      }
    }
    function keep(hunk, index) {
      kept.add(hunk.id);
      draw(hunk.id);
      say(`Kept change ${index + 1}.`);
    }
    async function undo(hunk, index, buttons) {
      for (const control of buttons) control.disabled = true;
      say(`Undoing change ${index + 1}…`);
      try {
        const result = await request(`${base}/hunks/undo`, {method: 'POST', body: JSON.stringify({
          path, hunk: hunk.id, expected_hash: view.current_hash})});
        if (result.view) { view = result.view; draw(hunk.id); } else await load(hunk.id);
        say(`Undid change ${index + 1}. Saved as change set ${result.checkpoint_id}, so Restore files can bring it back.`);
        options.onChange?.(result);
      } catch (error) {
        for (const control of buttons) control.disabled = false;
        say(`${error.message || 'The change could not be undone.'}`, true);
        const again = button('Reload diff', '', `Reload the diff of ${path}`);
        again.addEventListener('click', () => load());
        status.append(' ', again);
      }
    }

    window.KovaPanel.open({title: `Diff · ${path}`, node: wrap, returnFocus: options.returnFocus});
    summary();
    return load();
  }

  return {open, render};
})();
