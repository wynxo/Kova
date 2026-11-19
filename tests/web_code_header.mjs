// Logic test for the code-block header row: language label, Copy button,
// clipboard copy, 2 s "Copied" state, and the double-attach guard. Runs against
// a small tree-based DOM stub (the shared web_core stub has no tree querySelector).
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import vm from 'node:vm';

// ---- Tree DOM stub --------------------------------------------------------
class Element {
  constructor(tag = 'div') {
    this.tagName = tag;
    this.children = [];
    this.parentNode = null;
    this._text = '';
    this.className = '';
    this.attributes = {};
    this.listeners = {};
    this.value = '';
    this.open = false;
  }
  append(...nodes) {
    for (const n of nodes) { n.parentNode = this; this.children.push(n); }
    return this;
  }
  insertAdjacentHTML(position, html) {
    const text = html.replace(/<[^>]+>/g, '').trim();
    const t = new Element('#text');
    t.textContent = text;
    this.append(t);
  }
  replaceChildren(...nodes) {
    this.children = [];
    for (const n of nodes) { n.parentNode = this; this.children.push(n); }
  }
  insertBefore(child, ref) {
    child.parentNode = this;
    const i = ref ? this.children.indexOf(ref) : -1;
    if (i === -1) this.children.push(child);
    else this.children.splice(i, 0, child);
    return child;
  }
  remove() {
    const i = this.parentNode?.children.indexOf(this);
    if (i !== -1) this.parentNode.children.splice(i, 1);
    this.parentNode = null;
  }
  setAttribute(n, v) { this.attributes[n] = String(v); }
  getAttribute(n) { return n in this.attributes ? this.attributes[n] : null; }
  removeAttribute(n) { delete this.attributes[n]; }
  addEventListener(type, fn) { (this.listeners[type] ||= []).push(fn); }
  dispatchEvent(type) { (this.listeners[type] || []).forEach(fn => fn()); }
  click() { this.onclick?.(); }
  get textContent() {
    if (this.children.length) return this.children.map(c => c.textContent).join('');
    return this._text;
  }
  set textContent(v) { this._text = v; this.children = []; }
  set innerHTML(v) { this._html = v; }
  get innerHTML() { return this._html ?? ''; }
  get classList() {
    if (!this._classList) {
      this._classes = (this._classes ?? this.className.split(' ')).filter(Boolean);
      this._classList = {
        contains: c => this._classes.includes(c),
        add: c => { if (!this._classes.includes(c)) this._classes.push(c); },
        remove: c => { const i = this._classes.indexOf(c); if (i !== -1) this._classes.splice(i, 1); },
        toggle: c => { const i = this._classes.indexOf(c); if (i === -1) this._classes.push(c); else this._classes.splice(i, 1); },
      };
    }
    return this._classList;
  }
  set className(v) { this._className = v; }
  get className() { return this._className ?? ''; }
  querySelectorAll(sel) {
    const out = [];
    const walk = node => {
      if (matches(node, sel)) out.push(node);
      for (const c of node.children) walk(c);
    };
    walk(this);
    return out;
  }
  querySelector(sel) { return this.querySelectorAll(sel)[0] ?? null; }
}
const matches = (node, sel) => {
  const parts = sel.trim().split(/\s+/);
  const last = parts[parts.length - 1];
  if (last.startsWith('#')) return node.tagName === 'ELEMENT' && node.getAttribute('id') === last.slice(1);
  if (last.startsWith('.')) return node.className.split(' ').includes(last.slice(1));
  if (/^[a-z]/.test(last)) return node.tagName === last;
  return false;
};
const descendant = (sel) => {
  const parts = sel.trim().split(/\s+/);
  return node => {
    let cur = node;
    for (let i = parts.length - 1; i >= 0; i--) {
      if (!matches(cur, parts[i])) return false;
      cur = cur.parentNode;
      if (!cur) return false;
    }
    return true;
  };
};
Element.prototype.matches = function (sel) {
  const parts = sel.trim().split(/\s+/);
  let cur = this;
  for (let i = parts.length - 1; i >= 0; i--) {
    if (!matches(cur, parts[i])) return false;
    cur = cur.parentNode;
    if (!cur) return false;
  }
  return true;
};
Element.prototype.querySelectorAll = function (sel) {
  const out = [];
  const test = descendant(sel);
  const walk = node => { if (test(node)) out.push(node); for (const c of node.children) walk(c); };
  walk(this);
  return out;
};

