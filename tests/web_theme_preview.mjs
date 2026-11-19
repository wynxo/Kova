// The theme preview card (#175): a miniature sample painted with a theme's own
// tokens, used by the gallery and the Settings picker. No browser needed.
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import vm from 'node:vm';

const source = readFileSync(new URL('../src/kova/web_assets/theme-preview.js', import.meta.url), 'utf8');

class Node {
  constructor(tag) {
    this.tag = tag;
    this.children = [];
    this.attributes = {};
    this.style = {setProperty(name, value) { this[name] = value; }};
  }
  append(...children) {
    for (const child of children) child.parent = this;
    this.children.push(...children);
  }
  setAttribute(name, value) { this.attributes[name] = value; }
}

const document = {createElement: tag => new Node(tag)};
const window = {};
vm.runInNewContext(source, {window, document});

const tokens = {
  'bg.base': '#14121c', 'bg.surface': '#1c1b24', 'bg.raised': '#26262e',
  'fg.default': '#e8e6f0', 'fg.muted': '#8a8f98', 'fg.faint': '#6b7079',
  'accent.default': '#f6f6f8', 'accent.on': '#0d0c13', 'border.subtle': '#3a3d45',
  'status.danger': '#ff5f6d', 'status.success': '#5fd787', 'status.warning': '#ffaf5f',
};

const card = window.KovaThemePreview.card(tokens);
assert.equal(card.tag, 'div');
assert.equal(card.className, 'theme-preview');
assert.equal(card.attributes['aria-hidden'], 'true', 'the card is decorative');
assert.equal(card.style['--preview-bg-base'], tokens['bg.base'], 'bg.base maps to --preview-bg-base');
assert.equal(card.style['--preview-accent-on'], tokens['accent.on'], 'accent.on maps to --preview-accent-on');
assert.equal(card.style['--preview-status-success'], tokens['status.success'], 'status colours map too');

// A value the theme does not set is never written, so the CSS fallback applies.
assert.equal(card.style['--preview-border-strong'], undefined, 'unmapped tokens are left alone');

// The card has the pieces the stylesheet expects: a sidebar and a body with a
// user bubble, text lines, an accent chip and three status dots.
function find(node, predicate) {
  return predicate(node) ? node : node.children.map(child => find(child, predicate)).find(Boolean);
}
assert.ok(find(card, node => node.className === 'theme-preview-side'));
assert.ok(find(card, node => node.className === 'theme-preview-bubble'));
assert.ok(find(card, node => node.className === 'theme-preview-accent'));
const status = find(card, node => node.className === 'theme-preview-status');
assert.deepEqual(status.children.map(child => child.className), ['ok', 'warn', 'err']);

// A missing tokens object renders an empty-but-valid card instead of throwing.
const bare = window.KovaThemePreview.card(null);
assert.equal(bare.className, 'theme-preview');
assert.equal(bare.style['--preview-bg-base'], undefined);

console.log('Theme preview card: token mapping, structure and graceful fallback passed');
