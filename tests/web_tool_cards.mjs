import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import vm from 'node:vm';
class Element {
  constructor(tag) {
    this.tagName = tag; this.children = []; this.attrs = {}; this.text = ''; this.hidden = false;
    this.className = '';
    this.classList = {
      toggle: (name, force) => {
        const names = new Set(this.className.split(' ').filter(Boolean));
        const add = force ?? !names.has(name); if (add) names.add(name); else names.delete(name);
        this.className = [...names].join(' ');
      },
      contains: name => this.className.split(' ').includes(name),
    };
  }
  set textContent(value) { this.text = String(value); this.children = []; }
  get textContent() { return this.text + this.children.map(child => child.textContent).join(''); }
  append(...nodes) { for (const node of nodes) { node.parent = this; this.children.push(node); } }
  setAttribute(key, value) { this.attrs[key] = String(value); }
  remove() { this.parent.children.splice(this.parent.children.indexOf(this), 1); }
}
const byClass = (node, cls) => [node, ...node.children.flatMap(child => byClass(child, cls))]
  .filter(child => child.className.split(' ').includes(cls));
let now = 0, diffs = 0;
const context = {performance: {now: () => now}, document: {createElement: tag => new Element(tag)}, KovaDiff: {file: () => { diffs++; return new Element('div'); }}};
vm.createContext(context);
vm.runInContext(readFileSync(new URL('../src/kova/web_assets/tool-cards.js', import.meta.url), 'utf8') + '\nglobalThis.cards = KovaToolCards;', context);

const group = context.cards.create({now: () => now});
const summary = group.node.children[0];
assert.equal(summary.tagName, 'button', 'the group row is a real button');
assert.equal(summary.attrs['aria-expanded'], 'false', 'a new card is closed until it has something to say');
const first = group.add({name: 'run', args: '<script>bad</script>'});
group.add({name: 'run', args: {command: 'test'}});
assert.equal(summary.attrs['aria-expanded'], 'true', 'it opens by itself while the turn runs');
assert.match(group.node.textContent, /Running 2 tools/);
assert.ok(byClass(summary, 'tool-card-mark')[0].classList.contains('running'), 'a running mark');

now = 1200;
group.result({name: 'run', ok: true, output: '<img onerror=bad>' + 'x'.repeat(7000), file_change: {}});
assert.equal(first.status, 'ok');
assert.equal(first.statusNode.textContent, 'Done', 'the step status is written, not only drawn');
assert.equal(first.time.textContent, '1.2s');
assert.equal(diffs, 1, 'a file change still renders its diff');
const output = byClass(first.body, 'tool-card-output')[0];
assert.equal(output.textContent.length, 6000, 'long output is clipped');
assert.match(first.body.textContent, /Show all \(1 line\)/);
const all = byClass(first.body, 'tool-card-all')[0];
all.onclick();
assert.equal(output.textContent.length, 7017, 'Show all reveals the rest of the output');
assert.equal(all.attrs['aria-expanded'], 'true');
all.onclick();
assert.equal(output.textContent.length, 6000, 'Show less clips it again');
assert.equal(all.attrs['aria-expanded'], 'false');

// One step opens on its own disclosure, with its arguments and result inside.
assert.equal(first.title.attrs['aria-controls'], first.body.attrs.id);
assert.equal(first.body.hidden, true, 'a step starts folded');
first.title.onclick();
assert.equal(first.body.hidden, false);
assert.equal(first.title.attrs['aria-expanded'], 'true');
assert.match(first.body.textContent, /<script>bad<\/script>/);

group.result({name: 'run', ok: false, output: 'Oops'});
assert.equal(group.node.children[2].textContent, 'run: Oops', 'a failed call repeats above the fold');
assert.match(group.node.children[3].textContent, /run failed\./, 'a failure is announced once');
assert.match(group.node.textContent, /Ran 2 tools · 2.4s · 1 failed/);
assert.ok(byClass(summary, 'tool-card-mark')[0].classList.contains('failed'), 'a failure colours the group mark');

const interrupted = group.add({name: 'read'}); group.finish();
assert.equal(interrupted.statusNode.textContent, 'Interrupted');
assert.equal(interrupted.time.textContent, '0.0s');
assert.equal(summary.attrs['aria-expanded'], 'false', 'a finished turn folds the group');
assert.equal(group.node.children[1].hidden, true);
assert.match(group.node.children[2].textContent, /read: Interrupted/, 'failed calls stay visible when folded');
assert.match(group.node.children[3].textContent, /^Ran 3 tools/, 'the finish is announced as one summary');

// A group the reader opened stays open when the turn ends.
const kept = context.cards.create({now: () => now});
const keptSummary = kept.node.children[0];
kept.add({name: 'read', args: {path: 'a.txt'}});
keptSummary.onclick(); keptSummary.onclick();
assert.equal(keptSummary.attrs['aria-expanded'], 'true');
kept.finish();
assert.equal(keptSummary.attrs['aria-expanded'], 'true', 'the reader opened it, so it stays open');

const saved = context.cards.create({saved: true});
saved.add({id: 'a', name: 'read', hint: 'notes.txt'}); saved.result({id: 'a', output: 'history'});
assert.match(saved.node.textContent, /timing unavailable/);
assert.equal(saved.node.children[0].attrs['aria-expanded'], 'false', 'history stays folded');
assert.doesNotMatch(saved.node.textContent, /OK/);
console.log('tool cards: ok');
