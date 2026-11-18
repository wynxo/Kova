/* One toast system (#198, first slice).
 *
 * Every transient message goes through here: info, success, warning and error.
 * Three are visible at most; the rest wait in order. Success and info dismiss
 * themselves, errors stay until someone dismisses them, and hovering or
 * focusing a toast pauses its timer so it cannot vanish mid-sentence.
 * An optional action button (Undo, Retry, Open) closes the toast only when the
 * action succeeds.
 *
 * app.js owns the wording and the severity; this file owns the stack, the
 * queue, the timers, the live region, the keyboard path and the desktop
 * notification, so it can be exercised on its own in tests/web_toast.mjs.
 * No network, no innerHTML: an OS notification repeats the toast's own title
 * and text, and only while the window is hidden.
 */
(() => {
  'use strict';

  const MAX_VISIBLE = 3;
  // Errors are decisions, so they wait for a person. Success is a receipt: 4s.
  const DURATIONS = {success: 4000, info: 5000, warning: 8000, error: 0};
  const ICONS = {success: 'check', info: 'info', warning: 'triangle-alert', error: 'triangle-alert'};
  const SEVERITIES = new Set(['info', 'success', 'warning', 'error']);

  const doc = typeof document !== 'undefined' ? document : null;
  const shown = [];
  const waiting = [];
  let region = null;

  const now = () => Date.now();

  function container() {
    if (region) return region;
    region = doc ? doc.getElementById('toasts') : null;
    if (!region && doc && doc.body) {
      region = doc.createElement('div');
      region.id = 'toasts';
      doc.body.append(region);
    }
    if (region) {
      region.setAttribute('role', 'region');
      region.setAttribute('aria-label', 'Notifications');
      region.setAttribute('aria-live', 'polite');
      region.setAttribute('aria-relevant', 'additions');
    }
    return region;
  }

  function severityOf(options) {
    const severity = String(options.severity || 'info').toLowerCase();
    return SEVERITIES.has(severity) ? severity : 'info';
  }

  function durationOf(severity, options) {
    if (options.sticky) return 0;
    if (Number.isFinite(options.duration)) return Math.max(0, options.duration);
    return DURATIONS[severity] || 0;
  }

  function clearTimer(entry) {
    if (entry.timer === null) return;
    clearTimeout(entry.timer);
    entry.timer = null;
  }

  function startTimer(entry) {
    clearTimer(entry);
    entry.remaining = entry.duration;
    entry.startedAt = now();
    entry.timer = setTimeout(() => dismiss(entry), entry.duration);
  }

  function pause(entry) {
    if (entry.timer === null) return;
    clearTimer(entry);
    entry.remaining = Math.max(0, entry.remaining - (now() - entry.startedAt));
  }

  function resume(entry) {
    if (entry.timer !== null || entry.dismissed || !entry.remaining) return;
    entry.startedAt = now();
    entry.timer = setTimeout(() => dismiss(entry), entry.remaining);
  }

  function node(tag, className, value) {
    const element = doc.createElement(tag);
    if (className) element.className = className;
    if (value !== undefined) element.textContent = value;
    return element;
  }

  function closeButton(entry) {
    const close = node('button', 'icon-button toast-close');
    close.type = 'button';
    close.setAttribute('aria-label', 'Dismiss notification');
    const cross = node('span', 'icon i-x');
    cross.setAttribute('aria-hidden', 'true');
    close.append(cross);
    close.addEventListener('click', () => dismiss(entry));
    return close;
  }

  function actionButton(entry) {
    const action = node('button', 'btn quiet small toast-action', entry.action.label);
    action.type = 'button';
    action.addEventListener('click', async () => {
      if (entry.busy) return;
      entry.busy = true;
      action.disabled = true;
      try {
        await entry.action.onClick?.();
        dismiss(entry);
      } catch (_) {
        // The action failed; keep the message (and its retry) on screen.
        entry.busy = false;
        action.disabled = false;
      }
    });
    return action;
  }

  function build(entry) {
    const urgent = entry.severity === 'error' || entry.severity === 'warning';
    const toast = node('article', `toast toast-${entry.severity}`);
    toast.setAttribute('data-severity', entry.severity);
    toast.setAttribute('role', urgent ? 'alert' : 'status');
    toast.setAttribute('aria-live', urgent ? 'assertive' : 'polite');
    toast.setAttribute('aria-atomic', 'true');

    const mark = node('span', `icon i-${entry.icon || ICONS[entry.severity]}`);
    mark.setAttribute('aria-hidden', 'true');

    const body = node('div', 'toast-body');
    if (entry.title) body.append(node('strong', 'toast-title', entry.title));
    body.append(node('span', 'toast-message', entry.message));

    toast.append(mark, body);
    if (entry.action && entry.action.label) toast.append(actionButton(entry));
    entry.closeButton = closeButton(entry);
    toast.append(entry.closeButton);

    toast.addEventListener('pointerenter', () => pause(entry));
    toast.addEventListener('pointerleave', () => resume(entry));
    toast.addEventListener('focusin', () => pause(entry));
    toast.addEventListener('focusout', () => resume(entry));
    return toast;
  }

  // One notification per toast, never a second wording. Only a hidden window
  // asks for it; the switch is the same "Notify me" setting as reminders.
  function notify(entry) {
    if (!entry.desktop) return;
    try {
      if (!doc || !doc.hidden) return;
      if (typeof localStorage === 'undefined' || localStorage.getItem('kova-notify') !== 'yes') return;
      if (typeof Notification === 'undefined' || Notification.permission !== 'granted') return;
      if (typeof navigator === 'undefined' || !navigator.serviceWorker?.getRegistration) return;
      navigator.serviceWorker.getRegistration('/app').then(registration => {
        registration?.active?.postMessage({
          type: 'toast', title: entry.title, text: entry.message,
          severity: entry.severity, icon: entry.icon || '',
        });
      }).catch(() => {});
    } catch (_) {
      // No service worker or no permission: the in-page toast still showed.
    }
  }

  function mount(entry) {
    const host = container();
    if (!host) return false;
    entry.node = build(entry);
    host.append(entry.node);
    shown.push(entry);
    if (entry.duration) startTimer(entry);
    notify(entry);
    return true;
  }

  function retire(entry, promote = true) {
    entry.dismissed = true;
    clearTimer(entry);
    const focused = Boolean(entry.node && doc && entry.node.contains?.(doc.activeElement));
    entry.node?.remove();
    entry.node = null;
    const index = shown.indexOf(entry);
    if (index >= 0) shown.splice(index, 1);
    if (focused) shown[0]?.closeButton?.focus?.();
    if (!promote) return;
    // A dismissed toast makes room for the oldest waiting one.
    while (shown.length < MAX_VISIBLE && waiting.length) {
      if (!mount(waiting.shift())) break;
    }
  }

  function dismiss(entry) {
    const target = entry || shown[shown.length - 1];
    if (!target || target.dismissed) return;
    retire(target);
  }

  function clear() {
    const entries = [...shown];
    waiting.length = 0;
    for (const entry of entries) retire(entry, false);
  }

  /** Show one message. Returns the record, which `dismiss` also accepts. */
  function show(message, options) {
    const opts = options || {};
    const severity = severityOf(opts);
    const icon = typeof opts.icon === 'string' && /^[a-z0-9-]+$/.test(opts.icon) ? opts.icon : '';
    const entry = {
      message: String(message === undefined || message === null ? '' : message),
      title: opts.title === undefined ? 'Kova' : String(opts.title || ''),
      severity,
      icon,
      duration: durationOf(severity, opts),
      action: opts.action && typeof opts.action === 'object' ? opts.action : null,
      desktop: opts.desktop !== false,
      dismissed: false,
      busy: false,
      timer: null,
      remaining: 0,
      startedAt: 0,
      node: null,
      closeButton: null,
    };
    if (shown.length >= MAX_VISIBLE || !container()) {
      waiting.push(entry);
      return entry;
    }
    mount(entry);
    return entry;
  }

  if (doc && typeof doc.addEventListener === 'function') {
    // Escape closes the toast that holds focus and does nothing else, so the
    // composer keeps its own Escape (stop the reply).
    doc.addEventListener('keydown', event => {
      if (event.key !== 'Escape') return;
      const owner = shown.find(entry => entry.node?.contains?.(event.target));
      if (!owner) return;
      event.preventDefault?.();
      dismiss(owner);
    });
  }

  globalThis.KovaToast = {
    show,
    dismiss,
    clear,
    visible: () => shown.length,
    queued: () => waiting.length,
    max: MAX_VISIBLE,
  };
})();
