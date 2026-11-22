/* A draft previews on the app without changing its saved theme. The theme API
   validates imports and checks contrast as colours change, using Kova's WCAG pairs. */
(() => {
  'use strict';

  const GROUPS = {bg: 'Backgrounds', fg: 'Text', border: 'Borders', accent: 'Accent', status: 'Status', step: 'Steps', syntax: 'Code'};
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

  /* Small colour maths for Simple mode. These functions only derive values for
     existing semantic roles; a theme still stores plain colours, and the server
     measures every pair after the draft is merged over its base. */
  function channels(colour) {
    const hex = toHex(colour || '');
    if (!hex) return null;
    return {
      r: parseInt(hex.slice(1, 3), 16),
      g: parseInt(hex.slice(3, 5), 16),
      b: parseInt(hex.slice(5, 7), 16),
    };
  }

  function mix(from, to, amount) {
    const a = channels(from);
    const b = channels(to);
    if (!a || !b) return from;
    const channel = (x, y) => Math.round(x + (y - x) * amount).toString(16).padStart(2, '0');
    return `#${channel(a.r, b.r)}${channel(a.g, b.g)}${channel(a.b, b.b)}`;
  }

  function luminance(colour) {
    const rgb = channels(colour);
    if (!rgb) return 0;
    const linear = value => {
      const channel = value / 255;
      return channel <= 0.04045 ? channel / 12.92 : ((channel + 0.055) / 1.055) ** 2.4;
    };
    return 0.2126 * linear(rgb.r) + 0.7152 * linear(rgb.g) + 0.0722 * linear(rgb.b);
  }

  function contrast(first, second) {
    const a = luminance(first);
    const b = luminance(second);
    return (Math.max(a, b) + 0.05) / (Math.min(a, b) + 0.05);
  }

  function alphaOf(colour) {
    const match = /^rgba?\([^,]+,[^,]+,[^,]+,([0-9.]+)\)$/i.exec(String(colour || '').trim());
    return match && Number(match[1]) >= 0 && Number(match[1]) <= 1 ? Number(match[1]) : 0.12;
  }

  function translucent(colour, alpha) {
    const rgb = channels(colour);
    return rgb ? `rgba(${rgb.r}, ${rgb.g}, ${rgb.b}, ${alpha})` : colour;
  }

  // Change hue without changing a surface's lightness much. A plain mix with
  // the status colours would lift a dark surface enough to push muted text
  // below 4.5:1, so the tone control shifts hue and saturation in HSL instead;
  // the small lightness drop keeps the shipped dark raised surface readable.
  function hueOf(colour) {
    const rgb = channels(colour);
    if (!rgb) return 30;
    const r = rgb.r / 255;
    const g = rgb.g / 255;
    const b = rgb.b / 255;
    const max = Math.max(r, g, b);
    const min = Math.min(r, g, b);
    const range = max - min;
    if (!range) return 30;
    let hue;
    if (max === r) hue = ((g - b) / range) % 6;
    else if (max === g) hue = (b - r) / range + 2;
    else hue = (r - g) / range + 4;
    return (hue * 60 + 360) % 360;
  }

  function tinted(colour, hue, saturation, drop) {
    const rgb = channels(colour);
    if (!rgb) return colour;
    const r = rgb.r / 255;
    const g = rgb.g / 255;
    const b = rgb.b / 255;
    const max = Math.max(r, g, b);
    const min = Math.min(r, g, b);
    const lightness = Math.max(0, Math.min(1, (max + min) / 2 - drop));
    const currentSaturation = max === min ? 0 : (max + min) / 2 > 0.5
      ? (max - min) / (2 - max - min) : (max - min) / (max + min);
    const strength = Math.max(currentSaturation, saturation);
    const q = lightness < 0.5 ? lightness * (1 + strength) : lightness + strength - lightness * strength;
    const p = 2 * lightness - q;
    const channel = offset => {
      let value = offset + hue / 360;
      if (value < 0) value += 1;
      if (value > 1) value -= 1;
      if (value < 1 / 6) return p + (q - p) * 6 * value;
      if (value < 1 / 2) return q;
      if (value < 2 / 3) return p + (q - p) * (2 / 3 - value) * 6;
      return p;
    };
    const byte = value => Math.round(Math.max(0, Math.min(1, value)) * 255)
      .toString(16).padStart(2, '0');
    return `#${byte(channel(1 / 3))}${byte(channel(0))}${byte(channel(-1 / 3))}`;
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
    sources.append(field('theme-editor-base', 'Start from', base), importButton, share, importStringButton, file,
      el('p', 'theme-editor-hint', 'Or drop a theme JSON file anywhere on this panel to import it.'));
    const identity = el('fieldset', 'theme-editor-identity');
    identity.append(field('theme-editor-name', 'Name', name), field('theme-editor-id', 'Theme ID', id));
    const modes = el('div', 'theme-editor-modes');
    modes.setAttribute('role', 'tablist');
    modes.setAttribute('aria-label', 'Editor mode');
    const simpleTab = el('button', 'theme-editor-mode', 'Simple');
    simpleTab.type = 'button';
    simpleTab.id = 'theme-editor-tab-simple';
    simpleTab.setAttribute('role', 'tab');
    simpleTab.setAttribute('aria-controls', 'theme-editor-simple');
    const advancedTab = el('button', 'theme-editor-mode', 'Advanced');
    advancedTab.type = 'button';
    advancedTab.id = 'theme-editor-tab-advanced';
    advancedTab.setAttribute('role', 'tab');
    advancedTab.setAttribute('aria-controls', 'theme-editor-advanced');
    modes.append(simpleTab, advancedTab);

    const simplePanel = el('fieldset', 'theme-editor-simple');
    simplePanel.id = 'theme-editor-simple';
    simplePanel.setAttribute('role', 'tabpanel');
    simplePanel.setAttribute('aria-labelledby', simpleTab.id);
    const groups = el('fieldset', 'theme-editor-groups');
    groups.id = 'theme-editor-advanced';
    groups.setAttribute('role', 'tabpanel');
    groups.setAttribute('aria-labelledby', advancedTab.id);
    groups.hidden = true;
    const body = el('div', 'theme-editor-body');
    body.append(modes, sources, identity, simplePanel, groups);
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
    // The resolved values of the theme the draft started from, and the draft's
    // own overrides before Simple mode touched them. Simple controls derive
    // from the first and restore the second, so switching a control back to its
    // default leaves a duplicated or imported theme exactly as it arrived.
    let sourceTokens = {};
    let baseline = {};
    const simpleState = {accent: '', tone: 'neutral', contrast: 50};
    let colourRows = [];
    let colourInputs = new Map();
    let simpleControls;
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
      identity.disabled = simplePanel.disabled = groups.disabled = busy || !draft;
      save.disabled = exportButton.disabled = busy || !valid;
      copyButton.disabled = busy || !valid;
      importStringButton.disabled = busy;
      cancel.disabled = saving;
      reset.disabled = busy || !draft;
    }

    function report(messages) {
      checks.replaceChildren(...messages.map(message => el('p', '', message)));
      // Mark each colour row named by a failed pair (or an incomplete hex), so
      // the warning sits beside the values it is about as well as in the footer.
      const failing = new Set(incomplete);
      for (const message of messages) {
        for (const match of message.matchAll(/(?:bg|fg|border|accent|status|step|syntax)\.[a-z0-9-]+/g)) {
          failing.add(match[0]);
        }
      }
      for (const row of colourRows) {
        const key = row.dataset.token;
        const bad = failing.has(key);
        row.className = `theme-editor-colour${bad ? ' is-invalid' : ''}`;
        row.title = bad ? messages.filter(message => message.includes(key)).join('\n') : '';
      }
      if (simpleControls) {
        const accentBad = [...failing].some(key => key.startsWith('accent.'));
        simpleControls.accentRow.className =
          `theme-editor-colour theme-editor-simple-accent${accentBad ? ' is-invalid' : ''}`;
        simpleControls.accentRow.title = accentBad
          ? messages.filter(message => message.includes('accent.')).join('\n') : '';
      }
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
      row.dataset.token = key;
      row.append(el('span', '', key), swatch, hex);
      colourInputs.set(key, {swatch, hex});
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

    /* ---------------- Simple mode (#176) ----------------
       Accent, background tone and text contrast write existing semantic roles
       only; the theme schema is unchanged. Corner radius, density and font are
       app preferences with the same plumbing as the Settings rows, so they are
       shared with paired devices but never become part of a theme file. */
    const RADIUS_VALUES = ['sharp', 'default', 'round'];
    const RADIUS_LABELS = ['Sharp', 'Default', 'Round'];
    const ACCENT_ROLES = ['accent.default', 'accent.hover', 'accent.pressed',
      'accent.on', 'accent.soft', 'fg.inverse'];
    const TONE_ROLES = ['bg.base', 'bg.surface', 'bg.raised'];
    // Hue and saturation for the neutral-to-warm/cool shift. The hue itself
    // comes from the theme's own status.warning / status.info, so a themed
    // palette still decides which direction reads as warm or cool.
    const TONE_SHAPE = {
      dark: {saturation: 0.22, drop: {'bg.base': 0, 'bg.surface': 0.005, 'bg.raised': 0.02}},
      light: {saturation: 0.35, drop: {'bg.base': 0.01, 'bg.surface': 0, 'bg.raised': -0.01}},
    };
    const CONTRAST_ROLES = ['fg.default', 'fg.muted', 'fg.faint'];

    function hexOf(colour) {
      const hex = toHex(colour || '');
      return hex ? hex.slice(0, 7).toLowerCase() : '';
    }

    function sameColour(first, second) {
      const a = hexOf(first);
      return Boolean(a) && a === hexOf(second);
    }

    function assign(key, value) {
      const [group, role] = key.split('.');
      draft.tokens ??= {};
      (draft.tokens[group] ??= {})[role] = value;
      tokens[key] = value;
    }

    // Put back one role exactly as the draft arrived: an override the source
    // theme already had, or no override at all when it inherited the role.
    function restoreRole(key) {
      const [group, role] = key.split('.');
      const loaded = baseline?.[group]?.[role];
      if (loaded !== undefined) {
        assign(key, loaded);
        return;
      }
      if (draft.tokens?.[group]) {
        delete draft.tokens[group][role];
        if (!Object.keys(draft.tokens[group]).length) delete draft.tokens[group];
      }
      tokens[key] = sourceTokens[key];
    }

    function place(key, value) {
      if (sameColour(value, sourceTokens[key])) restoreRole(key);
      else assign(key, value);
    }

    // Simple controls and the Advanced rows edit the same draft, so the rows
    // follow a Simple change as well; only the roles that changed are touched.
    function syncColourRows(keys) {
      for (const key of keys) {
        const row = colourInputs.get(key);
        if (!row) continue;
        const colour = toHex(tokens[key]);
        if (!colour) continue;
        row.swatch.value = colour.slice(0, 7);
        row.hex.value = colour;
      }
    }

    function applySimpleAccent() {
      if (!draft) return;
      const accent = hexOf(simpleState.accent);
      if (!accent) return;
      if (sameColour(accent, sourceTokens['accent.default'])) {
        ACCENT_ROLES.forEach(restoreRole);
      } else {
        // Hover lightens and pressed darkens, in both appearances, which is
        // how the shipped palettes already treat the accent.
        const hover = mix(accent, '#ffffff', 0.16);
        const pressed = mix(accent, '#000000', 0.18);
        // Text on a solid accent chip: whichever of the theme's own text
        // extremes is more readable. The accent-on and inverse pairs both use
        // it, so they pass or fail together.
        const on = contrast(accent, sourceTokens['fg.default'])
          >= contrast(accent, sourceTokens['fg.inverse'])
          ? sourceTokens['fg.default'] : sourceTokens['fg.inverse'];
        assign('accent.default', accent);
        assign('accent.hover', hover);
        assign('accent.pressed', pressed);
        assign('accent.on', on);
        assign('accent.soft', translucent(accent, alphaOf(sourceTokens['accent.soft'])));
        assign('fg.inverse', on);
      }
      syncColourRows(ACCENT_ROLES);
      paint();
      check();
    }

    function applySimpleTone() {
      if (!draft) return;
      const source = simpleState.tone === 'warm' ? sourceTokens['status.warning']
        : simpleState.tone === 'cool' ? sourceTokens['status.info'] : '';
      const shape = TONE_SHAPE[draft.appearance] || TONE_SHAPE.dark;
      for (const key of TONE_ROLES) {
        place(key, source
          ? tinted(sourceTokens[key], hueOf(source), shape.saturation, shape.drop[key])
          : sourceTokens[key]);
      }
      syncColourRows(TONE_ROLES);
      paint();
      check();
    }

    function applySimpleContrast() {
      if (!draft) return;
      const value = Number(simpleState.contrast);
      const delta = (value - 50) / 50;
      // Softer moves toward the background's extreme; stronger moves toward
      // the opposite one. At 50 the role keeps the theme's own value exactly.
      const target = delta >= 0
        ? (draft.appearance === 'dark' ? '#ffffff' : '#000000')
        : (draft.appearance === 'dark' ? '#000000' : '#ffffff');
      const amount = Math.abs(delta) * 0.55;
      for (const key of CONTRAST_ROLES) {
        place(key, value === 50 ? sourceTokens[key] : mix(sourceTokens[key], target, amount));
      }
      syncColourRows(CONTRAST_ROLES);
      paint();
      check();
    }

    function segmented(prefix, label, options, onPick) {
      const group = el('div', 'theme-editor-segmented');
      group.setAttribute('role', 'radiogroup');
      group.setAttribute('aria-label', label);
      const items = [];
      for (const [value, text] of options) {
        const item = el('button', 'theme-editor-segment', text);
        item.type = 'button';
        item.id = `${prefix}-${value}`;
        item.dataset.value = value;
        item.setAttribute('role', 'radio');
        item.addEventListener('click', () => onPick(value));
        item.addEventListener('keydown', event => {
          if (!['ArrowLeft', 'ArrowRight', 'ArrowUp', 'ArrowDown'].includes(event.key)) return;
          event.preventDefault();
          const at = options.findIndex(([candidate]) => candidate === value);
          const step = event.key === 'ArrowLeft' || event.key === 'ArrowUp' ? -1 : 1;
          const next = options[(at + step + options.length) % options.length][0];
          onPick(next);
          items.find(candidate => candidate.dataset.value === next)?.focus();
        });
        items.push(item);
        group.append(item);
      }
      return {group, items};
    }

    function syncSegmented(items, value) {
      for (const item of items) {
        const checked = item.dataset.value === value;
        item.setAttribute('aria-checked', String(checked));
        item.tabIndex = checked ? 0 : -1;
      }
    }

    function contrastLabel(value) {
      const delta = Math.round(Number(value) - 50);
      if (!delta) return 'Default';
      return delta > 0 ? `Stronger ${delta}%` : `Softer ${-delta}%`;
    }

    function typography(method, value) {
      const target = window.KovaTypography;
      if (target && typeof target[method] === 'function') target[method](value);
      syncSimple();
    }

    function buildSimple() {
      const accentSection = el('section', 'theme-editor-group');
      const accentSwatch = el('input');
      accentSwatch.id = 'theme-editor-simple-accent';
      accentSwatch.type = 'color';
      accentSwatch.setAttribute('aria-label', 'Accent colour');
      const accentHex = el('input');
      accentHex.id = 'theme-editor-simple-accent-hex';
      accentHex.type = 'text';
      accentHex.spellcheck = false;
      accentHex.autocapitalize = 'none';
      accentHex.setAttribute('aria-label', 'Accent hex');
      const accentRow = el('div', 'theme-editor-colour theme-editor-simple-accent');
      accentRow.append(el('span', '', 'Accent colour'), accentSwatch, accentHex);
      accentSection.append(el('h3', '', 'Accent'), accentRow,
        el('p', 'theme-editor-hint', 'Used for buttons, links, selections and the focus ring.'));

      function editAccent(value) {
        const colour = HEX.test(value) ? hexOf(value) : '';
        accentHex.setAttribute('aria-invalid', String(!colour));
        if (!colour) return;
        simpleState.accent = colour;
        accentSwatch.value = colour;
        accentHex.value = colour;
        applySimpleAccent();
      }

      accentSwatch.addEventListener('input', () => editAccent(accentSwatch.value));
      accentHex.addEventListener('input', () => editAccent(accentHex.value.trim()));

      const toneSection = el('section', 'theme-editor-group');
      const tone = segmented('theme-editor-tone', 'Background tone', [
        ['neutral', 'Neutral'], ['warm', 'Warm'], ['cool', 'Cool'],
      ], value => {
        simpleState.tone = value;
        applySimpleTone();
        syncSimple();
      });
      toneSection.append(el('h3', '', 'Background tone'), tone.group,
        el('p', 'theme-editor-hint', "Tints the app background. Neutral keeps the theme's own values."));

      const contrastSection = el('section', 'theme-editor-group');
      const contrast = el('input');
      contrast.type = 'range';
      contrast.min = '0';
      contrast.max = '100';
      contrast.step = '5';
      const contrastShown = el('output', '', 'Default');
      contrast.addEventListener('input', () => {
        simpleState.contrast = Number(contrast.value);
        contrastShown.textContent = contrastLabel(simpleState.contrast);
        applySimpleContrast();
      });
      const contrastRow = el('div', 'theme-editor-range');
      contrastRow.append(field('theme-editor-contrast', 'Level', contrast), contrastShown);
      contrastSection.append(el('h3', '', 'Text contrast'), contrastRow,
        el('p', 'theme-editor-hint', 'Softer can drop a pair below WCAG AA; the check below names it.'));

      const shapeSection = el('section', 'theme-editor-group');
      const radius = el('input');
      radius.type = 'range';
      radius.min = '0';
      radius.max = '2';
      radius.step = '1';
      const radiusShown = el('output', '', 'Default');
      radius.addEventListener('input', () => {
        const index = Math.max(0, Math.min(2, Number(radius.value)));
        radiusShown.textContent = RADIUS_LABELS[index];
        typography('setRadius', RADIUS_VALUES[index]);
      });
      const radiusRow = el('div', 'theme-editor-range');
      radiusRow.append(field('theme-editor-radius', 'Corners', radius), radiusShown);
      const density = segmented('theme-editor-density', 'Density', [
        ['compact', 'Compact'], ['default', 'Default'], ['comfortable', 'Comfortable'],
      ], value => typography('setDensity', value));
      const font = segmented('theme-editor-font', 'Font', [
        ['inter', 'Geologica'], ['system', 'System'], ['mono', 'Monospace'],
      ], value => typography('setFont', value));
      const densityChoice = el('div', 'theme-editor-choice');
      densityChoice.append(el('span', 'theme-editor-choice-label', 'Density'), density.group);
      const fontChoice = el('div', 'theme-editor-choice');
      fontChoice.append(el('span', 'theme-editor-choice-label', 'Font'), font.group);
      shapeSection.append(el('h3', '', 'Shape and type'), radiusRow, densityChoice, fontChoice,
        el('p', 'theme-editor-hint', 'Corners, density and font are app settings shared with every paired device. They stay outside the theme file.'));

      simpleControls = {
        accentRow, accentSwatch, accentHex, tone: tone.items, contrast, contrastShown,
        radius, radiusShown, density: density.items, font: font.items,
      };
      simplePanel.replaceChildren(accentSection, toneSection, contrastSection, shapeSection);
      syncSimple();
      buttons();
    }

    function syncSimple() {
      if (!simpleControls) return;
      const accent = hexOf(simpleState.accent);
      if (accent) {
        simpleControls.accentSwatch.value = accent;
        simpleControls.accentHex.value = simpleState.accent || accent;
      }
      syncSegmented(simpleControls.tone, simpleState.tone);
      simpleControls.contrast.value = String(simpleState.contrast);
      simpleControls.contrastShown.textContent = contrastLabel(simpleState.contrast);
      const engine = window.KovaTypography;
      const radiusValue = engine && typeof engine.radius === 'function' ? engine.radius() : 'default';
      const radiusIndex = Math.max(0, RADIUS_VALUES.indexOf(radiusValue));
      simpleControls.radius.value = String(radiusIndex);
      simpleControls.radiusShown.textContent = RADIUS_LABELS[radiusIndex];
      const densityValue = engine && typeof engine.density === 'function' ? engine.density() : 'default';
      syncSegmented(simpleControls.density, densityValue);
      const fontValue = engine && typeof engine.font === 'function' ? engine.font() : 'inter';
      syncSegmented(simpleControls.font, fontValue);
    }

    function setMode(next) {
      const simple = next !== 'advanced';
      simpleTab.setAttribute('aria-selected', String(simple));
      advancedTab.setAttribute('aria-selected', String(!simple));
      simpleTab.tabIndex = simple ? 0 : -1;
      advancedTab.tabIndex = simple ? -1 : 0;
      simplePanel.hidden = !simple;
      groups.hidden = simple;
    }

    simpleTab.addEventListener('click', () => setMode('simple'));
    advancedTab.addEventListener('click', () => setMode('advanced'));
    modes.addEventListener('keydown', event => {
      if (!['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return;
      event.preventDefault();
      const next = event.key === 'Home' || event.key === 'ArrowLeft' ? 'simple' : 'advanced';
      setMode(next);
      (next === 'simple' ? simpleTab : advancedTab).focus();
    });
    setMode('simple');

    function loadDraft(next, palette, nextSource) {
      draft = next;
      tokens = {...palette.tokens};
      sourceTokens = {...palette.tokens};
      baseline = JSON.parse(JSON.stringify(next.tokens || {}));
      source = nextSource;
      parentPalette = palette;
      name.value = draft.name;
      id.value = draft.id;
      incomplete.clear();
      colourRows = [];
      colourInputs = new Map();
      groups.replaceChildren();
      for (const [group, label] of Object.entries(GROUPS)) {
        const section = el('section', 'theme-editor-group');
        const keys = Object.keys(tokens).filter(key => key.startsWith(group + '.'));
        const rows = keys.map(colourRow);
        colourRows.push(...rows);
        section.append(el('h3', '', label), ...rows);
        groups.append(section);
      }
      groups.append(backgroundSection());
      simpleState.accent = hexOf(palette.tokens['accent.default']);
      simpleState.tone = 'neutral';
      simpleState.contrast = 50;
      syncSimple();
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
    // The picker and a drop share one import path: the same size cap, the same
    // parse and the same server validation either way.
    async function importChosen(chosen, invalidLabel) {
      if (chosen.size > MAX_FILE_BYTES) throw new Error('Theme files must be at most 32 KiB.');
      let incoming;
      try { incoming = JSON.parse(await chosen.text()); }
      catch (_) { throw new Error(invalidLabel); }
      await applyImport(incoming);
    }
    file.addEventListener('change', () => {
      const chosen = file.files[0];
      file.value = '';
      if (!chosen) return;
      return replace(() => importChosen(chosen, 'Choose a valid theme JSON file.'), 'Could not import this theme.');
    });
    // Drag-and-drop import (#176): a theme file may be dropped anywhere on the
    // panel. Only file drags are claimed, so dragging text into a field keeps
    // the browser's own behaviour, and the outline comes off however the drag
    // ends.
    let dragDepth = 0;
    const dragHasFiles = transfer => {
      const types = transfer?.types;
      return Boolean(types) && (typeof types.contains === 'function'
        ? types.contains('Files') : [...types].includes('Files'));
    };
    panel.addEventListener('dragenter', event => {
      if (!dragHasFiles(event.dataTransfer)) return;
      event.preventDefault();
      dragDepth += 1;
      panel.classList.add('is-drop-target');
    });
    panel.addEventListener('dragover', event => {
      if (!dragHasFiles(event.dataTransfer)) return;
      event.preventDefault();
      event.dataTransfer.dropEffect = 'copy';
    });
    panel.addEventListener('dragleave', () => {
      dragDepth = Math.max(0, dragDepth - 1);
      if (!dragDepth) panel.classList.remove('is-drop-target');
    });
    panel.addEventListener('drop', event => {
      if (!dragHasFiles(event.dataTransfer)) return;
      event.preventDefault();
      dragDepth = 0;
      panel.classList.remove('is-drop-target');
      const chosen = event.dataTransfer.files[0];
      if (chosen) {
        return replace(() => importChosen(chosen, 'Drop a valid theme JSON file.'), 'Could not import this theme.');
      }
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

    buildSimple();
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