// ---- Fake clock (so the 2 s "Copied" timer is deterministic) --------------
let timers = [];
let clock = 0, nextId = 1;
const realSetTimeout = globalThis.setTimeout;
globalThis.setTimeout = (fn, ms = 0) => { const id = nextId++; timers.push({id, fn, due: clock + ms}); return id; };
globalThis.clearTimeout = (id) => { timers = timers.filter(t => t.id !== id); };
const advance = (ms) => {
  clock += ms;
  timers.sort((a, b) => a.due - b.due);
  const due = timers.filter(t => t.due <= clock);
  timers = timers.filter(t => t.due > clock);
  for (const t of due) t.fn();
};

// ---- Clipboard stub -------------------------------------------------------
const clipboard = { writes: [], writeText(text) { clipboard.writes.push(text); } };
const window = {};
// In-memory settings store: the wrap choice is persisted per browser.
const storage = new Map();
const localStorage = {
  getItem: key => (storage.has(key) ? storage.get(key) : null),
  setItem: (key, value) => { storage.set(key, String(value)); },
  removeItem: key => { storage.delete(key); },
};

const document = {
  createElement: tag => new Element(tag),
  createTextNode: t => { const e = new Element('#text'); e.textContent = t; return e; },
  getElementById: id => { const e = new Element('div'); e.setAttribute('id', id); return e; },
  querySelector: sel => document.getElementById(sel.slice(1)),
  addEventListener() {},
};
const nodesById = new Map();
  document.getElementById = id => nodesById.get(id) ?? null;
const toasts = new Element('div');
toasts.setAttribute('id', 'toasts');
nodesById.set('toasts', toasts);
const context = vm.createContext({
  Element, document, navigator: { clipboard }, setTimeout, clearTimeout, Date,
  localStorage,
  hljs: { highlightElement(code) { highlighted.push(code.textContent); } },
  console, URLSearchParams, JSON,
  window,
});
const highlighted = [];
const source = readFileSync(new URL('../src/kova/web_assets/code-header.js', import.meta.url), 'utf8');
vm.runInContext(source, context);
const CodeHeader = window.CodeHeader;
const flushVm = async () => { await window.__flush(); };
window.__flush = async () => { await Promise.resolve(); await Promise.resolve(); };

// ---- Build a `pre code` subtree ------------------------------------------
const root = new Element('div');
const pre = new Element('pre');
const code = new Element('code');
code.className = 'language-js';
code.textContent = 'const answer = 42;';
pre.append(code);
root.append(pre);
root.append(toasts);

// ---- attach: header row appears before the code --------------------------
CodeHeader.attach(root);
const bar = root.querySelector('.code-bar');
assert.ok(bar, 'a .code-bar is inserted');
assert.equal(bar.parentNode, pre, 'the bar sits inside the pre');
const idx = pre.children.indexOf(bar);
assert.ok(idx >= 0 && pre.children[idx + 1] === code, 'the bar is inserted before the code element');
const lang = root.querySelector('.code-lang');
assert.equal(lang.textContent, 'js', 'the language label is the detected language');
const copy = root.querySelector('.copy-code');
assert.equal(copy.tagName, 'button');
assert.equal(copy.getAttribute('aria-label'), 'Copy code', 'the Copy button is accessible');
assert.equal(copy.textContent, 'Copy', 'starts in the Copy state');

