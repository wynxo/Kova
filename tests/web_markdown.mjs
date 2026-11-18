// Logic test for markdown-view.js (#189): footnotes and their per-message ids,
// table wrappers, read-only task lists and the streaming-code hand-off. Runs
// against a small tree DOM stub; marked is the vendored parser, DOMPurify is
// stubbed (it needs a browser DOM) and its options are recorded.
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import vm from 'node:vm';

// ---- Tree DOM stub --------------------------------------------------------
const VOID = new Set(['br', 'hr', 'img', 'input', 'meta', 'link']);
const ENTITIES = {'&amp;': '&', '&lt;': '<', '&gt;': '>', '&quot;': '"', '&#39;': "'"};

class Element {
  constructor(tag = 'div') {
    this.tagName = tag;
    this.children = [];
    this.parentNode = null;
    this._text = '';
    this.className = '';
    this.attributes = {};
    this.dataset = {};
    this.listeners = {};
    this.disabled = false;
    this.checked = false;
    this.title = '';
    this.tabIndex = -1;
  }
  append(...nodes) {
    for (const n of nodes) { n.parentNode = this; this.children.push(n); }
    return this;
  }
  replaceChildren(...nodes) { this.children = []; this._text = ''; this.append(...nodes); }
  insertBefore(child, ref) {
    child.parentNode = this;
    const i = ref ? this.children.indexOf(ref) : -1;
    if (i === -1) this.children.push(child);
    else this.children.splice(i, 0, child);
    return child;
  }
  replaceWith(node) {
    const parent = this.parentNode;
    if (!parent) return;
    const i = parent.children.indexOf(this);
    parent.children[i] = node;
    node.parentNode = parent;
    this.parentNode = null;
  }
  remove() {
    const i = this.parentNode?.children.indexOf(this);
    if (i !== undefined && i !== -1) this.parentNode.children.splice(i, 1);
    this.parentNode = null;
  }
  setAttribute(name, value) { this.attributes[name] = String(value); }
  getAttribute(name) { return name in this.attributes ? this.attributes[name] : null; }
  removeAttribute(name) { delete this.attributes[name]; }
  addEventListener(type, fn) { (this.listeners[type] ||= []).push(fn); }
  click() { this.onclick?.(); }
  get textContent() {
    if (this.children.length) return this.children.map(c => c.textContent).join('');
    return this._text;
  }
  set textContent(value) { this._text = value; this.children = []; }
  get innerHTML() { return this._html ?? ''; }
  set innerHTML(html) {
    this._html = html;
    this.children = [];
    this._text = '';
    parseHtml(this, String(html));
  }
  get classList() {
    const names = () => this.className.split(' ').filter(Boolean);
    return {
      contains: c => names().includes(c),
      // The DOM's classList.add takes several names; the double must too.
      add: (...classes) => {
        for (const c of classes) if (!names().includes(c)) this.className = [...names(), c].join(' ');
      },
      remove: c => { this.className = names().filter(n => n !== c).join(' '); },
      toggle: c => { if (names().includes(c)) this.classList.remove(c); else this.classList.add(c); },
    };
  }
  querySelectorAll(selector) { return selectAll(this, selector); }
  querySelector(selector) { return selectAll(this, selector)[0] ?? null; }
}

