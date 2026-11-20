/* Kova command palette (#183).
 *
 * One dialog, one search box, one list of commands and conversations. app.js
 * supplies the registry (because every action already lives there); this file
 * owns the dialog, the fuzzy filter, the keyboard model and the ARIA wiring,
 * so the palette can be exercised on its own in tests/web_palette.mjs.
 *
 * No network access, no innerHTML: every row is built from text nodes.
 */
(() => {
  'use strict';

  const WORD_START = /[\s\-_/.:·]/;

  /** How well `text` matches `query`; null when it does not match at all.
   *
   * A match is a subsequence, so `nwch` finds "New conversation". Word starts,
   * contiguous runs and an exact/prefix match score higher; long titles pay a
   * small penalty so a short command beats a long conversation title. */
  function fuzzyScore(query, text) {
    const needle = String(query == null ? '' : query).toLowerCase().trim();
    const haystack = String(text == null ? '' : text).toLowerCase();
    if (!needle) return 0;
    if (!haystack) return null;
    let score = 0;
    let from = 0;
    let previous = -2;
    let run = 0;
    for (const character of needle) {
      if (character === ' ') continue;
      let found = -1;
      for (let index = from; index < haystack.length; index += 1) {
        if (haystack[index] === character) { found = index; break; }
      }
      if (found < 0) return null;
      if (found === previous + 1) { run += 1; score += 5 + run * 2; }
      else { run = 0; score += 2; }
      if (found === 0 || WORD_START.test(haystack[found - 1])) score += 8;
      score -= Math.min(found - from, 12) * 0.5;
      previous = found;
      from = found + 1;
    }
    if (haystack.startsWith(needle)) score += 24;
    if (haystack === needle) score += 40;
    score -= Math.min(haystack.length, 120) * 0.15;
    return score;
  }

  function scoreItem(item, query) {
    const title = fuzzyScore(query, item.title);
    const keywords = fuzzyScore(query, item.keywords);
    // A row the server already filtered (a content search hit whose match sits
    // in its snippet) is kept even when the query is not in its own text.
    if (title === null && keywords === null) return item.alwaysMatch ? -1 : null;
    // A keyword hit ranks below the same hit in the visible title.
    if (title !== null && keywords !== null) return Math.max(title, keywords - 4);
    return title !== null ? title : keywords - 10;
  }

  /** Registry order with no query; best group first and best row first otherwise.
   *
   * Groups stay contiguous (never interleaved) so the palette's headings remain
   * honest while a query is active. */
  function rankItems(items, query) {
    const needle = String(query == null ? '' : query).trim();
    if (!needle) return items.slice();
    const groups = new Map();
    items.forEach((item, index) => {
      const score = scoreItem(item, needle);
      if (score === null) return;
      const name = item.group || 'Commands';
      if (!groups.has(name)) groups.set(name, {best: -Infinity, first: index, entries: []});
      const group = groups.get(name);
      group.best = Math.max(group.best, score);
      group.entries.push({item, index, score});
    });
    return [...groups.values()]
      .sort((a, b) => b.best - a.best || a.first - b.first)
      .flatMap(group => group.entries
        .sort((a, b) => b.score - a.score || a.index - b.index)
        .map(entry => entry.item));
  }

  function keysOf(shortcut) {
    if (!shortcut) return [];
    const keys = Array.isArray(shortcut) ? shortcut : String(shortcut).split(/\s+/);
    return keys.filter(Boolean).map(String);
  }

  function node(doc, tag, className, text) {
    const element = doc.createElement(tag);
    if (className) element.className = className;
    if (text !== undefined) element.textContent = text;
    return element;
  }

  /**
   * Create the palette and attach its (closed) dialog to the document.
   *
   * options.items is a function returning the current registry, re-read on
   * every open, refresh and keystroke, so chats, models and enabled actions are
   * never stale. Optional: onOpen, onClose, onQuery, onError, label, placeholder.
   *
   * A row may carry `preview()`, run whenever it becomes the highlighted row, and
   * `previewEnd()`, run when the highlight leaves it or the palette closes, so a
   * reversible change (a theme, say) can be shown before Enter commits it. Give
   * rows a stable `previewKey` when the registry is rebuilt on every keystroke:
   * rows with the same key are the same preview, so it is not restarted.
   */
  function create(options = {}) {
    const doc = options.doc || globalThis.document;
    if (!doc || !doc.body) throw new Error('KovaPalette needs a document with a body');
    const provider = typeof options.items === 'function' ? options.items : () => options.items || [];
    const onError = options.onError || (() => {});
    const label = options.label || 'Command palette';
    const id = options.id || 'command-palette';

    let open = false;
    let active = -1;
    let restore = null;
    let entries = [];
    let previewing = null;   // {item, key}: the row whose preview is showing

    const dialog = node(doc, 'dialog', 'palette');
    dialog.id = id;
    dialog.setAttribute('aria-labelledby', `${id}-title`);
    const title = node(doc, 'h2', 'palette-title', label);
    title.id = `${id}-title`;

    const panel = node(doc, 'div', 'palette-panel');
    // A label, so the whole field row focuses the input and the target is the
    // row rather than its ~21px text line (#201).
    const field = node(doc, 'label', 'palette-field');
    const searchIcon = node(doc, 'span', 'icon i-search');
    searchIcon.setAttribute('aria-hidden', 'true');
    const input = node(doc, 'input', 'palette-input');
    input.id = `${id}-input`;
    input.type = 'text';
    input.autocomplete = 'off';
    input.spellcheck = false;
    input.placeholder = options.placeholder || 'Search commands, conversations and settings…';
    input.setAttribute('role', 'combobox');
    input.setAttribute('aria-expanded', 'false');
    input.setAttribute('aria-haspopup', 'listbox');
    input.setAttribute('aria-controls', `${id}-results`);
    input.setAttribute('aria-autocomplete', 'list');
    input.setAttribute('aria-label', options.inputLabel || 'Search commands and conversations');
    const escapeHint = node(doc, 'kbd', 'palette-escape', 'Esc');
    escapeHint.setAttribute('aria-hidden', 'true');
    field.append(searchIcon, input, escapeHint);

    const list = node(doc, 'div', 'palette-list');
    list.id = `${id}-results`;
    list.setAttribute('role', 'listbox');
    list.setAttribute('aria-label', options.resultsLabel || 'Commands and conversations');

    const empty = node(doc, 'p', 'palette-empty', options.emptyText || 'Nothing matches. Try a shorter word.');
    empty.hidden = true;
    const status = node(doc, 'p', 'palette-status');
    status.setAttribute('role', 'status');
    status.setAttribute('aria-live', 'polite');

    const footer = node(doc, 'div', 'palette-foot');
    footer.setAttribute('aria-hidden', 'true');
    for (const [keys, text] of [[['↑', '↓'], 'move'], [['Enter'], 'run'], [['Esc'], 'close']]) {
      const group = node(doc, 'span', 'palette-foot-key');
      for (const key of keys) group.append(node(doc, 'kbd', '', key));
      group.append(node(doc, 'span', '', text));
      footer.append(group);
    }

    panel.append(title, field, list, empty, footer, status);
    dialog.append(panel);
    doc.body.append(dialog);

    function stopPreview() {
      if (!previewing) return;
      const {item} = previewing;
      previewing = null;
      if (typeof item.previewEnd !== 'function') return;
      try { item.previewEnd(); } catch (error) { onError(error); }
    }

    function startPreview(item) {
      const live = item && typeof item.preview === 'function' ? item : null;
      const key = live ? (live.previewKey === undefined ? live.preview : live.previewKey) : null;
      if (previewing && previewing.key === key) return;
      stopPreview();
      if (!live) return;
      previewing = {item: live, key};
      try { live.preview(); } catch (error) { onError(error); }
    }

    function setActive(index, scroll = true) {
      if (!entries.length) {
        active = -1;
        input.removeAttribute('aria-activedescendant');
        return;
      }
      const bounded = ((index % entries.length) + entries.length) % entries.length;
      if (active >= 0 && entries[active]) entries[active].node.setAttribute('aria-selected', 'false');
      active = bounded;
      const option = entries[active].node;
      option.setAttribute('aria-selected', 'true');
      input.setAttribute('aria-activedescendant', option.id);
      if (scroll) option.scrollIntoView?.({block: 'nearest'});
      startPreview(entries[active].item);
    }

    function render() {
      const ranked = rankItems(provider() || [], input.value);
      entries = [];
      list.replaceChildren();
      let groupName = null;
      let groupNode = null;
      ranked.forEach((item, index) => {
        const name = item.group || 'Commands';
        if (name !== groupName) {
          groupName = name;
          groupNode = node(doc, 'div', 'palette-group');
          groupNode.setAttribute('role', 'group');
          const groupLabel = node(doc, 'div', 'palette-group-label', name);
          groupLabel.id = `${id}-group-${index}`;
          groupNode.setAttribute('aria-labelledby', groupLabel.id);
          groupNode.append(groupLabel);
          list.append(groupNode);
        }
        const option = node(doc, 'div', 'palette-option');
        option.id = `${id}-option-${index}`;
        option.setAttribute('role', 'option');
        option.setAttribute('aria-selected', 'false');
        option.setAttribute('tabindex', '-1');
        const text = node(doc, 'span', 'palette-option-text');
        text.append(node(doc, 'span', 'palette-option-title', item.title));
        if (item.hint) text.append(node(doc, 'span', 'palette-option-hint', item.hint));
        option.append(text);
        const keys = keysOf(item.shortcut);
        if (keys.length) {
          const wrap = node(doc, 'span', 'palette-option-keys');
          for (const key of keys) wrap.append(node(doc, 'kbd', '', key));
          option.append(wrap);
        }
        option.onclick = () => choose(index);
        option.addEventListener?.('pointermove', () => { if (active !== index) setActive(index, false); });
        groupNode.append(option);
        entries.push({node: option, item});
      });
      empty.hidden = ranked.length > 0;
      status.textContent = ranked.length
        ? `${ranked.length} result${ranked.length === 1 ? '' : 's'}`
        : 'No results';
      if (ranked.length) setActive(0, false);
      else { active = -1; input.removeAttribute('aria-activedescendant'); stopPreview(); }
    }

    function choose(index) {
      const entry = entries[index];
      if (!entry) return;
      close();
      try {
        const result = entry.item.run?.();
        if (result && typeof result.catch === 'function') result.catch(error => onError(error));
      } catch (error) { onError(error); }
    }

    function settle() {
      stopPreview();
      input.setAttribute('aria-expanded', 'false');
      input.removeAttribute('aria-activedescendant');
      active = -1;
    }

    function restoreFocus() {
      const target = restore;
      restore = null;
      if (target && typeof target.focus === 'function') target.focus();
    }

    function close() {
      if (!open) return;
      open = false;
      settle();
      try { dialog.close?.(); } catch (_) { /* a dialog that was never shown */ }
      if (typeof dialog.close !== 'function') dialog.removeAttribute('open');
      restoreFocus();
      options.onClose?.();
    }

    function openPalette() {
      if (open) return;
      open = true;
      restore = doc.activeElement || null;
      input.value = '';
      input.setAttribute('aria-expanded', 'true');
      render();
      try { dialog.showModal?.(); } catch (_) { dialog.setAttribute('open', ''); }
      if (typeof dialog.showModal !== 'function') dialog.setAttribute('open', '');
      input.focus();
      options.onOpen?.();
    }

    function refresh() {
      if (open) render();
    }

    function keydown(event) {
      if (!open || event.isComposing) return;
      const key = event.key;
      if (key === 'Escape') { event.preventDefault(); event.stopPropagation?.(); close(); return; }
      // The input is the only tab stop; keep focus there instead of leaving it.
      if (key === 'Tab') { event.preventDefault(); input.focus(); return; }
      if (key === 'ArrowDown') { event.preventDefault(); setActive(active + 1); return; }
      if (key === 'ArrowUp') { event.preventDefault(); setActive(active - 1); return; }
      if (key === 'PageDown') { event.preventDefault(); setActive(active + 6); return; }
      if (key === 'PageUp') { event.preventDefault(); setActive(active - 6); return; }
      if (key === 'Home') { event.preventDefault(); setActive(0); return; }
      if (key === 'End') { event.preventDefault(); setActive(entries.length - 1); return; }
      if (key === 'Enter' && entries.length) { event.preventDefault(); choose(active); }
    }

    input.addEventListener('input', () => {
      options.onQuery?.(input.value);
      render();
    });
    dialog.addEventListener?.('keydown', keydown);
    dialog.addEventListener?.('cancel', event => { event.preventDefault(); close(); });
    dialog.addEventListener?.('click', event => { if (event.target === dialog) close(); });
    dialog.addEventListener?.('close', () => {
      if (!open) return;
      open = false;
      settle();
      restoreFocus();
      options.onClose?.();
    });

    return {
      dialog,
      input,
      open: openPalette,
      close,
      refresh,
      isOpen: () => open,
      openCount: () => entries.length,
    };
  }

  globalThis.KovaPalette = {create, fuzzyScore, rankItems};
})();
