// Phone polish (#202).
//
// Self-contained classic script; mobile.css carries the look. It does the three
// things app.js should not grow:
//   1. keeps <body> at the visual viewport height, so the composer stays above
//      the on-screen keyboard,
//   2. turns the popovers and menus into bottom sheets on phones, with a drag
//      handle and swipe-down to close (Escape and the backdrop still work),
//   3. opens the sidebar with a left-edge swipe and closes it with a swipe
//      left, ignoring fingers that start on a horizontally scrolling element.
(function () {
  'use strict';
  if (typeof document === 'undefined' || !document.body) return;

  const root = document.documentElement;
  const body = document.body;
  const narrow = window.matchMedia ? window.matchMedia('(max-width:720px)') : {matches: false};
  const still = () => root.dataset.motion === 'off'
    || Boolean(window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches);

  // ---------- The viewport and the on-screen keyboard ----------
  // iOS leaves the layout viewport at full height when the keyboard opens and
  // shrinks only the visual one. Reading it once per frame and writing two
  // custom properties is enough: no polling, no layout reads during scroll.
  let viewportFrame = 0;
  function measureViewport() {
    viewportFrame = 0;
    const viewport = window.visualViewport;
    if (!viewport) return;
    // Pinch zoom is the reader's business, not the keyboard's: leave the
    // layout alone and keep the composer where it was.
    if (Math.abs((viewport.scale || 1) - 1) > 0.01) return;
    const height = Math.round(viewport.height);
    if (!height) return;
    root.style.setProperty('--app-viewport-height', `${height}px`);
    const inset = Math.max(0, Math.round(window.innerHeight - viewport.height - viewport.offsetTop));
    root.style.setProperty('--app-keyboard-inset', `${inset}px`);
  }
  function scheduleViewport() {
    if (!viewportFrame) viewportFrame = window.requestAnimationFrame(measureViewport);
  }
  if (window.visualViewport) {
    window.visualViewport.addEventListener('resize', scheduleViewport);
    window.visualViewport.addEventListener('scroll', scheduleViewport);
    scheduleViewport();
  }
  window.addEventListener('resize', scheduleViewport);
  window.addEventListener('orientationchange', scheduleViewport);

  // ---------- Bottom sheets ----------
  // The menu ids and the generic popover app.js builds for a listbox.
  const SHEETS = '.compose-menu, .chat-options, .chat-menu-list, .activity-panel, .popover';
  const sheets = () => Array.from(document.querySelectorAll(SHEETS));

  function visible(node) {
    if (!node || node.hidden) return false;
    const details = node.closest('details');
    if (details && !details.open) return false;
    return node.getClientRects().length > 0;
  }

  // One dimmed backdrop for every sheet, created once so index.html stays the
  // shell it already was. It sits under the sheet (mobile.css owns z-index).
  const scrim = document.createElement('div');
  scrim.className = 'sheet-scrim';
  scrim.setAttribute('aria-hidden', 'true');
  scrim.hidden = true;
  body.append(scrim);

  let sheetFrame = 0;
  function paintSheets() {
    sheetFrame = 0;
    if (!narrow.matches) {
      scrim.hidden = true;
      body.classList.remove('sheet-open');
      return;
    }
    const open = sheets().filter(visible);
    for (const sheet of open) addHandle(sheet);
    scrim.hidden = open.length === 0;
    body.classList.toggle('sheet-open', open.length > 0);
  }
  function scheduleSheets() {
    if (!sheetFrame) sheetFrame = window.requestAnimationFrame(paintSheets);
  }

  function addHandle(sheet) {
    if (sheet.querySelector(':scope > .sheet-handle')) return;
    const handle = document.createElement('div');
    handle.className = 'sheet-handle';
    handle.setAttribute('aria-hidden', 'true');
    sheet.prepend(handle);
  }

  // Close the sheet the way its owner already closes it, so focus returns and
  // aria-expanded stays true to the state. Every path is idempotent: a sheet
  // that is already hidden is left alone.
  function closeSheet(sheet) {
    if (!visible(sheet)) return false;
    const details = sheet.closest('details');
    if (details) {
      details.open = false;
      details.querySelector('summary')?.focus();
      return true;
    }
    if (sheet.classList.contains('popover')) {
      // Dispatch on the popover itself: its own Escape handler lives there, and
      // a non-bubbling event is how this stays out of the document listener.
      sheet.dispatchEvent(new KeyboardEvent('keydown', {key: 'Escape'}));
      return true;
    }
    const owner = {'compose-menu': 'compose-more', 'chat-options-panel': 'chat-options',
      'activity-panel': 'activity'}[sheet.id];
    const button = owner && document.getElementById(owner);
    if (button && button.getAttribute('aria-expanded') === 'true') {
      button.click();
      return true;
    }
    sheet.hidden = true;
    return true;
  }

  function topSheet() {
    const open = sheets().filter(visible);
    return open.length ? open[open.length - 1] : null;
  }

  // A tap on the scrim closes only the topmost sheet. This listener is added
  // before app.js's, so it can stop the app's own outside-tap handler from
  // closing a panel underneath a popover at the same time.
  document.addEventListener('pointerdown', event => {
    if (event.target !== scrim) return;
    event.preventDefault();
    event.stopImmediatePropagation();
    closeSheet(topSheet());
  }, true);

  // A keydown the sheet did not consume itself closes it and stops the app's
  // global Escape (stop the reply) from also firing.
  document.addEventListener('keydown', event => {
    if (event.key !== 'Escape' || event.isComposing || !narrow.matches) return;
    if (!closeSheet(topSheet())) return;
    event.preventDefault();
    event.stopImmediatePropagation();
  });

  const observer = new MutationObserver(records => {
    for (const record of records) {
      if (record.type === 'attributes') {
        if (record.target.matches?.(`${SHEETS}, details`)) return scheduleSheets();
        continue;
      }
      for (const node of [...record.addedNodes, ...record.removedNodes]) {
        if (node.nodeType === 1 && (node.matches?.(SHEETS) || node.querySelector?.(SHEETS))) {
          return scheduleSheets();
        }
      }
    }
  });
  observer.observe(body, {subtree: true, attributes: true, attributeFilter: ['hidden', 'open'], childList: true});
  if (narrow.addEventListener) narrow.addEventListener('change', scheduleSheets);
  scheduleSheets();

  // Swipe down on the handle to close. The handle is the only place that takes
  // the gesture, so scrolling inside the sheet is never fought over.
  let drag = null;
  document.addEventListener('pointerdown', event => {
    if (!narrow.matches || (event.pointerType === 'mouse' && event.button !== 0)) return;
    const sheet = event.target.closest?.(SHEETS);
    if (!visible(sheet)) return;
    if (!event.target.closest('.sheet-handle')) return;
    if (event.target.closest('input, textarea, select, [contenteditable="true"]')) return;
    drag = {sheet, id: event.pointerId, y: event.clientY, x: event.clientX, dy: 0, moved: false};
    try { sheet.setPointerCapture(event.pointerId); } catch (_) { /* a detached sheet cannot capture; the drag still works */ }
  }, {passive: true});

  document.addEventListener('pointermove', event => {
    if (!drag || event.pointerId !== drag.id) return;
    const dy = event.clientY - drag.y, dx = event.clientX - drag.x;
    if (!drag.moved) {
      if (Math.abs(dy) < 6 && Math.abs(dx) < 6) return;
      if (Math.abs(dx) > Math.abs(dy)) { drag = null; return; }  // sideways: not ours
      drag.moved = true;
      drag.sheet.classList.add('sheet-dragging');
    }
    if (dy < 0) return;
    drag.dy = dy;
    drag.sheet.style.transform = `translateY(${dy}px)`;
    event.preventDefault();
  }, {passive: false});

  function endDrag(event) {
    if (!drag || event.pointerId !== drag.id) return;
    const {sheet, dy, moved} = drag;
    drag = null;
    if (!moved) return;
    sheet.classList.remove('sheet-dragging');
    const height = sheet.getBoundingClientRect().height;
    const close = dy > Math.min(height * 0.4, 160);
    if (!close || still()) {
      sheet.style.transform = '';
      if (close) closeSheet(sheet);
      return;
    }
    sheet.classList.add('sheet-closing');
    sheet.style.transform = 'translateY(100%)';
    let done = false;
    const finish = () => {
      if (done) return;
      done = true;
      sheet.classList.remove('sheet-closing');
      sheet.style.transform = '';
      closeSheet(sheet);
    };
    sheet.addEventListener('transitionend', finish, {once: true});
    window.setTimeout(finish, 400);
  }
  document.addEventListener('pointerup', endDrag, {passive: true});
  document.addEventListener('pointercancel', endDrag, {passive: true});

  // ---------- Edge swipe for the sidebar ----------
  // A phone has no room for a permanent column, so the drawer also opens from
  // the left edge and closes with a leftward swipe. The gesture is a hint, not
  // the only path: the top-bar button and Escape still work.
  const EDGE = 24;       // how close to the left edge a swipe has to start
  const SWIPE = 56;      // how far it has to travel to count
  let swipe = null;

  // A finger that lands on a table, code block, diff or tool output may be
  // scrolling that element sideways; the drawer must not steal the gesture.
  function inHorizontalScroller(target) {
    let node = target instanceof Element ? target : null;
    while (node && node !== body) {
      const style = getComputedStyle(node);
      if (/(auto|scroll)/.test(style.overflowX) && node.scrollWidth > node.clientWidth + 4) return true;
      node = node.parentElement;
    }
    return false;
  }

  function setSidebar(open) {
    if (body.classList.contains('sidebar-open') === open) return;
    document.getElementById('menu')?.click();
  }

  document.addEventListener('touchstart', event => {
    if (!narrow.matches || event.touches.length !== 1 || body.classList.contains('sheet-open')) return;
    if (body.classList.contains('panel-open') || body.classList.contains('settings-open')) return;
    if (document.querySelector('dialog[open]')) return;
    if (event.target.closest?.('input, textarea, select, [contenteditable="true"]')) return;
    const touch = event.touches[0];
    const open = body.classList.contains('sidebar-open');
    // Closed: only from the left edge. Open: anywhere on the drawer itself.
    if (!open && (touch.clientX > EDGE || inHorizontalScroller(event.target))) return;
    if (open && !event.target.closest?.('#sidebar')) return;
    swipe = {x: touch.clientX, y: touch.clientY, open};
  }, {passive: true});

  document.addEventListener('touchmove', event => {
    if (!swipe || event.touches.length !== 1) return;
    const touch = event.touches[0];
    if (Math.abs(touch.clientY - swipe.y) > Math.abs(touch.clientX - swipe.x)) swipe = null;
  }, {passive: true});

  document.addEventListener('touchend', event => {
    if (!swipe) return;
    const touch = event.changedTouches[0];
    const started = swipe;
    swipe = null;
    const dx = touch.clientX - started.x, dy = touch.clientY - started.y;
    if (Math.abs(dx) < SWIPE || Math.abs(dy) > Math.abs(dx)) return;
    if (started.open && dx < 0) setSidebar(false);
    else if (!started.open && dx > 0) setSidebar(true);
  }, {passive: true});
  document.addEventListener('touchcancel', () => { swipe = null; }, {passive: true});
})();
