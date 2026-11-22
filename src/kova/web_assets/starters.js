/* Start screen suggestions (issue #194).
 *
 * Three quiet prompts under the greeting on an empty chat. Choosing one only
 * fills the message box, so it stays editable; nothing is sent until Send.
 * The list is a shared UI preference (``starters`` in preferences.json), so it
 * follows the user to another client and travels in `kova prefs export`. A
 * browser that cannot reach the server keeps its local copy.
 *
 * The editor lives here so app.js only wires two events: one to fill the
 * composer, one to open the dialog. No network is needed for the DOM path, so
 * tests/web_starters.mjs can exercise it with a small DOM.
 */
(() => {
  'use strict';

  const KEY = 'kova-starters';
  const MAX = 4;
  const LIMIT = 120;
  const DEFAULT = [
    'Plan a small project with me',
    'Summarise a file I send',
    'Help me write a tricky email',
  ];
  const doc = typeof document !== 'undefined' ? document : null;
  let values = stored();
  let revision = 0;

  // Trimmed, single-line, de-duplicated and bounded exactly as the server
  // validates it, so a local edit can never be refused after it was shown.
  function clean(list) {
    const out = [];
    for (const item of Array.isArray(list) ? list : []) {
      if (typeof item !== 'string') continue;
      const text = Array.from(item.replace(/\s+/g, ' ').trim()).slice(0, LIMIT).join('');
      if (text && !out.includes(text)) out.push(text);
      if (out.length >= MAX) break;
    }
    return out;
  }

  function stored() {
    try {
      const saved = JSON.parse(localStorage.getItem(KEY) || 'null');
      return Array.isArray(saved) ? clean(saved) : [...DEFAULT];
    } catch (_) {
      return [...DEFAULT];
    }
  }

  function remember() {
    try { localStorage.setItem(KEY, JSON.stringify(values)); } catch (_) {}
  }

  function announce() {
    try {
      if (doc && typeof CustomEvent === 'function') {
        doc.dispatchEvent(new CustomEvent('kova:starters', {detail: {starters: current()}}));
      }
    } catch (_) {}
  }

  function current() { return [...values]; }

  function request(url, options) {
    if (typeof fetch !== 'function') return null;
    try { return fetch(url, options); } catch (_) { return null; }
  }

  // The server document is the shared copy; a valid list always wins over the
  // local one, because a change made in another window should arrive here.
  function adopt(payload) {
    if (!payload || !Array.isArray(payload.starters)) return;
    const next = clean(payload.starters);
    if (JSON.stringify(next) === JSON.stringify(values)) return;
    values = next;
    remember();
    announce();
  }

  function load() {
    const version = revision;
    const pending = request('/preferences', {headers: {'Accept': 'application/json'}});
    if (!pending || typeof pending.then !== 'function') return;
    pending
      .then(response => (response && response.ok ? response.json() : null))
      .then(payload => { if (version === revision) adopt(payload); })
      .catch(() => {});
  }

  async function save(list) {
    const version = ++revision;
    const next = clean(list);
    let response;
    try {
      response = await request('/preferences', {
        method: 'PUT',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({starters: next}),
      });
    } catch (_) { /* Offline: keep the edit in this browser. */ }
    if (response && !response.ok) throw new Error('Could not save suggestions. Try again.');
    if (version === revision) adopt({starters: next});
    return current();
  }

  function node(tag, className, text) {
    const element = doc.createElement(tag);
    if (className) element.className = className;
    if (text !== undefined) element.textContent = text;
    return element;
  }

  // Fill a host with the chips. Hidden when the list is empty (suggestions
  // off), so the start screen has no orphaned gap.
  function render(host, onPick) {
    if (!host) return;
    host.replaceChildren();
    const list = current();
    host.hidden = !list.length;
    for (const text of list) {
      const button = node('button', 'starter', text);
      button.type = 'button';
      button.addEventListener('click', () => onPick?.(text));
      host.append(button);
    }
  }

  // One dialog: up to four lines, blank lines removed, and an explicit way to
  // turn suggestions off. Resolves to the new list, or null on cancel.
  function openEditor() {
    if (!doc) return Promise.resolve(null);
    const dialog = node('dialog', 'starters-dialog');
    const title = node('h2', '', 'Suggestions');
    title.id = 'starters-title';
    dialog.setAttribute('aria-labelledby', 'starters-title');
    const intro = node('p', 'starters-intro',
      'Quiet prompts on the start screen. Leave a line empty to remove it; choosing one only fills the message box.');
    const fields = node('div', 'starters-fields');
    const inputs = [];
    const shown = current();
    for (let index = 0; index < MAX; index += 1) {
      const label = node('label', '', `Suggestion ${index + 1}`);
      const input = node('input');
      input.type = 'text';
      input.maxLength = LIMIT;
      input.value = shown[index] || '';
      input.autocomplete = 'off';
      label.append(input);
      fields.append(label);
      inputs.push(input);
    }
    const actions = node('div', 'dialog-actions');
    const off = node('button', 'btn quiet', 'Turn off');
    off.type = 'button';
    const cancel = node('button', 'btn quiet', 'Cancel');
    cancel.type = 'button';
    const saveButton = node('button', 'btn primary', 'Save');
    saveButton.type = 'button';
    actions.append(off, cancel, saveButton);
    dialog.append(title, intro, fields, actions);
    doc.body.append(dialog);

    return new Promise(resolve => {
      const done = value => { dialog.close(); dialog.remove(); resolve(value); };
      off.onclick = () => done([]);
      cancel.onclick = () => done(null);
      dialog.addEventListener('cancel', event => { event.preventDefault(); done(null); });
      saveButton.onclick = () => done(clean(inputs.map(input => input.value)));
      dialog.showModal();
      inputs[0].focus();
    });
  }

  // A window opened from `kova web` (or a pairing URL) reaches this script
  // before app.js has exchanged its one-use token for a session cookie, so the
  // shared document is not readable yet. Keep the local copy for this first
  // frame; app.js calls sync() once the window is signed in.
  const loginPending = () => typeof location === 'object' &&
    /(^|[#&])(ticket|pair)=/.test(location.hash || '');

  globalThis.KovaStarters = {KEY, MAX, LIMIT, DEFAULT, current, save, render, openEditor, sync: load};
  if (!loginPending()) load();
})();
