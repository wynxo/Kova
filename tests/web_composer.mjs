// Composer history and @-mentions (#197), with no browser or packages.
//
// composer-history.js and composer-mentions.js are plain logic: the field is a
// tiny stub, and the list is a stub node, so the checks describe behaviour
// rather than the DOM's rendering.
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import vm from 'node:vm';

let focusedElement = null;
class Node {
  constructor(tag = 'div') {
    this.tagName = tag; this.children = []; this.attrs = {}; this.className = '';
    this.hidden = false; this._text = ''; this.listeners = {}; this.parentNode = null;
  }
  set textContent(text) { this._text = String(text); this.children = []; }
  get textContent() { return this._text + this.children.map(node => node.textContent).join(''); }
  setAttribute(key, value) { this.attrs[key] = String(value); }
  getAttribute(key) { return this.attrs[key]; }
  removeAttribute(key) { delete this.attrs[key]; }
  append(...nodes) { for (const node of nodes) { node.parentNode = this; this.children.push(node); } }
  replaceChildren(...nodes) { this._text = ''; this.children = []; this.append(...nodes); }
  contains(node) { return walk(this).includes(node); }
  addEventListener(type, fn) { this.listeners[type] = fn; }
  scrollIntoView() {}
  focus() { focusedElement = this; }
}
const walk = node => [node, ...node.children.flatMap(walk)];
class Field extends Node {
  constructor() { super('textarea'); this.value = ''; this.selectionStart = 0; this.selectionEnd = 0; }
  setSelectionRange(start, end) { this.selectionStart = start; this.selectionEnd = end; }
}
const sandbox = {
  console,
  document: {createElement: tag => new Node(tag)},
  setTimeout: fn => { fn(); return 0; },
  clearTimeout() {},
};
vm.runInNewContext(readFileSync(new URL('../src/kova/web_assets/composer-history.js', import.meta.url), 'utf8') + '\n' +
  readFileSync(new URL('../src/kova/web_assets/composer-mentions.js', import.meta.url), 'utf8'), sandbox);
const H = sandbox.KovaComposerHistory;
const M = sandbox.KovaComposerMentions;
const tick = () => new Promise(resolve => setImmediate(resolve));
const press = (key, extra = {}) => ({key, preventDefault() { this.prevented = (this.prevented || 0) + 1; },
  stopPropagation() { this.stopped = true; }, ...extra});

assert.equal(typeof H, 'object', 'composer-history.js exposes KovaComposerHistory');
assert.equal(typeof M, 'object', 'composer-mentions.js exposes KovaComposerMentions');

// --------------------------------------------------------------- history
const field = new Field();
const messages = [
  {role: 'assistant', content: 'an answer is not history'},
  {role: 'user', content: 'oldest'},
  {role: 'user', content: '  middle  '},
  {role: 'user', content: 'latest'},
  {role: 'user', content: '   '},
];
assert.deepEqual([...H.recalled(messages)], ['oldest', 'middle', 'latest'],
                 'only non-empty user messages are recalled, trimmed');
const history = H.attach(field, {messages});

// An empty box starts at the newest; Up moves older, Down forward, then back
// to the draft it started from.
field.value = '';
let event = press('ArrowUp');
assert.equal(history.keydown(event), true);
assert.equal(field.value, 'latest');
assert.equal(event.prevented, 1, 'Up in an empty box is ours');
history.keydown(press('ArrowUp'));
assert.equal(field.value, 'middle');
history.keydown(press('ArrowUp'));
assert.equal(field.value, 'oldest');
history.keydown(press('ArrowUp'));
assert.equal(field.value, 'oldest', 'the oldest message is a stop, not a bug');
history.keydown(press('ArrowDown'));
assert.equal(field.value, 'middle');
history.keydown(press('ArrowDown'));
assert.equal(field.value, 'latest');
history.keydown(press('ArrowDown'));
assert.equal(field.value, '', 'Down past the newest restores the empty draft');

// A draft is never clobbered; a caret below the first line keeps editing.
field.value = 'my draft'; field.selectionStart = field.value.length;
assert.equal(history.keydown(press('ArrowUp')), false);
assert.equal(field.value, 'my draft');
field.value = 'first line\nsecond line'; field.selectionStart = field.value.length;
assert.equal(history.keydown(press('ArrowUp')), false, 'multi-line editing keeps Up');
field.selectionStart = 3;
assert.equal(history.keydown(press('ArrowUp')), false, 'a non-empty box does not start history');
field.value = ''; field.selectionStart = 0;
assert.equal(history.keydown(press('ArrowUp', {ctrlKey: true})), false, 'modified keys are not ours');
assert.equal(field.value, '', 'Ctrl+Up is left to the browser');

// Escape puts the pre-browse draft back and stops browsing.
field.value = ''; field.selectionStart = 0;
history.keydown(press('ArrowUp'));
assert.equal(field.value, 'latest');
event = press('Escape');
assert.equal(history.keydown(event), true);
assert.equal(field.value, '');
assert.equal(history.browsing, false);
assert.equal(history.keydown(press('Escape')), false, 'Escape is left alone when not browsing');

// After a send, reset stops browsing without rewriting the new empty field.
history.keydown(press('ArrowUp'));
history.reset();
assert.equal(history.browsing, false);
assert.equal(field.value, 'latest', 'reset never touches the text');
field.value = '';