// ---- click: copies raw text, flips to "Copied" ---------------------------
code.textContent = 'const answer = 42;';
copy.click();
await flushVm();
assert.deepEqual(clipboard.writes, ['const answer = 42;'], 'copies the code text, not the markup');
assert.equal(copy.textContent, 'Copied', 'shows Copied after a successful copy');

// ---- after 2 s: reverts to "Copy" ----------------------------------------
advance(2000);
assert.equal(copy.textContent, 'Copy', 'reverts to Copy after 2 s');

// ---- double attach is a no-op --------------------------------------------
const barsBefore = root.querySelectorAll('.code-bar').length;
CodeHeader.attach(root);
assert.equal(root.querySelectorAll('.code-bar').length, barsBefore, 're-attaching does not duplicate the row');

// ---- default language when no language- class ----------------------------
const plainRoot = new Element('div');
const plain = new Element('pre');
const plainCode = new Element('code');
plainCode.textContent = 'no language here';
plain.append(plainCode);
plainRoot.append(plain);
const plainToasts = new Element('div');
plainToasts.setAttribute('id', 'toasts');
nodesById.set('toasts', plainToasts);
CodeHeader.attach(plainRoot);
const plainLang = plainRoot.querySelector('.code-lang');
assert.equal(plainLang.textContent, 'text', 'unknown language falls back to "text"');

// ---- clipboard fallback: a toast instead of throwing ---------------------
clipboard.writes = [];
const brokenRoot = new Element('div');
const broken = new Element('pre');
const brokenCode = new Element('code');
brokenCode.className = 'language-text';
brokenCode.textContent = 'fallback copy';
broken.append(brokenCode);
brokenRoot.append(broken);
const brokenToasts = new Element('div');
brokenToasts.setAttribute('id', 'toasts');
nodesById.set('toasts', brokenToasts);
CodeHeader.attach(brokenRoot);
const brokenCopy = brokenRoot.querySelector('.copy-code');
clipboard.writeText = () => { throw new Error('no clipboard'); };
brokenCopy.click();
await flushVm();
assert.equal(clipboard.writes.length, 0, 'writeText was attempted');
const toast = brokenToasts.querySelector('.toast');
assert.ok(toast, 'a fallback toast is shown when the clipboard fails');
assert.ok(toast.textContent.includes('Clipboard unavailable'), 'the toast guides the user to copy manually');

// ---- wrap toggle: one persisted preference, applied to later blocks ------
const wrapRoot = new Element('div');
const wrapPre = new Element('pre');
const wrapCode = new Element('code');
wrapCode.className = 'language-js';
wrapCode.textContent = 'const wide = 1;';
wrapPre.append(wrapCode);
wrapRoot.append(wrapPre);
CodeHeader.attach(wrapRoot);
const wrap = wrapRoot.querySelector('.wrap-code');
assert.equal(wrap.textContent, 'Wrap', 'the toggle names the setting');
assert.equal(wrap.getAttribute('aria-pressed'), 'false', 'wrapping starts off');
assert.equal(wrapPre.classList.contains('wrap-lines'), false, 'the block does not wrap yet');
wrap.click();
assert.equal(wrap.getAttribute('aria-pressed'), 'true', 'the toggle holds its pressed state');
assert.ok(wrapPre.classList.contains('wrap-lines'), 'the block wraps after the toggle');
assert.equal(storage.get('kova-code-wrap'), '1', 'the choice is saved for this browser');

const laterRoot = new Element('div');
const laterPre = new Element('pre');
const laterCode = new Element('code');
laterCode.className = 'language-js';
laterCode.textContent = 'console.log(1);';
laterPre.append(laterCode);
laterRoot.append(laterPre);
CodeHeader.attach(laterRoot);
assert.ok(laterPre.classList.contains('wrap-lines'), 'a later block inherits the saved choice');
assert.equal(laterRoot.querySelector('.wrap-code').getAttribute('aria-pressed'), 'true');

