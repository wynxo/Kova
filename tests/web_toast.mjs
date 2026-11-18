// One toast system (#198): severities, the three-deep stack, auto-dismiss with
// a pause on hover/focus, keyboard dismissal and action buttons. Runs the real
// toast.js with a small DOM and a controllable clock; no browser or packages.
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import vm from 'node:vm';

let clock = 0;
let nextTimer = 1;
const timers = new Map();

function setTimeoutFake(fn, delay) {
  const id = nextTimer++;
  timers.set(id, {fn, at: clock + Math.max(0, Number(delay) || 0)});
  return id;
}
function clearTimeoutFake(id) { timers.delete(id); }

class Element {
  constructor(tag = 'div') {
    this.tagName = tag; this.id = ''; this.className = ''; this.children = [];
    this.parentNode = null; this.attrs = {}; this.listeners = {}; this._text = '';
    this.disabled = false; this.type = '';
  }
  set textContent(value) { this._text = String(value); this.children = []; }
  get textContent() { return this._text + this.children.map(child => child.textContent).join(''); }
  append(...nodes) { for (const node of nodes) { node.parentNode = this; this.children.push(node); } }
  remove() {
    if (!this.parentNode) return;
    this.parentNode.children.splice(this.parentNode.children.indexOf(this), 1);
    this.parentNode = null;
  }
  setAttribute(name, value) { this.attrs[name] = String(value); if (name === 'id') this.id = String(value); }
  getAttribute(name) { return this.attrs[name]; }
  addEventListener(type, handler) { (this.listeners[type] ||= []).push(handler); }
  dispatch(type, event = {}) { for (const handler of this.listeners[type] || []) handler({preventDefault() {}, ...event}); }
  contains(node) {
    for (let current = node; current; current = current.parentNode) if (current === this) return true;
    return false;
  }
  focus() { document.activeElement = this; }
  click() { this.dispatch('click'); }
}

const body = new Element('body');
const region = new Element('div'); region.id = 'toasts'; body.append(region);
const document = {
  body, activeElement: null, listeners: {}, hidden: false,
  getElementById: id => (id === 'toasts' ? region : null),
  createElement: tag => new Element(tag),
  addEventListener(type, handler) { (this.listeners[type] ||= []).push(handler); },
  dispatch(type, event = {}) { for (const handler of this.listeners[type] || []) handler({preventDefault() {}, ...event}); },
};

const notifications = [];
const navigator = {serviceWorker: {getRegistration: async () => ({active: {postMessage: data => notifications.push(data)}})}};
const settings = new Map([['kova-notify', 'yes']]);
const sandbox = {
  document, window: {}, console, navigator,
  Notification: {permission: 'granted'},
  localStorage: {getItem: key => (settings.get(key) ?? null), setItem: (key, value) => settings.set(key, value)},
  Date: {now: () => clock},
  setTimeout: setTimeoutFake, clearTimeout: clearTimeoutFake,
};
vm.runInNewContext(
  readFileSync(new URL('../src/kova/web_assets/toast.js', import.meta.url), 'utf8'), sandbox);
const KovaToast = sandbox.KovaToast;
assert.ok(KovaToast, 'toast.js exposes window.KovaToast');

/** Run every timer due before `clock + ms`, in time order. */
function advance(ms) {
  const target = clock + ms;
  for (let guard = 0; guard < 1000; guard += 1) {
    const due = [...timers.entries()]
      .filter(([, timer]) => timer.at <= target)
      .sort((a, b) => a[1].at - b[1].at)[0];
    if (!due) break;
    timers.delete(due[0]);
    clock = due[1].at;
    due[1].fn();
  }
  clock = target;
}
const tick = async () => { for (let i = 0; i < 4; i += 1) await new Promise(resolve => setImmediate(resolve)); };
const cards = () => region.children;
const byClass = (node, name) => {
  const found = [];
  const walk = current => {
    for (const child of current.children) {
      if (` ${child.className} `.includes(` ${name} `)) found.push(child);
      walk(child);
    }
  };
  walk(node);
  return found;
};

// Info is polite, titled, and reads as one string to a screen reader.
const info = KovaToast.show('Saved your draft.', {title: 'Kova'});
// The region itself is the polite live region the markup promises.
assert.equal(region.getAttribute('role'), 'region');
assert.equal(region.getAttribute('aria-live'), 'polite');
assert.equal(region.getAttribute('aria-label'), 'Notifications');
assert.equal(KovaToast.visible(), 1);
assert.equal(cards().length, 1);
assert.match(info.node.className, /toast-info/);
assert.equal(info.node.getAttribute('role'), 'status');
assert.equal(info.node.getAttribute('aria-live'), 'polite');
assert.equal(info.node.getAttribute('data-severity'), 'info');
assert.match(info.node.textContent, /Kova/);
assert.match(info.node.textContent, /Saved your draft/);

// Success leaves after four seconds; info gets five.
KovaToast.show('Model ready.', {title: 'Model', severity: 'success'});
advance(3999);
assert.equal(KovaToast.visible(), 2, 'success waits its full 4s');
advance(1);
assert.equal(KovaToast.visible(), 1, 'success auto-dismisses at 4s');

