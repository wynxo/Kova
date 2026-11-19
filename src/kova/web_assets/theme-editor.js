/* A draft previews on the app without changing its saved theme. The theme API
   validates imports and checks contrast as colours change, using Kova's WCAG pairs. */
(() => {
  'use strict';

  const GROUPS = {bg: 'Backgrounds', fg: 'Text', border: 'Borders', accent: 'Accent', status: 'Status'};
  const HEX = /^#(?:[\da-f]{3,4}|[\da-f]{6}|[\da-f]{8})$/i;
  const CHECK_URL = '/themes?validate_only=true';
  const MAX_FILE_BYTES = 32 * 1024; // Same limit as theme_routes.py.
  let editor;

  function el(tag, className = '', text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
  }

  function button(label, kind = '') {
    const node = el('button', `btn ${kind}`, label);
    node.type = 'button';
    return node;
  }

  function field(id, label, node) {
    node.id = id;
    const wrap = el('label', 'theme-editor-field');
    wrap.append(el('span', '', label), node);
    return wrap;
  }

  function toHex(colour) {
    if (HEX.test(colour)) {
      const hex = colour.toLowerCase();
      return hex.length <= 5 ? '#' + [...hex.slice(1)].map(char => char.repeat(2)).join('') : hex;
    }
    // The server also accepts rgb()/rgba(). Show their hex equivalent without
    // replacing the source value until the user actually edits the field.
    const parts = colour.match(/[0-9.]+%?/g) || [];
    if (parts.length < 3 || parts.length > 4 || !CSS.supports('color', colour)) return '';
    const bytes = parts.map((part, index) => {
      const scale = part.endsWith('%') ? 255 / 100 : index === 3 ? 255 : 1;
      return Math.min(255, Math.round(parseFloat(part) * scale)).toString(16).padStart(2, '0');
    });
    return '#' + bytes.join('');
  }

  function makeDraft(palette, source, choices) {
    const builtin = source === 'dark' || source === 'light';
    const stem = builtin ? `custom-${source}` : `${source.slice(0, 35)}-copy`;
    let id = stem;
    for (let n = 2; choices.some(choice => choice.id === id); n += 1) {
      const suffix = `-${n}`;
      id = stem.slice(0, 40 - suffix.length) + suffix;
    }
    const draft = {
      id, name: builtin ? `Custom ${source}` : [...palette.name].slice(0, 55).join('') + ' copy',
      version: '1.0.0', appearance: palette.appearance,
      extends: palette.extends || `kova-${palette.appearance}`, tokens: {},
    };
    if (!builtin) {
      for (const [key, value] of Object.entries(palette.tokens)) {
        const [group, name] = key.split('.');
        (draft.tokens[group] ??= {})[name] = value;
      }
      draft.terminal = palette.terminal;
    }
    return draft;
  }

  async function request(url, payload) {
    const response = await fetch(url, payload === undefined ? undefined : {
      method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(payload),
    });
    const result = await response.json();
    if (response.ok) return result;
    const detail = result?.detail;
    throw new Error(Array.isArray(detail) ? detail.join('\n') : detail || 'Kova rejected this theme.');
  }

  function download(draft) {
    const options = {type: 'application/json'};
    let blob = new Blob([JSON.stringify(draft, null, 2) + '\n'], options);
    if (blob.size > MAX_FILE_BYTES) blob = new Blob([JSON.stringify(draft)], options);
    const url = URL.createObjectURL(blob);
    const link = el('a');
    link.download = `${draft.id}.json`;
    link.href = url;
    document.body.append(link);
    link.click();
    link.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  }

  function dismiss() {
    if (!editor) return true;
    return editor.close();
  }

  function open(onClose) {
    if (editor) return;
    const theme = window.KovaTheme;
    const panel = el('aside', 'theme-editor');
    // Keep editor controls readable even if a draft makes the app illegible.
    // tokens.css already supplies a complete palette for this data-theme node.
    panel.dataset.theme = theme.appearance();
    const title = el('h2', '', 'Customize theme');
    title.id = 'theme-editor-title';
    title.tabIndex = -1;
    panel.setAttribute('aria-labelledby', title.id);

    const base = el('select');
    const imported = el('option', '', 'Imported file');
    imported.value = ':imported';
    imported.disabled = true;
    const name = el('input');
    name.type = 'text';
    name.maxLength = 60;
    const id = el('input');
    id.type = 'text';
    id.maxLength = 40;
    id.autocapitalize = 'none';
    id.spellcheck = false;
    const file = el('input');
    file.id = 'theme-editor-file';
    file.type = 'file';
    file.accept = '.json,application/json';
    file.hidden = true;

    const importButton = button('Import');
    const exportButton = button('Export');
    const cancel = button('Cancel', 'quiet');
    const save = button('Save', 'primary');
    const sources = el('div', 'theme-editor-sources');
    sources.append(field('theme-editor-base', 'Start from', base), importButton, file);
    const identity = el('fieldset', 'theme-editor-identity');
    identity.append(field('theme-editor-name', 'Name', name), field('theme-editor-id', 'Theme ID', id));
    const groups = el('fieldset', 'theme-editor-groups');
    groups.setAttribute('aria-label', 'Theme colours');
    const body = el('div', 'theme-editor-body');
    body.append(sources, identity, groups);
    const checks = el('div', 'theme-editor-checks');
    checks.setAttribute('aria-live', 'polite');
    const notice = el('p', 'theme-editor-notice');
    notice.setAttribute('role', 'status');
    const actions = el('div', 'theme-editor-actions');
    actions.append(cancel, exportButton, save);
    const footer = el('footer', 'theme-editor-footer');
    footer.append(checks, notice, actions);
    panel.append(title, body, footer);

    let options = [];
    let draft;
    let tokens;
    let source;
    let busy = true;
    let saving = false;
    let valid = false;
    let reviewTimer;
    let revision = 0;
    const incomplete = new Set();

    const session = {close};
    editor = session;
    document.body.append(panel);
    title.focus({preventScroll: true});

    function buttons() {
      base.disabled = importButton.disabled = busy;
      identity.disabled = groups.disabled = busy || !draft;
      save.disabled = exportButton.disabled = busy || !valid;
      cancel.disabled = saving;
    }

    function report(messages) {
      checks.replaceChildren(...messages.map(message => el('p', '', message)));
    }

    function close(returnToSettings = false) {
      if (saving) return false;
      clearTimeout(reviewTimer);
      editor = undefined;
      panel.remove();
      theme.clearPreview();
      if (returnToSettings) onClose?.();
      return true;
    }

    function paint() {
      theme.preview({appearance: draft.appearance, tokens});
    }

    async function review(ticket) {
      if (editor !== session || ticket !== revision) return;
      try {
        await request(CHECK_URL, draft);
        if (editor !== session || ticket !== revision) return;
        valid = true;
        report(['Contrast checks passed.']);
      } catch (error) {
        if (editor !== session || ticket !== revision) return;
        report(error.message.split('\n'));
      }
      buttons();
    }

    function check(immediate = false) {
      clearTimeout(reviewTimer);
      const ticket = ++revision;
      valid = false;
      buttons();
      if (incomplete.size) {
        report([`Enter a valid hex colour for ${[...incomplete].join(', ')}.`]);
        return;
      }
      report(['Checking contrast…']);
      if (immediate) return review(ticket);
      reviewTimer = setTimeout(() => review(ticket), 200);
    }

    function changeColour(key, value) {
      const [group, role] = key.split('.');
      draft.tokens ??= {};
      (draft.tokens[group] ??= {})[role] = value;
      tokens[key] = value;
      paint();
    }

    function colourRow(key) {
      const swatch = el('input');
      swatch.type = 'color';
      swatch.setAttribute('aria-label', `${key} colour`);
      const hex = el('input');
      hex.type = 'text';
      hex.spellcheck = false;
      hex.autocapitalize = 'none';
      hex.setAttribute('aria-label', `${key} hex`);
      const initial = toHex(tokens[key]);
      hex.value = initial || tokens[key];
      if (initial) swatch.value = initial.slice(0, 7);

      function edit(value) {
        const colour = HEX.test(value) ? toHex(value) : '';
        hex.setAttribute('aria-invalid', String(!colour));
        if (colour) {
          incomplete.delete(key);
          swatch.value = colour.slice(0, 7);
          changeColour(key, value.toLowerCase());
        } else {
          incomplete.add(key);
        }
        check();
      }

      swatch.addEventListener('input', () => {
        const alpha = toHex(tokens[key]).slice(7);
        hex.value = swatch.value + alpha;
        edit(hex.value);
      });
      hex.addEventListener('input', () => edit(hex.value.trim()));
      const row = el('div', 'theme-editor-colour');
      row.append(el('span', '', key), swatch, hex);
      return row;
    }

    function loadDraft(next, palette, nextSource) {
      draft = next;
      tokens = {...palette.tokens};
      source = nextSource;
      name.value = draft.name;
      id.value = draft.id;
      incomplete.clear();
      groups.replaceChildren();
      for (const [group, label] of Object.entries(GROUPS)) {
        const section = el('section', 'theme-editor-group');
        const keys = Object.keys(tokens).filter(key => key.startsWith(group + '.'));
        section.append(el('h3', '', label), ...keys.map(colourRow));
        groups.append(section);
      }
      paint();
    }

    async function replace(operation, errorLabel) {
      busy = true;
      buttons();
      notice.textContent = 'Loading theme…';
      try {
        await operation();
        if (editor === session) notice.textContent = '';
      } catch (error) {
        if (editor === session) notice.textContent = `${errorLabel} ${error.message}`;
      } finally {
        if (editor === session) {
          busy = false;
          base.value = source ?? base.value;
          buttons();
        }
      }
    }

    async function choose(sourceId) {
      const themeId = sourceId === 'dark' || sourceId === 'light' ? `kova-${sourceId}` : sourceId;
      const palette = await request(`/themes/${encodeURIComponent(themeId)}`);
      if (editor !== session) return;
      loadDraft(makeDraft(palette, sourceId, options), palette, sourceId);
      await check(true);
    }

    base.addEventListener('change', () => replace(() => choose(base.value), 'Could not load the starting theme.'));
    name.addEventListener('input', () => { draft.name = name.value; check(); });
    id.addEventListener('input', () => { draft.id = id.value; check(); });
    cancel.addEventListener('click', () => close(true));
    panel.addEventListener('keydown', event => {
      if (event.key !== 'Escape' || event.isComposing) return;
      event.stopPropagation();
      event.preventDefault();
      close(true);
    });
    exportButton.addEventListener('click', () => download(draft));
    importButton.addEventListener('click', () => file.click());
    file.addEventListener('change', () => {
      const chosen = file.files[0];
      file.value = '';
      if (!chosen) return;
      return replace(async () => {
        if (chosen.size > MAX_FILE_BYTES) throw new Error('Theme files must be at most 32 KiB.');
        let incoming;
        try { incoming = JSON.parse(await chosen.text()); }
        catch (_) { throw new Error('Choose a valid theme JSON file.'); }
        const palette = await request(CHECK_URL, incoming);
        if (editor !== session) return;
        clearTimeout(reviewTimer);
        revision += 1;
        if (!imported.parentNode) base.append(imported);
        loadDraft(incoming, palette, imported.value);
        valid = true;
        report(['Contrast checks passed.']);
      }, 'Could not import this theme.');
    });
    save.addEventListener('click', async () => {
      if (!valid || busy) return;
      busy = saving = true;
      buttons();
      notice.textContent = 'Saving theme…';
      try {
        const saved = await request('/themes', draft);
        await theme.set(saved.id);
        theme.refresh();
      } catch (error) {
        notice.textContent = `Could not save this theme. ${error.message}`;
        busy = saving = false;
        buttons();
        return;
      }
      saving = false;
      close(true);
    });

    replace(async () => {
      options = (await theme.refresh()).filter(option => option.id !== 'system');
      if (editor !== session) return;
      for (const option of options) {
        const row = el('option', '', option.name);
        row.value = option.id;
        base.append(row);
      }
      const current = options.find(option => option.id === theme.choice());
      base.value = current?.id || theme.appearance();
      await choose(base.value);
    }, 'Could not load the starting theme.');
  }

  window.KovaThemeEditor = {open, dismiss};
})();
