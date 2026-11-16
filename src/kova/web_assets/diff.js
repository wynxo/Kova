/* Unified checkpoint diffs. Untrusted paths and source are text, never HTML. */
const KovaDiff = (() => {
  function node(tag, cls, text) {
    const element = document.createElement(tag);
    element.className = cls;
    if (text !== undefined) element.textContent = text;
    return element;
  }
  function rows(diff) {
    let old = 0, next = 0, inHunk = false;
    return String(diff || '').split('\n').filter(Boolean).map(text => {
      const hunk = /^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@/.exec(text);
      if (hunk) { old = Number(hunk[1]); next = Number(hunk[2]); inHunk = true; }
      const kind = hunk ? 'hunk' : !inHunk ? 'meta' : text[0] === '+' ? 'add' : text[0] === '-' ? 'del' : text[0] === ' ' ? 'context' : 'meta';
      const numbered = ['add', 'del', 'context'].includes(kind);
      return {text, kind, old: numbered && kind !== 'add' ? old++ : '', next: numbered && kind !== 'del' ? next++ : ''};
    });
  }
  function render(preview) {
    const view = node('div', 'file-diff');
    view.setAttribute('role', 'region');
    view.setAttribute('aria-label', 'Unified diff');
    view.setAttribute('tabindex', '0');
    for (const line of rows(preview.diff)) {
      const row = node('div', `file-diff-row file-diff-${line.kind}`);
      row.append(node('span', 'file-diff-number', String(line.old)), node('span', 'file-diff-number', String(line.next)), node('code', 'file-diff-source', line.text));
      view.append(row);
    }
    if (!preview.diff) view.append(node('p', '', 'No textual changes.'));
    if (preview.truncated) view.append(node('p', '', 'Preview truncated; not all changes are shown.'));
    return view;
  }
  function file(preview) {
    const details = node('details', 'file-diff-file');
    details.append(node('summary', '', `${preview.path} · ${preview.status} · Changes so far this turn`), render(preview));
    return details;
  }
  return {rows, render, file};
})();