// ---- Lines: a gutter beside the code, one persisted preference ------------
const linesRoot = new Element('div');
const linesPre = new Element('pre');
const linesCode = new Element('code');
linesCode.className = 'language-js';
linesCode.textContent = 'one\ntwo\nthree\n';
linesPre.append(linesCode);
linesRoot.append(linesPre);
CodeHeader.attach(linesRoot);

const linesButton = linesRoot.querySelector('.lines-code');
assert.equal(linesButton.textContent, 'Lines', 'the toggle names the setting');
assert.equal(linesButton.getAttribute('aria-pressed'), 'false', 'line numbers start off');
assert.equal(linesPre.querySelector('.code-gutter'), null, 'no gutter until it is asked for');
linesButton.click();

const gutter = linesPre.querySelector('.code-gutter');
assert.ok(gutter, 'the gutter appears beside the code');
assert.equal(gutter.textContent, '1\n2\n3', 'one number per source line, no trailing blank');
assert.equal(gutter.getAttribute('aria-hidden'), 'true', 'numbers are decoration, not content');
assert.ok(linesPre.classList.contains('has-lines'), 'the block lays out a gutter column');
assert.equal(linesButton.getAttribute('aria-pressed'), 'true');
assert.equal(storage.get('kova-code-lines'), '1', 'the choice is saved for this browser');

const laterLinesRoot = new Element('div');
const laterLinesPre = new Element('pre');
const laterLinesCode = new Element('code');
laterLinesCode.textContent = 'x\ny';
laterLinesPre.append(laterLinesCode);
laterLinesRoot.append(laterLinesPre);
CodeHeader.attach(laterLinesRoot);
assert.equal(laterLinesPre.querySelector('.code-gutter').textContent, '1\n2',
  'a later block inherits the saved choice');

linesButton.click();
assert.equal(linesPre.querySelector('.code-gutter'), null, 'turning it off removes the gutter');
assert.equal(storage.get('kova-code-lines'), '0');

// ---- a fence still streaming stays plain and cannot be copied -------------
highlighted.length = 0;
const streamRoot = new Element('div');
const donePre = new Element('pre');
const doneCode = new Element('code');
doneCode.className = 'language-js';
doneCode.textContent = 'const done = 1;';
donePre.append(doneCode);
const openPre = new Element('pre');
const openCode = new Element('code');
openCode.className = 'language-js';
openCode.textContent = 'const open = 2;';
openPre.append(openCode);
streamRoot.append(donePre, openPre);
CodeHeader.attach(streamRoot, false, true);
assert.deepEqual(highlighted, ['const done = 1;'],
  'the closed fence is highlighted and the open one stays plain');
assert.equal(donePre.getAttribute('data-streaming'), null, 'a finished block is not marked');
assert.equal(openPre.getAttribute('data-streaming'), '', 'the open block is marked as streaming');
const streamCopies = streamRoot.querySelectorAll('.copy-code');
assert.ok(!streamCopies[0].disabled, 'a finished block can be copied');
assert.equal(streamCopies[1].disabled, true, 'a block still arriving cannot be copied');
assert.equal(streamCopies[1].textContent, 'Copy', 'the waiting button keeps its label');

highlighted.length = 0;
const finalRoot = new Element('div');
const finalPre = new Element('pre');
const finalCode = new Element('code');
finalCode.className = 'language-js';
finalCode.textContent = 'const done = 1;';
finalPre.append(finalCode);
finalRoot.append(finalPre);
CodeHeader.attach(finalRoot, true);
assert.deepEqual(highlighted, ['const done = 1;'], 'the finished reply highlights its block');
assert.ok(!finalRoot.querySelector('.copy-code').disabled, 'copy is enabled once complete');
assert.equal(finalPre.getAttribute('data-streaming'), null, 'nothing is left marked as streaming');

console.log('Code header row passed');