// Errors stay until dismissed, and are announced assertively.
const error = KovaToast.show('The model server stopped responding.', {severity: 'error'});
assert.equal(error.node.getAttribute('role'), 'alert');
assert.equal(error.node.getAttribute('aria-live'), 'assertive');
advance(60_000);
assert.ok(cards().some(card => /model server stopped/.test(card.textContent)), 'errors do not expire');

// Warnings are urgent too, but they still clear on their own.
KovaToast.show('This month is nearly spent.', {title: 'Budget', severity: 'warning'});
assert.equal(cards().at(-1).getAttribute('role'), 'alert');
advance(8000);
assert.ok(!cards().some(card => /nearly spent/.test(card.textContent)), 'warnings clear after 8s');

// At most three are visible; the rest wait their turn, oldest first.
KovaToast.clear();
assert.equal(KovaToast.visible(), 0);
for (const message of ['one', 'two', 'three', 'four', 'five']) KovaToast.show(message, {sticky: true});
assert.equal(KovaToast.visible(), 3);
assert.equal(KovaToast.queued(), 2);
assert.ok(!region.textContent.includes('four'), 'the fourth waits out of sight');
KovaToast.dismiss();
assert.equal(KovaToast.visible(), 3);
assert.equal(KovaToast.queued(), 1);
assert.ok(region.textContent.includes('four'), 'a dismissal promotes the queue');
assert.ok(!region.textContent.includes('five'));

// Hover pauses: two seconds in, a long hover does not consume the remaining
// three, and leaving resumes exactly where it stopped.
KovaToast.clear();
const hovered = KovaToast.show('Read me first.');
advance(2000);
hovered.node.dispatch('pointerenter');
advance(30_000);
assert.equal(KovaToast.visible(), 1, 'hover keeps the toast on screen');
hovered.node.dispatch('pointerleave');
advance(2999);
assert.equal(KovaToast.visible(), 1);
advance(1);
assert.equal(KovaToast.visible(), 0);

// Keyboard focus pauses for the same reason, so tabbing to Undo does not race it.
const focused = KovaToast.show('Focused message.', {severity: 'info'});
advance(3000);
focused.node.dispatch('focusin');
advance(20_000);
assert.equal(KovaToast.visible(), 1);
focused.node.dispatch('focusout');
advance(2000);
assert.equal(KovaToast.visible(), 0);

// Escape closes the toast that holds focus, and only that one.
const escape = KovaToast.show('Dismiss with Escape.');
escape.closeButton.focus();
document.dispatch('keydown', {key: 'Escape', target: escape.closeButton});
assert.equal(KovaToast.visible(), 0);
const keep = KovaToast.show('Keep this.');
document.dispatch('keydown', {key: 'Escape', target: body});
assert.equal(KovaToast.visible(), 1, 'Escape outside the stack leaves it alone');

// The close button is a real keyboard-operable button.
keep.closeButton.click();
assert.equal(KovaToast.visible(), 0);

// An action runs, then closes the toast.
let undone = 0;
const withAction = KovaToast.show('Vision on.', {severity: 'success', title: 'Model',
  action: {label: 'Undo', onClick: async () => { undone += 1; }}});
const [undo] = byClass(withAction.node, 'toast-action');
assert.equal(undo.textContent, 'Undo');
undo.click();
await tick();
assert.equal(undone, 1);
assert.equal(KovaToast.visible(), 0, 'a successful action closes the toast');

// A failed action keeps the message, and the retry, on screen.
const failing = KovaToast.show('Could not save.', {severity: 'error',
  action: {label: 'Retry', onClick: async () => { throw new Error('offline'); }}});
const [retry] = byClass(failing.node, 'toast-action');
retry.click();
await tick();
assert.equal(KovaToast.visible(), 1, 'a failed action keeps the toast');
assert.equal(retry.disabled, false);

// Sticky messages (reminders) never expire, and keep their bell.
KovaToast.clear();
const reminder = KovaToast.show('Stretch your legs.', {title: 'Reminder', sticky: true, icon: 'bell'});
advance(120_000);
assert.equal(KovaToast.visible(), 1);
assert.ok(byClass(reminder.node, 'i-bell').length, 'a reminder keeps its own icon');

// A hidden window repeats the toast's own title and text as one OS
// notification; a visible window, the off switch and an opt-out stay quiet.
KovaToast.clear();
document.hidden = true;
const before = notifications.length;
const os = KovaToast.show('Stretch your legs.', {title: 'Reminder', sticky: true, icon: 'bell'});
await tick();
assert.equal(notifications.length, before + 1, 'the toast forwards one notification');
assert.equal(notifications.at(-1).type, 'toast');
assert.equal(notifications.at(-1).title, 'Reminder');
assert.equal(notifications.at(-1).text, 'Stretch your legs.');
document.hidden = false;
KovaToast.show('Only in the page.');
await tick();
assert.equal(notifications.length, before + 1, 'a visible window sends nothing');
document.hidden = true;
KovaToast.show('Chosen quiet.', {desktop: false});
await tick();
assert.equal(notifications.length, before + 1, 'a toast can opt out of the desktop copy');
settings.set('kova-notify', 'off');
KovaToast.show('Notifications are off.');
await tick();
assert.equal(notifications.length, before + 1, 'the Notify me switch still owns the decision');
assert.ok(os.node, 'the in-page toast is unaffected');

console.log('Toast stack, timers, pause, keyboard, actions and desktop wording passed');
