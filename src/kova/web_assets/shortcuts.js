/* Kova keyboard shortcuts (#200).
 *
 * One registry for the web and desktop app: every action has an id, its default
 * key and the sentence that describes it. app.js dispatches the document's
 * chords through it, the palette and the shortcuts dialog read their labels
 * from it, and the Settings row lets the user record new keys.
 *
 * A user's changes are overrides in the shared UI preferences (#200):
 * `shortcuts` in the server's preferences.json, so every paired device and
 * `kova prefs export` see them. localStorage keeps a copy under
 * `kova-shortcuts` for an instant first paint, and the keys that lived only
 * there are migrated to the server once. Only the actions that differ from
 * their default are written, so a corrupt or partial document always falls
 * back to a working keyboard. A binding that would collide with another action
 * is refused rather than saved: two actions can never share a chord.
 *
 * Three presets - Default, Vim-ish and Emacs-ish - are whole override maps;
 * the preset in use is read back from the map, so recording one key afterwards
 * makes it "Custom" without another preference to keep in step.
 *
 * A binding is a lowercase string such as "mod+shift+k". "mod" is Ctrl on
 * Windows and Linux and Command on macOS; ctrl, meta, alt and shift are the
 * literal keys. The file is plain logic plus the remap list and the dialog,
 * with no innerHTML, so tests/web_shortcuts.mjs can exercise it with a small
 * stand-in for the page.
 */
