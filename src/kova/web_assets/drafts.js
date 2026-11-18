/* Per-chat composer drafts (#197).
 *
 * An unsent message belongs to the conversation it was typed in. It survives a
 * reload and comes back when that chat is opened again; sending or deleting the
 * chat clears it. Incognito chats have no key at all, so their text never
 * reaches browser storage.
 *
 * The text lives in localStorage for this browser only; it is not a transcript
 * and is never sent anywhere. Storage failures (private mode, a full quota)
 * simply mean the draft lasts for this session. tests/web_drafts.mjs exercises
 * this file with a small localStorage stand-in.
 */
(() => {
  'use strict';

  const PREFIX = 'kova-draft:';
  const fallback = new Map();

  // Only a saved chat owns a draft. A blank start screen has no id yet and an
  // incognito id is deliberately excluded.
  function key(chat) {
    const id = chat && typeof chat.id === 'string' ? chat.id : '';
    return id && !chat.ephemeral && !id.startsWith('incognito-') ? PREFIX + id : null;
  }

  function read(chat) {
    const name = key(chat);
    if (!name) return '';
    if (fallback.has(name)) return fallback.get(name);
    try {
      const value = localStorage.getItem(name);
      return typeof value === 'string' ? value : '';
    } catch (_) {
      return '';
    }
  }

  function write(chat, text) {
    const name = key(chat);
    if (!name) return false;
    const value = typeof text === 'string' && text.trim() ? text : '';
    try {
      if (value) localStorage.setItem(name, value);
      else localStorage.removeItem(name);
      fallback.delete(name);
      return true;
    } catch (_) {
      fallback.set(name, value);
      return false;
    }
  }

  function clear(chat) {
    write(chat, '');
  }

  globalThis.KovaDrafts = {PREFIX, key, read, write, clear};
})();
