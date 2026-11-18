// Per-chat composer drafts (#197): keys, restore, clearing, the incognito
// exclusion and a storage failure that must not throw. Runs the real drafts.js
// with a small localStorage stand-in; no browser or packages.
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import vm from 'node:vm';

const store = new Map();
let failing = false;
const sandbox = {
  window: {}, console,
  localStorage: {
    getItem(key) { if (failing) throw new Error('denied'); return store.has(key) ? store.get(key) : null; },
    setItem(key, value) { if (failing) throw new Error('quota'); store.set(key, String(value)); },
    removeItem(key) { if (failing) throw new Error('denied'); store.delete(key); },
  },
};
vm.runInNewContext(readFileSync(new URL('../src/kova/web_assets/drafts.js', import.meta.url), 'utf8'), sandbox);
const drafts = sandbox.KovaDrafts;
assert.ok(drafts, 'drafts.js exposes KovaDrafts');

const chat = {id: 'abc123'};
const other = {id: 'def456'};
const incognito = {id: 'incognito-abc123', ephemeral: true};

// Only a saved chat has a draft key.
assert.equal(drafts.key(chat), 'kova-draft:abc123');
assert.equal(drafts.key(other), 'kova-draft:def456');
assert.equal(drafts.key(incognito), null);
assert.equal(drafts.key({id: 'incognito-invented'}), null, 'the reserved id alone is enough');
assert.equal(drafts.key(null), null);
assert.equal(drafts.key({}), null);

// Nothing saved reads as an empty composer, never an error.
assert.equal(drafts.read(chat), '');
assert.equal(drafts.read(null), '');
assert.equal(drafts.read(incognito), '');

// Writing keeps the text exactly, per chat; an empty or blank value clears it.
assert.equal(drafts.write(chat, 'unfinished idea\nsecond line'), true);
assert.equal(drafts.read(chat), 'unfinished idea\nsecond line');
assert.equal(drafts.write(other, 'another chat'), true);
assert.equal(drafts.read(chat), 'unfinished idea\nsecond line');
assert.equal(drafts.read(other), 'another chat');
assert.equal(drafts.write(chat, '   '), true);
assert.equal(drafts.read(chat), '');
assert.equal(drafts.read(other), 'another chat');
drafts.clear(other);
assert.equal(drafts.read(other), '');
assert.equal(store.has('kova-draft:def456'), false);

// Incognito never touches storage, even when the text is non-empty.
assert.equal(drafts.write(incognito, 'private thought'), false);
assert.equal([...store.keys()].some(key => key.includes('incognito')), false);

// A refused storage write is reported, not thrown: the draft just stays local
// to the session.
failing = true;
assert.equal(drafts.write(chat, 'kept in memory'), false);
assert.equal(drafts.read(chat), '');
assert.equal(drafts.write(chat, ''), false);
assert.equal(drafts.read(incognito), '');

console.log('drafts ok');