const decode = text => text.replace(/&(?:amp|lt|gt|quot|#39);/g, m => ENTITIES[m] ?? m);

function parseHtml(root, html) {
  const stack = [root];
  const pattern = /<!--[\s\S]*?-->|<\/?([a-zA-Z][\w-]*)((?:\s+[\w:-]+(?:\s*=\s*(?:"[^"]*"|'[^']*'|[^\s"'>]+))?)*)\s*\/?>|([^<]+)/g;
  let match;
  while ((match = pattern.exec(html))) {
    const [whole, tag, attrs, text] = match;
    if (whole.startsWith('<!--')) continue;
    if (text !== undefined) {
      const node = new Element('#text');
      node.textContent = decode(text);
      stack.at(-1).append(node);
      continue;
    }
    if (!tag) continue;
    const name = tag.toLowerCase();
    if (whole.startsWith('</')) {
      if (stack.length > 1) stack.pop();
      continue;
    }
    const node = new Element(name);
    for (const attr of attrs.matchAll(/([\w:-]+)(?:\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s"'>]+)))?/g)) {
      node.setAttribute(attr[1], attr[2] ?? attr[3] ?? attr[4] ?? '');
      if (attr[1] === 'class') node.className = attr[2] ?? attr[3] ?? attr[4] ?? '';
      if (attr[1] === 'checked') node.checked = true;
      if (attr[1] === 'disabled') node.disabled = true;
    }
    stack.at(-1).append(node);
    if (!VOID.has(name) && !whole.endsWith('/>')) stack.push(node);
  }
}

function matches(node, part) {
  if (node._text !== undefined && node.tagName === '#text') return false;
  const m = /^([a-zA-Z][\w-]*)?(?:\.([\w-]+))?(?:#([\w-]+))?(?:\[([\w:-]+)(?:(\^?=)"([^"]*)")?\])?$/.exec(part);
  if (!m) return false;
  const [, tag, cls, id, attr, op, value] = m;
  if (tag && node.tagName !== tag.toLowerCase()) return false;
  if (cls && !node.className.split(' ').includes(cls)) return false;
  if (id && node.getAttribute('id') !== id) return false;
  if (attr) {
    const actual = node.getAttribute(attr);
    if (actual === null) return false;
    if (op === '=' && actual !== value) return false;
    if (op === '^=' && !actual.startsWith(value)) return false;
  }
  return true;
}

function selectAll(root, selector) {
  const parts = selector.trim().split(/\s+/);
  const out = [];
  const walk = (node) => {
    const chain = [];
    let cursor = node;
    for (let i = parts.length - 1; i >= 0; i -= 1) {
      if (!cursor || !matches(cursor, parts[i])) return;
      chain.push(cursor);
      cursor = cursor.parentNode;
    }
    if (chain.length) out.push(node);
  };
  const visit = node => { walk(node); for (const child of node.children) visit(child); };
  visit(root);
  return out;
}

// ---- Environment ----------------------------------------------------------
const written = [];
const storage = new Map();
const localStorage = {
  getItem: key => (storage.has(key) ? storage.get(key) : null),
  setItem: (key, value) => { storage.set(key, String(value)); },
  removeItem: key => { storage.delete(key); },
};
const highlights = [];
const sanitizeOptions = [];
const window = {};
const document = {
  createElement: tag => new Element(tag),
  createTextNode: text => { const e = new Element('#text'); e.textContent = text; return e; },
  querySelectorAll: selector => selectAll(document.body, selector),
  getElementById: () => null,
  addEventListener() {},
};
document.body = new Element('body');
const context = vm.createContext({
  Element, document, window, navigator: {clipboard: {writeText: async () => {}}},
  setTimeout: (fn, ms) => setTimeout(fn, ms), clearTimeout,
  localStorage, console, JSON, Date, String, Boolean, Set, Map, Array, RegExp,
  hljs: {highlightElement(code) { highlights.push(code.textContent); }},
});
window.localStorage = localStorage;
vm.runInContext(readFileSync(new URL('../src/kova/web_assets/vendor/marked.js', import.meta.url), 'utf8'), context);
window.marked = context.marked;
window.DOMPurify = {
  sanitize(html, options) { sanitizeOptions.push(options); return html; },
};
vm.runInContext(readFileSync(new URL('../src/kova/web_assets/code-header.js', import.meta.url), 'utf8'), context);
vm.runInContext(readFileSync(new URL('../src/kova/web_assets/markdown-view.js', import.meta.url), 'utf8'), context);
const Markdown = window.KovaMarkdown;

const render = (text, final = true) => {
  const root = new Element('div');
  root.className = 'markdown';
  document.body.append(root);
  Markdown.render(root, text, {final});
  return root;
};

// ---- Open fences ----------------------------------------------------------
assert.equal(Markdown.hasOpenFence('```js\nconst a = 1;'), true, 'an unclosed fence is open');
assert.equal(Markdown.hasOpenFence('```js\nconst a = 1;\n```'), false, 'a closed fence is done');
assert.equal(Markdown.hasOpenFence('~~~\nplain\n~~~'), false, 'tilde fences close too');
assert.equal(Markdown.hasOpenFence('no code here'), false, 'prose has no fence');

// ---- Tables ----------------------------------------------------------------
const table = render('| Name | State |\n|---|---|\n| One | Done |\n| Two | Next |');
const frame = table.querySelector('.table-wrap');
assert.ok(frame, 'a table is wrapped for scrolling');
assert.equal(frame.querySelector('table').tagName, 'table', 'the table stays inside the frame');
assert.equal(frame.tabIndex, 0, 'the frame is keyboard scrollable');
assert.equal(frame.getAttribute('role'), 'region');
assert.equal(frame.getAttribute('aria-label'), 'Table');

// ---- Task lists are read-only ---------------------------------------------
const tasks = render('- [x] shipped\n- [ ] pending');
const boxes = tasks.querySelectorAll('input');
assert.equal(boxes.length, 2, 'both checkboxes survive sanitizing');
assert.ok(boxes.every(box => box.disabled), 'every task box is read-only');
assert.equal(boxes[0].checked, true, 'a checked item stays checked');
assert.equal(boxes[0].getAttribute('aria-label'), 'Done');
assert.equal(boxes[1].getAttribute('aria-label'), 'Not done');
const items = tasks.querySelectorAll('li');
assert.ok(items.every(item => item.classList.contains('task-item')), 'task items lose the bullet');
assert.ok(items[0].classList.contains('task-done'), 'finished work is marked');

// ---- Footnotes and per-message ids ---------------------------------------
const note = 'Claim[^a] and a second[^b].\n\n[^a]: First note.\n[^b]: Second note.';
const first = render(note);
const second = render(note);
const firstRefs = first.querySelectorAll('.fn-ref');
const secondRefs = second.querySelectorAll('.fn-ref');
assert.equal(firstRefs.length, 2, 'both references render');
const notes = first.querySelector('.footnotes');
assert.ok(notes, 'the footnote list is rendered');
assert.equal(notes.querySelectorAll('li').length, 2, 'both notes render');
const firstId = firstRefs[0].getAttribute('id');
const secondId = secondRefs[0].getAttribute('id');
assert.notEqual(firstId, secondId, 'two replies do not share footnote ids');
assert.equal(firstRefs[0].querySelector('a').getAttribute('href'), `#${firstId.replace('fnref-', 'fn-')}`,
  'the reference points at its own note');
assert.ok(firstId.endsWith('-fnref-1'), 'the message prefix scopes the id');
assert.ok(notes.querySelectorAll('[id]')[0].getAttribute('id').endsWith('-fn-1'),
  'the note id carries the same scope');

// Re-rendering the same block while it streams keeps its prefix, so anchors hold.
first.textContent = '';
Markdown.render(first, note, {final: true});
assert.equal(first.querySelector('.fn-ref').getAttribute('id'), firstId,
  're-rendering one block keeps its scope');

const codeRef = render('```\n[^a]\n```\n\n[^a]: note');
assert.equal(codeRef.querySelectorAll('.fn-ref').length, 0, 'a reference inside code is literal');

// Footnotes wait for the finished reply, so the note list does not move.
const streamingNotes = render('Claim[^a].\n\n[^a]: Note.', false);
assert.equal(streamingNotes.querySelectorAll('.fn-ref').length, 0, 'no footnote while streaming');
assert.equal(streamingNotes.querySelector('.footnotes'), null, 'no note list while streaming');
const finishedNotes = render('Claim[^a].\n\n[^a]: Note.', true);
assert.equal(finishedNotes.querySelectorAll('.fn-ref').length, 1, 'the finished reply shows the note');

// ---- Sanitizer keeps what the renderer needs ------------------------------
const options = sanitizeOptions.at(-1);
for (const tag of ['sup', 'section', 'input', 'table', 'details', 'summary']) {
  assert.ok(options.ALLOWED_TAGS.includes(tag), `the allowlist keeps <${tag}>`);
}
for (const attr of ['id', 'href', 'type', 'checked', 'disabled', 'aria-label', 'open']) {
  assert.ok(options.ALLOWED_ATTR.includes(attr), `the allowlist keeps ${attr}`);
}

// ---- Callouts and collapsible blocks (#189) -------------------------------
const callout = render('> [!NOTE]\n> The build is green.');
const quote = callout.querySelector('blockquote');
assert.ok(quote.classList.contains('callout'), 'a marker turns a quote into a callout');
assert.ok(quote.classList.contains('callout-note'), 'the kind is on the element');
assert.equal(quote.getAttribute('role'), 'note');
assert.equal(quote.querySelector('.callout-label').textContent, 'Note', 'the kind is said in words');
assert.ok(quote.textContent.includes('The build is green.'), 'the body is kept');
assert.equal(quote.textContent.includes('[!NOTE]'), false, 'the marker itself is gone');

const warning = render('> [!WARNING]\n> Deleting this cannot be undone.');
assert.ok(warning.querySelector('blockquote').classList.contains('callout-warning'));
assert.equal(warning.querySelector('.callout-label').textContent, 'Warning');

const plainQuote = render('> Just a quote.');
assert.equal(plainQuote.querySelector('blockquote').classList.contains('callout'), false,
  'a quote without a marker stays a quote');

const details = render('<details>\n<summary>More</summary>\n\nThe body.\n\n</details>');
const disclosure = details.querySelector('details');
assert.ok(disclosure, 'a details block survives rendering');
assert.equal(disclosure.querySelector('summary').textContent, 'More');
assert.ok(disclosure.textContent.includes('The body.'), 'the body is kept');
assert.equal(details.querySelectorAll('details').length, 1, 'one block, not a second wrapper');

// ---- Streaming code is plain until it closes -----------------------------
highlights.length = 0;
const streaming = render('```js\nconst done = 1;\n```\n\n```js\nconst open = 2;', false);
const blocks = streaming.querySelectorAll('pre code');
assert.equal(blocks.length, 2, 'both fences render while streaming');
assert.equal(highlights.length, 1, 'the closed fence is highlighted and the open one is not');
assert.equal(blocks[1].getAttribute('data-streaming') !== null || blocks[1].parentNode.getAttribute('data-streaming') !== null, true,
  'the open block is marked as streaming');
const copies = streaming.querySelectorAll('.copy-code');
assert.equal(copies[0].disabled, false, 'a finished block can be copied');
assert.equal(copies[1].disabled, true, 'a block still arriving cannot be copied');
assert.equal(copies[1].getAttribute('aria-label'), 'Copy code, available when the block finishes');

highlights.length = 0;
const finished = render('```js\nconst done = 1;\n```', true);
assert.equal(highlights.length, 1, 'the closed block is highlighted when the reply ends');
assert.equal(finished.querySelector('.copy-code').disabled, false, 'copy works once finished');

// ---- Wrap is one persisted preference ------------------------------------
const wrapRoot = render('```js\nconst aVeryLongLine = "that would otherwise scroll sideways";\n```');
const wrap = wrapRoot.querySelector('.wrap-code');
const pre = wrapRoot.querySelector('pre');
assert.equal(wrap.getAttribute('aria-pressed'), 'false', 'wrap starts off');
wrap.click();
assert.equal(wrap.getAttribute('aria-pressed'), 'true', 'the toggle is pressed');
assert.ok(pre.classList.contains('wrap-lines'), 'the block wraps');
assert.equal(localStorage.getItem('kova-code-wrap'), '1', 'the choice is persisted');
const later = render('```js\nconsole.log(1);\n```');
assert.ok(later.querySelector('pre').classList.contains('wrap-lines'), 'later blocks inherit the choice');
assert.equal(later.querySelector('.wrap-code').getAttribute('aria-pressed'), 'true');

console.log('Markdown view passed');
