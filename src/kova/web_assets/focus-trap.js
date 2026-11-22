/* Keep keyboard focus inside an open modal dialog (#201).

   A native <dialog> with showModal() already moves focus in on open and returns
   it on close; what it does not do is stop Tab from leaving the dialog into the
   page or the browser chrome behind it. This one document-level listener closes
   that gap for every modal dialog at once, so no dialog has to remember to wire
   its own trap: Tab and Shift+Tab cycle through the topmost open dialog.
   A responsive drawer can opt in while it is acting as a modal. */
(() => {
  'use strict';

  const FOCUSABLE = 'a[href], button:not([disabled]), textarea:not([disabled]), '
    + 'input:not([disabled]), select:not([disabled]), summary, [tabindex]:not([tabindex="-1"])';

  function focusable(node) {
    return node.tabIndex >= 0 && !node.matches(':disabled') &&
      !node.closest('[inert]') && node.getClientRects().length &&
      getComputedStyle(node).visibility === 'visible';
  }

  document.addEventListener('keydown', event => {
    if (event.key !== 'Tab' || event.defaultPrevented) return;
    const active = document.activeElement;
    const dialogs = document.querySelectorAll('dialog[open]');
    const dialog = dialogs[dialogs.length - 1] || active?.closest('[data-focus-trap]');
    if (!dialog) return;
    const tabbable = Array.from(dialog.querySelectorAll(FOCUSABLE)).filter(focusable);
    if (!tabbable.length) {
      // Nothing to reach inside the dialog; keep the browser from leaving it.
      event.preventDefault();
      return;
    }
    const first = tabbable[0];
    const last = tabbable[tabbable.length - 1];
    const inside = Boolean(active && dialog.contains && dialog.contains(active));
    if (event.shiftKey) {
      if (!inside || active === first) { event.preventDefault(); last.focus(); }
    } else if (!inside || active === last) {
      event.preventDefault(); first.focus();
    }
  });
})();
