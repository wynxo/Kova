/* Kova keyboard shortcuts (#200).
 *
 * One registry for the web and desktop app: every action has an id, its default
 * key and the sentence that describes it. app.js dispatches the document's
 * chords through it, the palette and the shortcuts dialog read their labels
 * from it, and the Settings row lets the user record new keys.
 *
 * A user's changes are overrides in this browser's preferences
 * (`localStorage['kova-shortcuts']`, next to 'kova-theme' and 'kova-sidebar'),
 * stored as {action id: key}. Only the actions that differ from their default
 * are written, so a corrupt or partial document always falls back to a working
 * keyboard. A binding that would collide with another action is refused rather
 * than saved: two actions can never share a chord.
 *
 * A binding is a lowercase string such as "mod+shift+k". "mod" is Ctrl on
 * Windows and Linux and Command on macOS; ctrl, meta, alt and shift are the
 * literal keys. The file is plain logic plus the remap list, with no network
 * access and no innerHTML, so tests/web_shortcuts.mjs can exercise it alone.
 */
(() => {
  'use strict';

  const STORAGE_KEY = 'kova-shortcuts';
  const MODIFIER_ORDER = ['mod', 'ctrl', 'meta', 'alt', 'shift'];
  const MODIFIER_ALIASES = {
    mod: 'mod', ctrl: 'ctrl', control: 'ctrl', meta: 'meta', cmd: 'meta',
    command: 'meta', win: 'meta', super: 'meta', alt: 'alt', option: 'alt',
    opt: 'alt', shift: 'shift',
  };
  const KEY_ALIASES = {
    esc: 'escape', escape: 'escape', return: 'enter', enter: 'enter',
    spacebar: 'space', ' ': 'space', space: 'space', up: 'arrowup',
    down: 'arrowdown', left: 'arrowleft', right: 'arrowright',
    pageup: 'pageup', pagedown: 'pagedown',
  };
  const NAMED_KEYS = new Set([
    'escape', 'enter', 'tab', 'space', 'backspace', 'delete', 'insert', 'home',
    'end', 'pageup', 'pagedown', 'arrowup', 'arrowdown', 'arrowleft', 'arrowright',
    ...Array.from({length: 12}, (_, index) => `f${index + 1}`),
  ]);
  // Pressing one of these alone records nothing; the next key still counts.
  const MODIFIER_KEYS = new Set(['control', 'shift', 'alt', 'meta', 'os', 'altgraph', 'capslock']);
  const PRINTABLE = /^[\x20-\x7e]$/;
  const KEY_LABELS = {
    escape: 'Esc', enter: 'Enter', tab: 'Tab', space: 'Space', backspace: 'Backspace',
    delete: 'Delete', insert: 'Insert', home: 'Home', end: 'End', pageup: 'PageUp',
    pagedown: 'PageDown', arrowup: '↑', arrowdown: '↓', arrowleft: '←', arrowright: '→',
  };

  /** The actions the app knows, in the order the shortcuts dialog lists them. */
  const REGISTRY = Object.freeze([
    {id: 'palette', defaultKey: 'mod+k', description: 'Open the command palette'},
    {id: 'new-conversation', defaultKey: 'mod+shift+k', description: 'New conversation'},
    {id: 'new-incognito', defaultKey: 'mod+shift+n', description: 'New incognito conversation'},
    {id: 'search-conversations', defaultKey: 'mod+shift+f', description: 'Search conversations'},
    {id: 'toggle-sidebar', defaultKey: 'mod+b', description: 'Show or hide the sidebar'},
    {id: 'toggle-theme', defaultKey: 'mod+shift+l', description: 'Switch light and dark'},
    {id: 'focus-composer', defaultKey: 'mod+space', description: 'Focus the message box'},
    {id: 'stop-reply', defaultKey: 'escape', description: 'Stop the reply'},
    {id: 'shortcuts', defaultKey: 'mod+/', description: 'Keyboard shortcuts'},
  ]);
  const BY_ID = new Map(REGISTRY.map(entry => [entry.id, entry]));

  function isMac(platform) {
    return /Mac|iPhone|iPad/.test(String(platform ?? globalThis.navigator?.platform ?? ''));
  }

  function safeStorage() {
    try { return globalThis.localStorage || null; } catch (_) { return null; }
  }

  /** Split a binding into its modifiers and one key; null when it is not one. */
  function parse(binding) {
    if (typeof binding !== 'string') return null;
    const text = (binding === ' ' ? 'space' : binding.trim()).toLowerCase();
    if (!text) return null;
    const parts = {mod: false, ctrl: false, meta: false, alt: false, shift: false, key: null};
    for (const token of text.split('+').map(piece => piece.trim())) {
      if (!token) return null;
      const modifier = MODIFIER_ALIASES[token];
      if (modifier) {
        if (parts[modifier]) return null;
        parts[modifier] = true;
        continue;
      }
      if (parts.key !== null) return null;
      const key = KEY_ALIASES[token] || token;
      if (key.length !== 1 && !NAMED_KEYS.has(key)) return null;
      if (key === '+') return null;  // the separator cannot be a key
      parts.key = key;
    }
    return parts.key === null ? null : parts;
  }

  /** The one saved spelling of a binding: modifiers in a fixed order, lowercase. */
  function formatBinding(binding) {
    const parts = parse(binding);
    if (!parts) return null;
    return [...MODIFIER_ORDER.filter(name => parts[name]), parts.key].join('+');
  }

  /** The chord a binding really uses on this platform, for conflict checks. */
  function chordOf(binding, mac = isMac()) {
    const parts = parse(binding);
    if (!parts) return null;
    const tokens = [];
    if (parts.ctrl || (parts.mod && !mac)) tokens.push('ctrl');
    if (parts.meta || (parts.mod && mac)) tokens.push('meta');
    if (parts.alt) tokens.push('alt');
    if (parts.shift) tokens.push('shift');
    tokens.push(parts.key);
    return tokens.join('+');
  }

  /** What the dialog and the palette show, for example ['Ctrl', 'Shift', 'K']. */
  function labels(binding, options = {}) {
    const parts = parse(binding);
    if (!parts) return [];
    const mac = options.mac ?? isMac(options.platform);
    const shown = [];
    if (parts.mod) shown.push(mac ? '⌘' : 'Ctrl');
    if (parts.ctrl) shown.push(mac ? '⌃' : 'Ctrl');
    if (parts.meta) shown.push(mac ? '⌘' : 'Meta');
    if (parts.alt) shown.push(mac ? '⌥' : 'Alt');
    if (parts.shift) shown.push(mac ? '⇧' : 'Shift');
    const key = KEY_LABELS[parts.key] || (parts.key.length === 1 ? parts.key.toUpperCase() : parts.key);
    shown.push(key);
    return shown;
  }

  /** Accept only a key a shortcut can really own: a modifier, or a named key. */
  function isUsable(binding) {
    const parts = parse(binding);
    if (!parts) return false;
    if (parts.mod || parts.ctrl || parts.meta || parts.alt) return true;
    return !PRINTABLE.test(parts.key);
  }

  function eventKey(event) {
    const key = typeof event?.key === 'string' ? event.key : '';
    if (!key) return null;
    if (key === ' ') return 'space';
    const lowered = key.toLowerCase();
    return KEY_ALIASES[lowered] || lowered;
  }

  /** Does one keydown event press this binding? */
  function matches(binding, event, options = {}) {
    const parts = parse(binding);
    if (!parts || !event) return false;
    const key = eventKey(event);
    if (key === null || key !== parts.key) return false;
    const mac = options.mac ?? isMac(options.platform);
    const needMeta = Boolean(parts.meta || (parts.mod && mac));
    const needCtrl = Boolean(parts.ctrl || (parts.mod && !mac));
    return Boolean(event.metaKey) === needMeta
      && Boolean(event.ctrlKey) === needCtrl
      && Boolean(event.altKey) === parts.alt
      && Boolean(event.shiftKey) === parts.shift;
  }

  /** The action a keydown event runs, or null when no binding matches it. */
  function actionFor(event, overrides = {}, options = {}) {
    const bindings = resolveOverrides(overrides);
    for (const entry of REGISTRY) {
      if (matches(bindings[entry.id], event, options)) return entry.id;
    }
    return null;
  }

  /** Turn a keydown event into a binding, or null while only modifiers are down. */
  function bindingFromEvent(event, options = {}) {
    if (!event || event.isComposing) return null;
    const key = eventKey(event);
    if (!key || MODIFIER_KEYS.has(key)) return null;
    if (key.length !== 1 && !NAMED_KEYS.has(key)) return null;
    const mac = options.mac ?? isMac(options.platform);
    const chosen = [];
    if (mac ? event.metaKey : event.ctrlKey) chosen.push('mod');
    if (mac && event.ctrlKey) chosen.push('ctrl');
    if (!mac && event.metaKey) chosen.push('meta');
    if (event.altKey) chosen.push('alt');
    if (event.shiftKey) chosen.push('shift');
    return formatBinding([...chosen, key].join('+'));
  }

  /** Only known actions with a valid, non-default binding survive a load. */
  function normalizeOverrides(value) {
    const clean = {};
    if (!value || typeof value !== 'object' || Array.isArray(value)) return clean;
    for (const entry of REGISTRY) {
      const binding = formatBinding(value[entry.id]);
      if (binding && binding !== entry.defaultKey) clean[entry.id] = binding;
    }
    return clean;
  }

  /** The complete map: every default, with the user's overrides on top. */
  function resolveOverrides(overrides = {}) {
    const clean = normalizeOverrides(overrides);
    const resolved = {};
    for (const entry of REGISTRY) resolved[entry.id] = clean[entry.id] || entry.defaultKey;
    return resolved;
  }

  /** Every chord used by more than one action on this platform. */
  function conflicts(bindings = {}, options = {}) {
    const mac = options.mac ?? isMac(options.platform);
    const groups = new Map();
    for (const entry of REGISTRY) {
      const chord = chordOf(bindings?.[entry.id] ?? entry.defaultKey, mac);
      if (!chord) continue;
      if (!groups.has(chord)) groups.set(chord, []);
      groups.get(chord).push(entry.id);
    }
    return [...groups]
      .filter(([, ids]) => ids.length > 1)
      .map(([chord, ids]) => ({chord, ids}));
  }

  /**
   * Validate one change without saving it.
   *
   * Returns {ok: true, overrides, binding} with the new override map, or
   * {ok: false, error, conflictWith?} where error is 'invalid',
   * 'needs-modifier', 'unknown-action' or 'conflict'. The caller decides when
   * to write the result.
   */
  function planChange(id, binding, overrides = {}, options = {}) {
    const entry = BY_ID.get(id);
    if (!entry) return {ok: false, error: 'unknown-action'};
    const canonical = formatBinding(binding);
    if (!canonical) return {ok: false, error: 'invalid'};
    if (!isUsable(canonical)) return {ok: false, error: 'needs-modifier'};
    const mac = options.mac ?? isMac(options.platform);
    const candidate = resolveOverrides({...overrides, [id]: canonical});
    const clash = conflicts(candidate, {mac}).find(group => group.ids.includes(id));
    if (clash) return {ok: false, error: 'conflict', conflictWith: clash.ids.filter(other => other !== id)};
    const changed = normalizeOverrides(overrides);
    if (canonical === entry.defaultKey) delete changed[id];
    else changed[id] = canonical;
    return {ok: true, overrides: changed, binding: canonical};
  }

  function loadOverrides(storage) {
    if (!storage || typeof storage.getItem !== 'function') return {};
    try {
      return normalizeOverrides(JSON.parse(storage.getItem(STORAGE_KEY) || 'null'));
    } catch (_) {
      return {};
    }
  }

  function saveOverrides(storage, overrides) {
    const clean = normalizeOverrides(overrides);
    if (!storage || typeof storage.setItem !== 'function') return;
    try {
      if (Object.keys(clean).length) storage.setItem(STORAGE_KEY, JSON.stringify(clean));
      else storage.removeItem?.(STORAGE_KEY);
    } catch (_) { /* a blocked or full storage keeps the in-memory change */ }
  }

  /** One app instance: the overrides in memory, saved to preferences on change. */
  function create(options = {}) {
    const mac = options.mac === undefined ? isMac(options.platform) : Boolean(options.mac);
    const storage = options.storage === undefined ? safeStorage() : options.storage;
    let overrides = loadOverrides(storage);
    const listeners = new Set();
    const resolved = () => resolveOverrides(overrides);
    const emit = () => {
      for (const listener of [...listeners]) {
        try { listener(); } catch (_) { /* a broken panel must not block the save */ }
      }
    };
    return {
      mac,
      registry: REGISTRY,
      overrides: () => ({...overrides}),
      binding: id => resolved()[id] || null,
      labels: id => labels(resolved()[id], {mac}),
      all: () => {
        const map = resolved();
        return REGISTRY.map(entry => ({
          ...entry,
          binding: map[entry.id],
          labels: labels(map[entry.id], {mac}),
          isDefault: !Object.hasOwn(overrides, entry.id),
        }));
      },
      actionFor: event => actionFor(event, overrides, {mac}),
      set(id, binding) {
        const change = planChange(id, binding, overrides, {mac});
        if (!change.ok) return change;
        overrides = change.overrides;
        saveOverrides(storage, overrides);
        emit();
        return {ok: true, binding: change.binding};
      },
      reset(id) {
        if (!Object.hasOwn(overrides, id)) return {ok: true};
        const next = {...overrides};
        delete next[id];
        overrides = next;
        saveOverrides(storage, overrides);
        emit();
        return {ok: true};
      },
      resetAll() {
        if (!Object.keys(overrides).length) return {ok: true};
        overrides = {};
        saveOverrides(storage, overrides);
        emit();
        return {ok: true};
      },
      conflicts: () => conflicts(resolved(), {mac}),
      subscribe(listener) {
        listeners.add(listener);
        return () => listeners.delete(listener);
      },
    };
  }

  // ---------------------------------------------------------------- the page

  function element(doc, tag, className, text) {
    const node = doc.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
  }

  /** Fill the shortcuts dialog from the registry, keeping its static typing rows. */
  function renderDialog(instance) {
    if (typeof document === 'undefined') return;
    const list = document.getElementById('shortcut-list');
    if (!list) return;
    for (const row of list.querySelectorAll('[data-shortcut-row]')) row.remove();
    for (const item of instance.all()) {
      const row = document.createElement('div');
      row.setAttribute('data-shortcut-row', item.id);
      const description = element(document, 'dt', '', item.description);
      const binding = document.createElement('dd');
      for (const label of item.labels) binding.append(element(document, 'kbd', '', label));
      row.append(description, binding);
      list.append(row);
    }
  }

  /**
   * The Settings list: one row per action, its keys as a button that records
   * the next keypress, a reset for changed rows, and an inline warning when a
   * key is already taken. Nothing here invents a colour or a second registry.
   */
  function mount(host, instance) {
    const doc = host.ownerDocument || document;
    let recording = null;

    const list = element(doc, 'div', 'settings-rows');
    const rows = new Map();

    function messageFor(result) {
      if (result.error === 'conflict') {
        const names = (result.conflictWith || []).map(id => BY_ID.get(id)?.description).filter(Boolean);
        return names.length ? `Already used by ${names.join(', ')}.` : 'That key is already in use.';
      }
      if (result.error === 'needs-modifier') return 'Use Ctrl or Alt with that key.';
      return 'That key cannot be used as a shortcut.';
    }

    function updateRow(row) {
      const item = instance.all().find(entry => entry.id === row.id);
      if (!item) return;
      row.button.replaceChildren(...item.labels.map(label => element(doc, 'kbd', '', label)));
      row.button.setAttribute('aria-label',
        `Change the shortcut for ${item.description}. Current keys: ${item.labels.join(' ')}.`);
      row.reset.hidden = item.isDefault;
      row.reset.setAttribute('aria-label', `Reset the shortcut for ${item.description}`);
      row.note.textContent = item.isDefault ? '' : `Default: ${labels(item.defaultKey, {mac: instance.mac}).join(' ')}`;
    }

    function stopRecording() {
      if (!recording) return;
      const row = rows.get(recording);
      recording = null;
      doc.removeEventListener?.('keydown', capture, true);
      if (row) {
        row.error.hidden = true;
        row.error.textContent = '';
        updateRow(row);
      }
    }

    function capture(event) {
      if (!recording) return;
      event.preventDefault?.();
      event.stopPropagation?.();
      if (event.isComposing) return;
      if (eventKey(event) === 'escape') { stopRecording(); return; }
      const binding = bindingFromEvent(event, {mac: instance.mac});
      if (!binding) return;  // a modifier alone: the next real key still counts
      const row = rows.get(recording);
      const result = instance.set(recording, binding);
      stopRecording();
      if (!result.ok && row) {
        row.error.textContent = messageFor(result);
        row.error.hidden = false;
      }
    }

    function record(row) {
      if (recording === row.id) { stopRecording(); return; }
      stopRecording();
      recording = row.id;
      row.error.hidden = true;
      row.button.textContent = 'Press keys…';
      row.button.setAttribute('aria-label', `Press the new keys for ${row.description}. Escape cancels.`);
      doc.addEventListener?.('keydown', capture, true);
    }

    for (const item of instance.all()) {
      const row = element(doc, 'div', 'settings-row shortcut-row');
      const copy = element(doc, 'div', 'settings-copy');
      const title = element(doc, 'span', 'settings-row-title', item.description);
      const note = element(doc, 'p', 'settings-row-help');
      const error = element(doc, 'p', 'settings-row-help shortcut-error');
      error.setAttribute('role', 'status');
      error.setAttribute('aria-live', 'polite');
      error.hidden = true;
      copy.append(title, note, error);

      const button = element(doc, 'button', 'btn small shortcut-key');
      button.type = 'button';
      button.addEventListener('click', () => record(rowState));
      const reset = element(doc, 'button', 'btn small quiet', 'Reset');
      reset.type = 'button';
      reset.addEventListener('click', () => { stopRecording(); instance.reset(item.id); });

      row.append(copy, button, reset);
      list.append(row);
      const rowState = {id: item.id, description: item.description, button, reset, note, error};
      rows.set(item.id, rowState);
      updateRow(rowState);
    }

    const foot = element(doc, 'div', 'shortcut-remap-foot');
    const resetAll = element(doc, 'button', 'btn small quiet', 'Reset all');
    resetAll.type = 'button';
    resetAll.addEventListener('click', () => { stopRecording(); instance.resetAll(); });
    foot.append(resetAll);

    host.replaceChildren(list, foot);

    instance.subscribe(() => {
      for (const row of rows.values()) updateRow(row);
      resetAll.disabled = instance.all().every(item => item.isDefault);
    });
    resetAll.disabled = instance.all().every(item => item.isDefault);
  }

  const current = create({});

  function init() {
    if (typeof document === 'undefined' || !document.getElementById) return;
    renderDialog(current);
    current.subscribe(() => renderDialog(current));
    const host = document.getElementById('shortcut-remap');
    if (host) mount(host, current);
  }

  if (typeof document !== 'undefined') {
    if (document.readyState === 'loading') document.addEventListener?.('DOMContentLoaded', init);
    else init();
  }

  globalThis.KovaShortcuts = {
    STORAGE_KEY, REGISTRY, create, current,
    isMac, parse, formatBinding, chordOf, labels, isUsable, matches, actionFor,
    bindingFromEvent, normalizeOverrides, resolveOverrides, conflicts, planChange,
    loadOverrides, saveOverrides, renderDialog, mount,
  };
})();
