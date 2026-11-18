/* Input history for the message box (issue #197).
 *
 * Up at the start of an empty box brings back what you sent last, Down moves
 * forward and Escape puts back the draft the browsing started from. It only
 * acts while the caret is on the first line, so editing a multi-line message
 * keeps its own Up/Down behaviour. app.js owns the keydown listener and calls
 * in, so the composer works even when this file is missing from a cached shell.
 */
(() => {
  'use strict';

  // The user messages of one chat, oldest first. Empty lines are not history:
  // recalling one would overwrite the box with nothing.
  function recalled(messages) {
    return (Array.isArray(messages) ? messages : [])
      .filter(message => message && message.role === 'user' && typeof message.content === 'string')
      .map(message => message.content.trim())
      .filter(Boolean);
  }

  function onFirstLine(textarea) {
    const caret = Number(textarea.selectionStart) || 0;
    return !String(textarea.value).slice(0, caret).includes('\n');
  }

  // ``attach`` returns a handler instead of registering one: the composer has a
  // single keydown listener, and one handler keeps the order obvious.
  function attach(textarea, options = {}) {
    let index = null;   // where in the history the box currently is
    let draft = '';     // what was in the box before browsing started
    const messages = typeof options.messages === 'function' ? options.messages : () => options.messages;
    const changed = typeof options.onChange === 'function' ? options.onChange : () => {};

    function show(value) {
      textarea.value = value;
      const end = value.length;
      try { textarea.setSelectionRange(end, end); } catch (_) { /* detached field */ }
      changed();
    }

    // Stop browsing without touching the box; used when the chat changes or
    // the message goes out.
    function reset() {
      index = null;
      draft = '';
    }

    // Put the pre-browse draft back, the shell's Escape.
    function restore() {
      const value = draft;
      reset();
      show(value);
    }

    function keydown(event) {
      if (!event || event.isComposing || event.altKey || event.ctrlKey || event.metaKey) return false;
      if (event.key === 'Escape' && index !== null) {
        event.preventDefault();
        restore();
        return true;
      }
      if (event.key === 'ArrowUp' && onFirstLine(textarea) && (textarea.value === '' || index !== null)) {
        const history = recalled(messages());
        if (!history.length) return false;
        event.preventDefault();
        if (index === null) {
          draft = textarea.value;
          index = history.length - 1;
        } else if (index > 0) {
          index -= 1;
        }
        show(history[index]);
        return true;
      }
      if (event.key === 'ArrowDown' && index !== null) {
        event.preventDefault();
        const history = recalled(messages());
        if (index < history.length - 1) {
          index += 1;
          show(history[index]);
        } else {
          restore();
        }
        return true;
      }
      return false;
    }

    return {keydown, reset, restore, get browsing() { return index !== null; }};
  }

  globalThis.KovaComposerHistory = {attach, recalled, onFirstLine};
})();