// -------------------------------------------------------------- mentions
assert.deepEqual(JSON.parse(JSON.stringify(M.mentionAt('hello @src/ma', 13))),
                 {start: 6, fragment: 'src/ma'});
assert.deepEqual(JSON.parse(JSON.stringify(M.mentionAt('@', 1))), {start: 0, fragment: ''});
assert.equal(M.mentionAt('name@example.com', 16), null, 'an email is not a mention');
assert.equal(M.mentionAt('no mention here', 15), null);
assert.equal(M.mentionAt('@file', 0), null, 'the caret must be after the @');

let inserted = M.insertMention('see @ma', 7, 'src/main.py');
assert.equal(inserted.value, 'see @src/main.py ');
assert.equal(inserted.caret, inserted.value.length);
inserted = M.insertMention('see @sr', 7, 'src/');
assert.equal(inserted.value, 'see @src/', 'a folder keeps its slash and no space');
inserted = M.insertMention('@ma rest', 3, 'a.py');
assert.equal(inserted.value, '@a.py rest', 'existing whitespace is not doubled');

// The listbox: open on @, move with arrows, insert with Enter, close with Esc.
const textarea = new Field();
const listbox = new Node('div');
listbox.hidden = true;
textarea.setAttribute('aria-controls', 'mention-list');
let changes = 0;
const mentions = M.attach({
  textarea, listbox, onChange: () => { changes += 1; },
  paths: async fragment => fragment.startsWith('m')
    ? ['src/main.py', 'src/math.py', 'readme.md'] : ['readme.md'],
});
textarea.value = '@m';
textarea.selectionStart = 2;
mentions.input();
await tick();
assert.equal(mentions.open, true);
assert.equal(listbox.hidden, false);
assert.equal(listbox.children.length, 3);
assert.equal(textarea.getAttribute('aria-expanded'), 'true');
assert.equal(textarea.getAttribute('aria-activedescendant'), 'mention-option-0');
assert.equal(listbox.children[0].getAttribute('role'), 'option');
assert.equal(listbox.children[0].getAttribute('aria-selected'), 'true');
assert.equal(listbox.children[0].className.includes('active'), true);

event = press('ArrowDown');
assert.equal(mentions.keydown(event), true);
assert.equal(textarea.getAttribute('aria-activedescendant'), 'mention-option-1');
assert.equal(listbox.children[0].getAttribute('aria-selected'), 'false');
assert.equal(listbox.children[1].className.includes('active'), true);
mentions.keydown(press('ArrowUp'));
assert.equal(textarea.getAttribute('aria-activedescendant'), 'mention-option-0');
mentions.keydown(press('ArrowUp'));
assert.equal(textarea.getAttribute('aria-activedescendant'), 'mention-option-2',
             'Up wraps to the last suggestion');

mentions.keydown(press('ArrowDown'));
event = press('Enter');
assert.equal(mentions.keydown(event), true, 'Enter inserts instead of sending');
assert.equal(textarea.value, '@src/main.py ');
assert.equal(textarea.selectionStart, textarea.value.length);
assert.equal(listbox.hidden, true);
assert.equal(textarea.getAttribute('aria-expanded'), 'false');
assert.equal(textarea.getAttribute('aria-activedescendant'), undefined);
assert.equal(changes, 1);

// Escape closes and keeps the Escape key from the document's stop-reply handler.
textarea.value = '@r'; textarea.selectionStart = 2;
mentions.input();
await tick();
assert.equal(mentions.open, true);
event = press('Escape');
assert.equal(mentions.keydown(event), true);
assert.equal(event.stopped, true);
assert.equal(mentions.open, false);
assert.equal(textarea.value, '@r', 'Escape leaves the typed token alone');
assert.equal(textarea.getAttribute('aria-expanded'), 'false');

// No match means no empty box, and a mention in a word stays plain text.
textarea.value = 'nothing'; textarea.selectionStart = 7;
mentions.input();
await tick();
assert.equal(mentions.open, false);
assert.equal(listbox.hidden, true);

// Blur closes; a click inside the list does not.
textarea.value = '@r'; textarea.selectionStart = 2;
mentions.input();
await tick();
assert.equal(mentions.open, true);
textarea.listeners.blur({relatedTarget: listbox.children[0]});
assert.equal(mentions.open, true, 'the list stays while its own option is clicked');
textarea.listeners.blur({relatedTarget: null});
assert.equal(mentions.open, false);

// A stale answer for an earlier token never replaces the current list.
const resolvers = [];
const staleField = new Field();
const staleList = new Node('div');
const stale = M.attach({textarea: staleField, listbox: staleList,
  paths: () => new Promise(resolve => resolvers.push(resolve))});
staleField.value = '@a'; staleField.selectionStart = 2; stale.input();
await tick();
staleField.value = '@ab'; staleField.selectionStart = 3; stale.input();
await tick();
assert.equal(resolvers.length, 2, 'each token asks once');
resolvers[0](['old.py']);
await tick();
assert.equal(stale.open, false, 'an answer for an earlier @token is ignored');
resolvers[1](['new.py']);
await tick();
assert.equal(stale.open, true);
assert.equal(staleList.children[0].textContent, 'new.py');

console.log('Composer history and @-mention behaviour passed');
