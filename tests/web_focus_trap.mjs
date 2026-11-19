// The modal-dialog focus trap (#201): Tab and Shift+Tab cycle inside the
// topmost open dialog instead of leaving it. No browser needed.
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import vm from 'node:vm';

const source = readFileSync(new URL('../src/kova/web_assets/focus-trap.js', import.meta.url), 'utf8');

const openDialogs = [];
class El {
  constructor(tag) {
    this.tag = tag;
    this.children = [];
    this.disabled = false;
    this.hidden = false;
    this.focused = false;
  }
  append(...kids) { this.children.push(...kids); }
  getAttribute(name) { return name === 'hidden' && this.hidden ? '' : null; }
  contains(node) { return this === node || this.children.some(child => child.contains(node)); }
  focus() { document.activeElement = this; }
  querySelectorAll() {
    const out = [];
    const walk = node => {
      if (node !== this && !node.disabled) out.push(node);
      node.children.forEach(walk);
    };
    walk(this);
    return out;
  }
}

const document = {
  activeElement: null,
  listeners: {},
  addEventListener(type, fn) { this.listeners[type] = fn; },
  querySelectorAll(selector) {
    if (selector === 'dialog[open]') return openDialogs.slice();
    return [];
  },
};
vm.runInNewContext(source, {document, Array});

function tab(opts = {}) {
  let prevented = false;
  document.listeners.keydown?.({key: 'Tab', shiftKey: false, preventDefault() { prevented = true; }, ...opts});
  return prevented;
}

const dialog = new El('dialog');
const first = new El('button');
const middle = new El('button');
const last = new El('button');
dialog.append(first, middle, last);
openDialogs.push(dialog);

// Forward at the last element wraps to the first.
document.activeElement = last;
assert.equal(tab(), true, 'Tab at the end is trapped');
assert.equal(document.activeElement, first);

// Shift+Tab at the first element wraps to the last.
document.activeElement = first;
assert.equal(tab({shiftKey: true}), true, 'Shift+Tab at the start is trapped');
assert.equal(document.activeElement, last);

// A Tab from outside the dialog enters it at the first element.
const outside = new El('button');
document.activeElement = outside;
assert.equal(tab(), true);
assert.equal(document.activeElement, first);

// A Tab in the middle moves freely (the browser handles the default).
document.activeElement = middle;
assert.equal(tab(), false, 'a mid-dialog Tab is left to the browser');
assert.equal(document.activeElement, middle);

// No open dialog: Tab is never intercepted.
openDialogs.length = 0;
document.activeElement = outside;
assert.equal(tab(), false);
assert.equal(document.activeElement, outside);

// A dialog with nothing focusable still stops focus from leaving.
openDialogs.push(new El('dialog'));
document.activeElement = outside;
assert.equal(tab(), true, 'an empty dialog still traps focus');
openDialogs.length = 0;

console.log('Focus trap: modal dialogs keep Tab inside and still allow free movement passed');
