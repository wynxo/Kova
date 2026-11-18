// Logic test for the GitHub alerts in code-header.js (#189): the marker becomes
// a visible label, the kind stays in a class, ordinary quotes are left alone and
// a second attach cannot duplicate the label. Runs against a small tree DOM
// stub; no browser or model is needed.
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import vm from 'node:vm';

class Node {
  constructor(tag) {
    this.tagName = tag;
    this.children = [];
    this.parentNode = null;
    this.className = '';
    this.attrs = {};
    this._text = '';
  }
  get firstChild() { return this.children[0] ?? null; }
  get firstElementChild() { return this.children.find(child => child.tagName !== '#text') ?? null; }
  get textContent() { return this.children.length ? this.children.map(child => child.textContent).join('') : this._text; }
  set textContent(value) { this._text = String(value); this.children = []; }
  append(...nodes) { for (const node of nodes) { node.parentNode = this; this.children.push(node); } return this; }
  insertBefore(node, ref) {
    node.parentNode = this;
    const index = ref ? this.children.indexOf(ref) : -1;
    if (index === -1) this.children.push(node);
    else this.children.splice(index, 0, node);
    return node;
  }
  remove() {
    const index = this.parentNode?.children.indexOf(this) ?? -1;
    if (index !== -1) this.parentNode.children.splice(index, 1);
    this.parentNode = null;
  }
  setAttribute(name, value) { this.attrs[name] = String(value); }
  getAttribute(name) { return name in this.attrs ? this.attrs[name] : null; }
  addEventListener() {}
  get classList() {
    const names = () => this.className.split(' ').filter(Boolean);
    return {
      contains: name => names().includes(name),
      add: name => { if (!names().includes(name)) this.className = [...names(), name].join(' '); },
    };
  }
  querySelectorAll(selector) {
    if (!/^[a-z]+$/.test(selector)) return [];
    const found = [];
    const visit = node => {
      for (const child of node.children) {
        if (child.tagName === selector) found.push(child);
        visit(child);
      }
    };
    visit(this);
    return found;
  }
}

const text = value => { const node = new Node('#text'); node.textContent = value; return node; };
const element = tag => new Node(tag);
const document = {createElement: element, querySelectorAll: () => []};
const window = {};
const context = vm.createContext({
  document, window, console, String, Boolean, RegExp, Set, Map, Array,
  localStorage: {getItem: () => null, setItem: () => {}}, setTimeout, clearTimeout,
});
vm.runInContext(readFileSync(new URL('../src/kova/web_assets/code-header.js', import.meta.url), 'utf8'), context);
const CodeHeader = window.CodeHeader;

function reply(marker = '[!NOTE]', body = 'Useful information.') {
  const root = element('div');
  const quote = element('blockquote');
  const para = element('p');
  para.append(text(marker), element('br'), text(body));
  quote.append(para);
  root.append(quote);
  CodeHeader.attach(root, true, false);
  return root;
}

const note = reply();
const quote = note.querySelectorAll('blockquote')[0];
assert.ok(quote.classList.contains('callout'), 'an alert becomes a callout');
assert.ok(quote.classList.contains('callout-note'), 'the alert type names the callout');
assert.equal(quote.getAttribute('role'), 'note', 'the callout is a note for assistive tech');
assert.equal(quote.firstElementChild.tagName, 'p', 'the label is its own paragraph');
assert.equal(quote.firstElementChild.className, 'callout-label');
assert.equal(quote.firstElementChild.textContent, 'Note', 'the kind is written out');
assert.ok(!quote.textContent.includes('[!NOTE]'), 'the marker is removed');
assert.ok(quote.textContent.includes('Useful information.'), 'the body stays');

for (const [marker, kind, label] of [
  ['TIP', 'tip', 'Tip'],
  ['IMPORTANT', 'important', 'Important'],
  ['WARNING', 'warning', 'Warning'],
  ['CAUTION', 'caution', 'Caution'],
]) {
  const alert = reply(`[!${marker}]`, 'Body.');
  const found = alert.querySelectorAll('blockquote')[0];
  assert.ok(found.classList.contains(`callout-${kind}`), `${marker} maps to ${kind}`);
  assert.equal(found.firstElementChild.textContent, label, `${marker} has its label`);
}

const ordinary = element('div');
const plain = element('blockquote');
const paragraph = element('p');
paragraph.append(text('Just a quote.'));
plain.append(paragraph);
ordinary.append(plain);
CodeHeader.attach(ordinary, true, false);
assert.equal(plain.className, '', 'an ordinary quote is not a callout');

const inline = element('div');
const inlineParagraph = element('p');
inlineParagraph.append(text('This mentions [!NOTE] in passing.'));
inline.append(inlineParagraph);
CodeHeader.attach(inline, true, false);
assert.equal(inline.querySelectorAll('blockquote').length, 0, 'the marker only works at the top of a quote');

CodeHeader.attach(note, true, false);
assert.equal(note.querySelectorAll('blockquote')[0].className, 'callout callout-note',
  'a second attach does not add another callout');

console.log('Markdown callouts passed');
