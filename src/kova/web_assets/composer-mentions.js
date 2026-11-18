/* @-mentions in the message box (issue #197).
 *
 * Typing @ opens a listbox of paths in the chat's working folder. Arrow keys
 * move, Enter or Tab inserts @path, Escape closes. The heavy lifting is pure
 * text: mentionAt() finds the token under the caret and insertMention() splices
 * the chosen path in, so the list and the field cannot disagree. app.js owns
 * the keydown handler and the request that lists the paths.
 */
(() => {
  'use strict';
  const MAX_OPTIONS = 20;
  const TOKEN = /(?:^|\s)@([^\s@]*)$/;

  // The @token being typed before ``caret``, or null. Start is the @ itself.
  function mentionAt(text, caret) {
    const before = String(text ?? '').slice(0, Math.max(0, Number(caret) || 0));
    const match = TOKEN.exec(before);
    if (!match) return null;
    return {start: before.length - match[1].length - 1, fragment: match[1]};
  }

  // Replace the @token with @path. Files get a trailing space so typing can
  // continue; a folder keeps its slash so the next @token narrows it.
  function insertMention(text, caret, path) {
    const value = String(text ?? '');
    const at = Number(caret) || 0;
    const token = mentionAt(value, at);
    if (!token) return {value, caret: at};
    const inserted = `@${String(path ?? '').replace(/^@/, '')}`;
    const after = value.slice(at);
    const gap = inserted.endsWith('/') || /^\s/.test(after) ? '' : ' ';
    const next = value.slice(0, token.start) + inserted + gap + after;
    return {value: next, caret: token.start + inserted.length + gap.length};
  }

  function attach(options = {}) {
    const {textarea, listbox} = options;
    if (!textarea || !listbox) return null;
    const paths = typeof options.paths === 'function' ? options.paths : async () => [];
    const changed = typeof options.onChange === 'function' ? options.onChange : () => {};
    let items = [];
    let active = 0;
    let token = null;
    let sequence = 0;
    let timer = null;

    const visible = () => !listbox.hidden && items.length > 0;

    function paint() {
      listbox.hidden = !items.length;
      textarea.setAttribute('aria-expanded', String(visible()));
      if (!items.length) {
        textarea.removeAttribute('aria-activedescendant');
        return;
      }
      listbox.replaceChildren();
      items.forEach((path, index) => {
        const option = document.createElement('div');
        option.className = `mention-option${index === active ? ' active' : ''}`;
        option.id = `mention-option-${index}`;
        option.setAttribute('role', 'option');
        option.setAttribute('aria-selected', String(index === active));
        option.textContent = path;
        // Keep the caret in the field: a click must not blur the composer
        // before the click lands.
        option.onmousedown = event => event.preventDefault();
        option.onclick = () => choose(index);
        listbox.append(option);
      });
      const current = listbox.children[active];
      if (current?.scrollIntoView) current.scrollIntoView({block: 'nearest'});
      textarea.setAttribute('aria-activedescendant', `mention-option-${active}`);
    }

    function close() {
      token = null;
      items = [];
      active = 0;
      sequence += 1;
      if (timer !== null) { clearTimeout(timer); timer = null; }
      listbox.hidden = true;
      listbox.replaceChildren();
      textarea.setAttribute('aria-expanded', 'false');
      textarea.removeAttribute('aria-activedescendant');
    }

    function choose(index) {
      if (!token || !items.length) return;
      active = Math.max(0, Math.min(index, items.length - 1));
      const result = insertMention(textarea.value, textarea.selectionStart, items[active]);
      const chosen = token;
      close();
      textarea.value = result.value;
      try { textarea.setSelectionRange(result.caret, result.caret); } catch (_) { /* detached field */ }
      textarea.focus();
      changed(chosen);
    }

    async function load(fragment) {
      const ask = ++sequence;
      let found = [];
      try { found = await paths(fragment); } catch (_) { found = []; }
      // A newer keystroke or a closed list wins over this answer.
      if (ask !== sequence || !token || token.fragment !== fragment) return;
      items = (Array.isArray(found) ? found : [])
        .filter(path => typeof path === 'string' && path)
        .slice(0, MAX_OPTIONS);
      active = 0;
      paint();
    }

    // Called on every input, including text put there by the history browser.
    function input() {
      const next = mentionAt(textarea.value, textarea.selectionStart);
      if (!next) { if (token) close(); return; }
      if (token && token.fragment === next.fragment) { token = next; return; }
      token = next;
      items = [];
      active = 0;
      listbox.hidden = true;
      textarea.setAttribute('aria-expanded', 'false');
      textarea.removeAttribute('aria-activedescendant');
      const fragment = next.fragment;
      if (timer !== null) clearTimeout(timer);
      timer = setTimeout(() => { timer = null; load(fragment); }, 120);
    }

    function keydown(event) {
      if (!event || !visible()) return false;
      if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
        event.preventDefault();
        active = (active + (event.key === 'ArrowDown' ? 1 : -1) + items.length) % items.length;
        paint();
        return true;
      }
      if (event.key === 'Enter' || event.key === 'Tab') {
        event.preventDefault();
        choose(active);
        return true;
      }
      if (event.key === 'Escape') {
        event.preventDefault();
        event.stopPropagation?.();
        close();
        return true;
      }
      return false;
    }

    // A click elsewhere closes the list; the field keeps focus on its own.
    const blur = event => {
      if (!event?.relatedTarget || !listbox.contains?.(event.relatedTarget)) close();
    };
    textarea.addEventListener?.('blur', blur);

    return {keydown, input, close, get open() { return visible(); }};
  }

  globalThis.KovaComposerMentions = {attach, mentionAt, insertMention, MAX_OPTIONS};
})();