(() => {
  'use strict';

  const STORAGE_KEY = 'kova-shortcuts';
  // One-time marker: the keys saved here before sync have reached the server.
  const SYNCED_KEY = 'kova-shortcuts-synced';
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
  // Keys that already mean something in a text field. A binding without Ctrl,
  // Alt or Cmd that uses one of them only fires outside a form control, so a
  // preset like Alt+J can never steal typing from the message box.
  const TYPING_KEYS = new Set([
    'enter', 'space', 'tab', 'backspace', 'delete', 'arrowup', 'arrowdown',
    'arrowleft', 'arrowright', 'home', 'end', 'pageup', 'pagedown',
  ]);
  const KEY_LABELS = {
    escape: 'Esc', enter: 'Enter', tab: 'Tab', space: 'Space', backspace: 'Backspace',
    delete: 'Delete', insert: 'Insert', home: 'Home', end: 'End', pageup: 'PageUp',
    pagedown: 'PageDown', arrowup: '↑', arrowdown: '↓', arrowleft: '←', arrowright: '→',
  };
  const ARIA_KEYS = {
    escape: 'Escape', enter: 'Enter', tab: 'Tab', space: 'Space',
    backspace: 'Backspace', delete: 'Delete', insert: 'Insert', home: 'Home',
    end: 'End', pageup: 'PageUp', pagedown: 'PageDown', arrowup: 'ArrowUp',
    arrowdown: 'ArrowDown', arrowleft: 'ArrowLeft', arrowright: 'ArrowRight',
  };

  /** The actions the app knows, in the order the shortcuts dialog lists them. */
  const REGISTRY = Object.freeze([
    {id: 'palette', defaultKey: 'mod+k', description: 'Open the command palette'},
    {id: 'new-conversation', defaultKey: 'mod+shift+k', description: 'New conversation'},
    {id: 'new-incognito', defaultKey: 'mod+shift+n', description: 'New incognito conversation'},
    {id: 'search-conversations', defaultKey: 'mod+shift+f', description: 'Search conversations'},
    {id: 'previous-conversation', defaultKey: 'alt+arrowup', description: 'Previous conversation'},
    {id: 'next-conversation', defaultKey: 'alt+arrowdown', description: 'Next conversation'},
    {id: 'toggle-sidebar', defaultKey: 'mod+b', description: 'Show or hide the sidebar'},
    {id: 'toggle-panel', defaultKey: 'mod+alt+b', description: 'Show or hide the side panel'},
    {id: 'next-pane', defaultKey: 'f6', description: 'Move focus to the next pane'},
    {id: 'previous-pane', defaultKey: 'shift+f6', description: 'Move focus to the previous pane'},
    {id: 'toggle-theme', defaultKey: 'mod+shift+l', description: 'Switch light and dark'},
    {id: 'focus-composer', defaultKey: 'mod+space', description: 'Focus the message box'},
    {id: 'stop-reply', defaultKey: 'escape', description: 'Stop the reply'},
    {id: 'shortcuts', defaultKey: 'mod+/', description: 'Keyboard shortcuts'},
  ]);
  const BY_ID = new Map(REGISTRY.map(entry => [entry.id, entry]));

  /**
   * Whole override maps. Every key carries a modifier, so no preset can steal
   * typing from the message box; the ones that move through the conversation
   * list follow the editor they are named after.
   */
  const PRESETS = Object.freeze({
    default: {
      label: 'Default',
      overrides: {},
    },
    vim: {
      label: 'Vim-ish',
      overrides: {
        palette: 'mod+shift+p',
        'previous-conversation': 'alt+k',
        'next-conversation': 'alt+j',
      },
    },
    emacs: {
      label: 'Emacs-ish',
      overrides: {
        palette: 'alt+x',
        'previous-conversation': 'alt+p',
        'next-conversation': 'alt+n',
      },
    },
  });
  const PRESET_NAMES = Object.freeze(['default', 'vim', 'emacs']);

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

  /** The aria-keyshortcuts value; "mod" names both Ctrl and Command. */
  function ariaKeyshortcuts(binding) {
    const parts = parse(binding);
    if (!parts) return '';
    const key = ARIA_KEYS[parts.key] || (parts.key.length === 1 ? parts.key.toUpperCase() : parts.key);
    const combos = [];
    for (const modifier of parts.mod ? ['Control', 'Meta'] : [null]) {
      const names = new Set();
      if (parts.ctrl) names.add('Control');
      if (modifier) names.add(modifier);
      if (parts.meta) names.add('Meta');
      if (parts.alt) names.add('Alt');
      if (parts.shift) names.add('Shift');
      combos.push([...names, key].join('+'));
    }
    return [...new Set(combos)].join(' ');
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

  /**
   * Does this binding already mean something inside a text field?
   *
   * A command modifier makes a chord safe to own anywhere; without one, a
   * printable key or a cursor key belongs to the person typing.
   */
  function disruptiveWhileTyping(binding) {
    const parts = parse(binding);
    if (!parts) return true;
    if (parts.mod || parts.ctrl || parts.meta) return false;
    return PRINTABLE.test(parts.key) || TYPING_KEYS.has(parts.key);
  }

  /** The action a keydown event runs, or null when no binding matches it. */
  function actionFor(event, overrides = {}, options = {}) {
    const bindings = resolveOverrides(overrides);
    for (const entry of REGISTRY) {
      if (options.guardTyping && disruptiveWhileTyping(bindings[entry.id])) continue;
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

  /** Two override maps hold the same actions with the same keys. */
  function sameOverrides(left, right) {
    const first = normalizeOverrides(left);
    const second = normalizeOverrides(right);
    const keys = Object.keys(first);
    return keys.length === Object.keys(second).length
      && keys.every(key => first[key] === second[key]);
  }

  /** The complete map: every default, with the user's overrides on top. */
  function resolveOverrides(overrides = {}) {
    const clean = normalizeOverrides(overrides);
    const resolved = {};
    for (const entry of REGISTRY) resolved[entry.id] = clean[entry.id] || entry.defaultKey;
    return resolved;
  }

  /** The preset a map is exactly: one of PRESET_NAMES, or "custom". */
  function presetOf(overrides = {}) {
    for (const name of PRESET_NAMES) {
      if (sameOverrides(overrides, PRESETS[name].overrides)) return name;
    }
    return 'custom';
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

  function syncedBefore(storage) {
    try { return storage?.getItem?.(SYNCED_KEY) === '1'; } catch (_) { return false; }
  }

  function markSynced(storage) {
    try { storage?.setItem?.(SYNCED_KEY, '1'); } catch (_) { /* storage is optional */ }
  }

  /**
   * One app instance: the overrides in memory, kept in localStorage for the
   * first paint, saved to the shared preferences on change, and adopted from
   * them once the page has loaded.
   */
  function create(options = {}) {
    const mac = options.mac === undefined ? isMac(options.platform) : Boolean(options.mac);
    const storage = options.storage === undefined ? safeStorage() : options.storage;
    const fetchImpl = options.fetchImpl === undefined ? globalThis.fetch : options.fetchImpl;
    const sync = options.sync !== false && typeof fetchImpl === 'function';
    let overrides = loadOverrides(storage);
    let touched = false;
    const listeners = new Set();
    const resolved = () => resolveOverrides(overrides);
    const emit = () => {
      for (const listener of [...listeners]) {
        try { listener(); } catch (_) { /* a broken panel must not block the save */ }
      }
    };

    let lastPushed = null;
    const push = () => {
      if (!sync) return;
      const body = JSON.stringify({shortcuts: normalizeOverrides(overrides)});
      if (body === lastPushed) return;  // the server already has this exact map
      lastPushed = body;
      try {
        const pending = fetchImpl('/preferences', {
          method: 'PUT',
          headers: {'Content-Type': 'application/json'},
          body,
        });
        if (pending && typeof pending.catch === 'function') pending.catch(() => {});
      } catch (_) { /* offline: the local copy is still right */ }
    };

    const commit = next => {
      overrides = normalizeOverrides(next);
      touched = true;
      saveOverrides(storage, overrides);
      markSynced(storage);
      push();
      emit();
    };

    let settle;
    const ready = new Promise(resolve => { settle = resolve; });

    async function hydrate() {
      if (!sync) { settle(); return; }
      try {
        const response = await fetchImpl('/preferences', {headers: {'Accept': 'application/json'}});
        if (!response || !response.ok) return;
        const payload = await response.json();
        const remote = normalizeOverrides(payload && payload.shortcuts);
        if (touched) {
          // A key recorded before the first reply is newer than the document.
          push();
        } else if (!syncedBefore(storage) && !Object.keys(remote).length
                   && Object.keys(overrides).length) {
          // The keys from before sync live only in this browser: send them once.
          push();
        } else if (!sameOverrides(remote, overrides)) {
          overrides = remote;
          saveOverrides(storage, overrides);
          emit();
        }
        markSynced(storage);
      } catch (_) {
        /* offline: the cached keys are still the working set */
      } finally {
        settle();
      }
    }

    const instance = {
      mac,
      registry: REGISTRY,
      presets: PRESET_NAMES.map(name => ({id: name, label: PRESETS[name].label})),
      ready,
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
      actionFor: (event, options = {}) => actionFor(event, overrides, {mac, ...options}),
      preset: () => presetOf(overrides),
      applyPreset(name) {
        const preset = PRESETS[name];
        if (!preset) return {ok: false, error: 'unknown-preset'};
        const next = normalizeOverrides(preset.overrides);
        if (conflicts(resolveOverrides(next), {mac}).length) {
          return {ok: false, error: 'conflict'};
        }
        commit(next);
        return {ok: true, preset: name};
      },
      set(id, binding) {
        const change = planChange(id, binding, overrides, {mac});
        if (!change.ok) return change;
        commit(change.overrides);
        return {ok: true, binding: change.binding};
      },
      reset(id) {
        if (!Object.hasOwn(overrides, id)) return {ok: true};
        const next = {...overrides};
        delete next[id];
        commit(next);
        return {ok: true};
      },
      resetAll() {
        if (!Object.keys(overrides).length) return {ok: true};
        commit({});
        return {ok: true};
      },
      conflicts: () => conflicts(resolved(), {mac}),
      subscribe(listener) {
        listeners.add(listener);
        return () => listeners.delete(listener);
      },
    };
    hydrate();
    return instance;
  }

  // ---------------------------------------------------------------- the page

  function element(doc, tag, className, text) {
    const node = doc.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
  }

  function normalizeQuery(query) {
    return String(query ?? '').trim().toLowerCase();
  }

  /** Does every word of a search appear in the text? Names and keys both count. */
  function matchesQuery(text, query) {
    const words = normalizeQuery(query).split(/[\s+]+/).filter(Boolean);
    if (!words.length) return true;
    const haystack = String(text ?? '').toLowerCase();
    return words.every(word => haystack.includes(word));
  }

  /**
   * Fill the shortcuts dialog from the registry and filter it by name or key.
   *
   * `options.query` is the search text; without it the dialog's own search box
   * is read, so a saved change re-renders without losing the filter. The
   * `doc` and `list` options let tests pass a small stand-in for the page.
   */
  function renderDialog(instance, options = {}) {
    const doc = options.doc || (typeof document === 'undefined' ? null : document);
    if (!doc) return;
    const list = options.list || doc.getElementById?.('shortcut-list');
    if (!list) return;
    const box = doc.getElementById?.('shortcut-search');
    const query = options.query === undefined ? String(box?.value ?? '') : options.query;
    for (const row of list.querySelectorAll('[data-shortcut-row]')) row.remove();
    let shown = 0;
    for (const item of instance.all()) {
      const row = doc.createElement('div');
      row.setAttribute('data-shortcut-row', item.id);
      const description = element(doc, 'dt', '', item.description);
      const binding = doc.createElement('dd');
      for (const label of item.labels) binding.append(element(doc, 'kbd', '', label));
      row.append(description, binding);
      row.hidden = !matchesQuery(
        [item.description, item.binding, ...item.labels].join(' '), query);
      if (!row.hidden) shown += 1;
      list.append(row);
    }
    // The typing keys are part of the same list, so they filter with it.
    for (const row of [...list.children]) {
      if (row.hasAttribute?.('data-shortcut-row')) continue;
      row.hidden = !matchesQuery(row.textContent, query);
      if (!row.hidden) shown += 1;
    }
    const empty = doc.getElementById?.('shortcut-empty');
    if (empty) {
      const needle = normalizeQuery(query);
      empty.hidden = shown > 0 || !needle;
      empty.textContent = `No shortcuts match “${needle}”.`;
    }
  }

  /**
   * Show the current keys on the controls that offer the action: the sidebar's
   * New chat, the composer's incognito row, the Stop button. The first paint
   * keeps the title the markup shipped, then the keys are appended to it.
   */
  function applyHints(instance, doc = typeof document === 'undefined' ? null : document) {
    if (!doc || typeof doc.querySelectorAll !== 'function') return;
    for (const node of doc.querySelectorAll('[data-shortcut]')) {
      const id = node.getAttribute?.('data-shortcut');
      const binding = id ? instance.binding(id) : null;
      if (!binding) continue;
      if (!node.hasAttribute?.('data-shortcut-title')) {
        const base = node.getAttribute?.('title') || node.getAttribute?.('aria-label')
          || String(node.textContent ?? '').trim();
        node.setAttribute('data-shortcut-title', base);
      }
      const base = node.getAttribute('data-shortcut-title');
      const keys = instance.labels(id);
      node.setAttribute('aria-keyshortcuts', ariaKeyshortcuts(binding));
      node.setAttribute('title', keys.length ? `${base} (${keys.join(' ')})` : base);
    }
  }

  /**
   * The Settings list: a preset for the whole keyboard, then one row per
   * action with its keys as a button that records the next keypress, a reset
   * for changed rows, and an inline warning when a key is already taken.
   * Nothing here invents a colour or a second registry.
   */
  function mount(host, instance) {
    const doc = host.ownerDocument || document;
    let recording = null;

    const list = element(doc, 'div', 'settings-rows');
    const rows = new Map();

    const presetRow = element(doc, 'div', 'settings-row shortcut-preset-row');
    const presetCopy = element(doc, 'div', 'settings-copy');
    presetCopy.append(
      element(doc, 'span', 'settings-row-title', 'Preset'),
      element(doc, 'p', 'settings-row-help',
        'Start from a familiar keyboard. Recording a key below makes it Custom.'),
    );
    const presetLabel = element(doc, 'label', 'sr-only', 'Shortcut preset');
    presetLabel.htmlFor = 'shortcut-preset';
    const preset = element(doc, 'select', 'shortcut-preset');
    preset.id = 'shortcut-preset';
    for (const name of PRESET_NAMES) {
      const option = element(doc, 'option', '', PRESETS[name].label);
      option.value = name;
      preset.append(option);
    }
    const custom = element(doc, 'option', '', 'Custom');
    custom.value = 'custom';
    preset.append(custom);
    presetRow.append(presetCopy, presetLabel, preset);

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

    function updatePreset() {
      const current = instance.preset();
      preset.value = current;
      custom.disabled = current !== 'custom';
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

    preset.addEventListener('change', () => {
      stopRecording();
      if (preset.value === 'custom') { updatePreset(); return; }
      instance.applyPreset(preset.value);
    });

    host.replaceChildren(presetRow, list, foot);

    instance.subscribe(() => {
      for (const row of rows.values()) updateRow(row);
      updatePreset();
      resetAll.disabled = instance.all().every(item => item.isDefault);
    });
    updatePreset();
    resetAll.disabled = instance.all().every(item => item.isDefault);
  }

  const current = create({});

  function init() {
    if (typeof document === 'undefined' || !document.getElementById) return;
    const search = document.getElementById('shortcut-search');
    search?.addEventListener?.('input', () => renderDialog(current, {query: search.value}));
    renderDialog(current);
    current.subscribe(() => {
      renderDialog(current);
      applyHints(current);
    });
    applyHints(current);
    const dialog = document.getElementById('shortcuts-dialog');
    dialog?.addEventListener?.('close', () => {
      if (search) search.value = '';
      renderDialog(current);
    });
    const host = document.getElementById('shortcut-remap');
    if (host) mount(host, current);
  }

  if (typeof document !== 'undefined') {
    if (document.readyState === 'loading') document.addEventListener?.('DOMContentLoaded', init);
    else init();
  }

  globalThis.KovaShortcuts = {
    STORAGE_KEY, SYNCED_KEY, REGISTRY, PRESETS, PRESET_NAMES, create, current,
    isMac, parse, formatBinding, chordOf, labels, ariaKeyshortcuts, isUsable,
    matches, actionFor, bindingFromEvent, normalizeOverrides, sameOverrides,
    resolveOverrides, presetOf, conflicts, planChange, loadOverrides,
    saveOverrides, renderDialog, applyHints, matchesQuery, mount,
  };
})();
