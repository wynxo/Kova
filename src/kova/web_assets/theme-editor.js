/* A draft previews on the app without changing its saved theme. The theme API
   validates imports and checks contrast as colours change, using Kova's WCAG pairs. */
(() => {
  'use strict';

  const GROUPS = {bg: 'Backgrounds', fg: 'Text', border: 'Borders', accent: 'Accent', status: 'Status'};
  const HEX = /^#(?:[\da-f]{3,4}|[\da-f]{6}|[\da-f]{8})$/i;
  const BACKGROUNDS = {none: 'None', gradient: 'Gradient', noise: 'Noise', image: 'Image'};
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
      if (palette.background) draft.background = JSON.parse(JSON.stringify(palette.background));
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
  // A share string is a compact, paste-safe encoding of a theme so it can be
  // copied and imported on another device. It round-trips the theme exactly:
  // the minified JSON is made ASCII-safe, then base64url encoded.
  const SHARE_PREFIX = 'kova-theme:';
  const B64 = 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_';
  function encodeBase64url(str) {
    let out = '';
    for (let i = 0; i < str.length; i += 3) {
      const a = str.charCodeAt(i);
      const b = i + 1 < str.length ? str.charCodeAt(i + 1) : 0;
      const c = i + 2 < str.length ? str.charCodeAt(i + 2) : 0;
      const n = (a << 16) | (b << 8) | c;
      out += B64[(n >> 18) & 63] + B64[(n >> 12) & 63] +
        (i + 1 < str.length ? B64[(n >> 6) & 63] : '=') + (i + 2 < str.length ? B64[n & 63] : '=');
    }
    return out;
  }
  function decodeBase64url(str) {
    let out = '';
    for (let i = 0; i < str.length; i += 4) {
      const a = B64.indexOf(str[i]);
      const b = B64.indexOf(str[i + 1]);
      const c = str[i + 2] === '=' ? 0 : B64.indexOf(str[i + 2]);
      const d = str[i + 3] === '=' ? 0 : B64.indexOf(str[i + 3]);
      const n = (a << 18) | (b << 12) | (c << 6) | d;
      out += String.fromCharCode((n >> 16) & 255);
      if (str[i + 2] !== '=') out += String.fromCharCode((n >> 8) & 255);
      if (str[i + 3] !== '=') out += String.fromCharCode(n & 255);
    }
    return out;
  }
  function asciiSafe(json) {
    return json.replace(/[^\x21-\x7e]/g, char => '\\u' + ('0000' + char.charCodeAt(0).toString(16)).slice(-4));
  }
  function shareString(draft) {
    return SHARE_PREFIX + encodeBase64url(asciiSafe(JSON.stringify(draft)));
  }
  function parseShareString(str) {
    if (typeof str !== 'string' || !str.startsWith(SHARE_PREFIX)) throw new Error('Not a Kova theme share string.');
    const payload = str.slice(SHARE_PREFIX.length);
    // The ASCII codec can expand a character to a six-byte JSON escape.
    if (payload.length > MAX_FILE_BYTES * 8) throw new Error('Theme share string is too large.');
    return JSON.parse(decodeBase64url(payload));
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
    const share = el('input');
    share.id = 'theme-editor-share';
    share.type = 'text';
    share.maxLength = 2000;
    share.spellcheck = false;
    share.autocapitalize = 'none';
    share.setAttribute('aria-label', 'Share string to import');
    share.placeholder = 'Paste a theme share string';
    const importStringButton = button('Import string');

    const importButton = button('Import');
    const exportButton = button('Export');
    const copyButton = button('Copy share string');
    const cancel = button('Cancel', 'quiet');
    const save = button('Save', 'primary');
    const reset = button('Reset to base', 'quiet');
    reset.addEventListener('click', () => resetToBase());
    const sources = el('div', 'theme-editor-sources');
    sources.append(field('theme-editor-base', 'Start from', base), importButton, share, importStringButton, file);
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
    actions.append(reset, cancel, exportButton, copyButton, save);
    const footer = el('footer', 'theme-editor-footer');
    footer.append(checks, notice, actions);
    panel.append(title, body, footer);

    let options = [];
    let draft;
    let tokens;
    let source;
    let parentPalette;
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
      copyButton.disabled = busy || !valid;
      importStringButton.disabled = busy;
      cancel.disabled = saving;
      reset.disabled = busy || !draft;
    }

    function report(messages) {
      checks.replaceChildren(...messages.map(message => el('p', '', message)));
    }

    function close(returnToSettings = false) {
      if (saving) return false;
      clearTimeout(reviewTimer);
      editor = undefined;
      panel.remove();
      theme.clearPalettePreview();
      if (returnToSettings) onClose?.();
      return true;
    }

    function paint() {
      theme.previewPalette({appearance: draft.appearance, tokens, background: draft.background});
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

    /* The app background (#206): none, a gradient, fine noise or one image file.
       Every change repaints the preview and asks the server to re-check contrast,
       which also measures text over the worst-case area behind a panel. */
    function setBackground(next) {
      if (next) draft.background = next;
      else delete draft.background;
      paint();
      check();
    }

    function rangeRow(key, label, {min, max, step, value, unit = ''}, onChange) {
      const input = el('input');
      input.type = 'range';
      input.min = String(min);
      input.max = String(max);
      input.step = String(step);
      input.value = String(value);
      const shown = el('output', '', `${value}${unit}`);
      input.addEventListener('input', () => {
        const number = Number(input.value);
        shown.textContent = `${number}${unit}`;
        onChange(number);
      });
      const row = el('div', 'theme-editor-range');
      row.append(field(`theme-editor-background-${key}`, label, input), shown);
      return row;
    }

    function stopRow(index, stops) {
      const swatch = el('input');
      swatch.type = 'color';
      swatch.value = (toHex(stops[index]) || '#000000').slice(0, 7);
      swatch.addEventListener('input', () => {
        const next = [...draft.background.stops];
        next[index] = swatch.value;
        setBackground({...draft.background, stops: next});
      });
      return field(`theme-editor-background-stop-${index + 1}`, `Colour ${index + 1}`, swatch);
    }

    function backgroundControls(holder) {
      const current = draft.background;
      holder.replaceChildren();
      if (!current) return;
      if (current.kind === 'gradient') {
        const stops = current.stops;
        stops.forEach((_, index) => holder.append(stopRow(index, stops)));
        const more = button('Add colour', 'quiet');
        more.disabled = stops.length >= 4;
        const fewer = button('Remove colour', 'quiet');
        fewer.disabled = stops.length <= 2;
        more.addEventListener('click', () => {
          setBackground({...draft.background, stops: [...draft.background.stops, draft.background.stops.at(-1)]});
          backgroundControls(holder);
        });
        fewer.addEventListener('click', () => {
          setBackground({...draft.background, stops: draft.background.stops.slice(0, -1)});
          backgroundControls(holder);
        });
        const row = el('div', 'theme-editor-actions');
        row.append(more, fewer);
        holder.append(row, rangeRow('angle', 'Angle', {min: 0, max: 360, step: 5, value: current.angle ?? 160, unit: '°'},
          angle => setBackground({...draft.background, angle})));
      } else if (current.kind === 'noise') {
        holder.append(rangeRow('opacity', 'Grain strength', {min: 0.02, max: 0.2, step: 0.01, value: current.opacity ?? 0.06},
          opacity => setBackground({...draft.background, opacity})));
      } else if (current.kind === 'image') {
        const file = el('input');
        file.type = 'text';
        file.maxLength = 64;
        file.spellcheck = false;
        file.autocapitalize = 'none';
        file.placeholder = 'wallpaper.png';
        file.value = current.file || '';
        file.addEventListener('input', () => setBackground({...draft.background, file: file.value.trim()}));
        holder.append(field('theme-editor-background-file', 'Image file name', file),
          el('p', 'theme-editor-hint', 'Put a png, jpg or webp file (up to 4 MiB) in the themes/assets folder of Kova\'s home first.'),
          rangeRow('dim', 'Dim', {min: 0, max: 0.8, step: 0.05, value: current.dim ?? 0.55},
            dim => setBackground({...draft.background, dim})),
          rangeRow('blur', 'Blur', {min: 0, max: 24, step: 1, value: current.blur ?? 0, unit: ' px'},
            blur => setBackground({...draft.background, blur})));
      }
    }

    function backgroundSection() {
      const section = el('section', 'theme-editor-group theme-editor-background');
      const kind = el('select');
      for (const [value, label] of Object.entries(BACKGROUNDS)) {
        const option = el('option', '', label);
        option.value = value;
        kind.append(option);
      }
      kind.value = draft.background?.kind || 'none';
      const holder = el('div', 'theme-editor-background-controls');
      kind.addEventListener('change', () => {
        const next = {
          none: () => null,
          gradient: () => ({kind: 'gradient', stops: [toHex(tokens['bg.base']).slice(0, 7), toHex(tokens['bg.raised']).slice(0, 7)], angle: 160}),
          noise: () => ({kind: 'noise', opacity: 0.06}),
          image: () => ({kind: 'image', file: '', dim: 0.55, blur: 0}),
        }[kind.value]();
        setBackground(next);
        backgroundControls(holder);
      });
      section.append(el('h3', '', 'Background'), field('theme-editor-background-kind', 'Behind the app', kind), holder);
      backgroundControls(holder);
      return section;
    }

    function loadDraft(next, palette, nextSource) {
      draft = next;
      tokens = {...palette.tokens};
      source = nextSource;
      parentPalette = palette;
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
      groups.append(backgroundSection());
      paint();
    }

    // Revert every edited colour and any background back to the base theme the
    // draft started from, leaving the base and the theme's name and ID alone.
    function resetToBase() {
      if (busy || !draft || !parentPalette) return;
      const nested = {};
      for (const [key, value] of Object.entries(parentPalette.tokens)) {
        const [group, role] = key.split('.');
        (nested[group] ??= {})[role] = value;
      }
      draft.tokens = nested;
      if (parentPalette.background) {
        draft.background = JSON.parse(JSON.stringify(parentPalette.background));
      } else {
        delete draft.background;
      }
      loadDraft(draft, parentPalette, source);
      check();
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
      const palette = await request(theme.detailUrl(themeId));
      if (editor !== session) return;
      loadDraft(makeDraft(palette, sourceId, options), palette, sourceId);
      await check(true);
    }
    async function applyImport(incoming) {
      const palette = await request(CHECK_URL, incoming);
      if (editor !== session) return;
      clearTimeout(reviewTimer);
      revision += 1;
      if (!imported.parentNode) base.append(imported);
      loadDraft(incoming, palette, imported.value);
      valid = true;
      report(['Contrast checks passed.']);
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
        await applyImport(incoming);
      }, 'Could not import this theme.');
    });
    importStringButton.addEventListener('click', () => {
      const text = share.value.trim();
      share.value = '';
      return replace(async () => {
        let incoming;
        try { incoming = parseShareString(text); }
        catch (_) { throw new Error('Paste a valid theme share string.'); }
        await applyImport(incoming);
      }, 'Could not import this theme.');
    });
    copyButton.addEventListener('click', async () => {
      const str = shareString(draft);
      try {
        await navigator.clipboard.writeText(str);
        notice.textContent = 'Share string copied to clipboard.';
      } catch (_) {
        share.value = str;
        share.select?.();
        notice.textContent = 'Paste the share string to import it on another device.';
      }
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
