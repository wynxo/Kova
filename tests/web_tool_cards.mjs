import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import vm from 'node:vm';
class Element {
  constructor(tag) { this.tagName = tag; this.children = []; this.attrs = {}; this.text = ''; this.classList = {toggle() {}}; }
  set textContent(value) { this.text = String(value); this.children = []; }
  get textContent() { return this.text + this.children.map(child => child.textContent).join(''); }
  append(...nodes) { for (const node of nodes) { node.parent = this; this.children.push(node); } }
  setAttribute(key, value) { this.attrs[key] = value; }
  remove() { this.parent.children.splice(this.parent.children.indexOf(this), 1); }
}
let now = 0, diffs = 0;
const context = {performance: {now: () => now}, document: {createElement: tag => new Element(tag)}, KovaDiff: {file: () => { diffs++; return new Element('div'); }}};
vm.createContext(context);
vm.runInContext(readFileSync(new URL('../src/kova/web_assets/tool-cards.js', import.meta.url), 'utf8') + '\nglobalThis.cards = KovaToolCards;', context);
const group = context.cards.create({now: () => now});
const first = group.add({name: 'run', args: '<script>bad</script>'});
group.add({name: 'run', args: {command: 'test'}});
assert.equal(group.node.children[0].open, undefined, 'collapsed by default');
assert.match(group.node.textContent, /Running 2 tools/);
now = 1200;
group.result({name: 'run', ok: true, output: '<img onerror=bad>' + 'x'.repeat(7000), file_change: {}});
assert.equal(first.status, 'ok');
assert.equal(diffs, 1);
assert.match(first.details.textContent, /1.2s/);
assert.equal(first.details.children[2].textContent.length, 6000);
first.details.children[3].onclick();
assert.equal(first.details.children[2].textContent.length, 7017);
group.result({name: 'run', ok: false, output: 'Oops'});
assert.match(group.node.children[1].textContent, /run: Oops/);
assert.match(group.node.textContent, /Ran 2 tools · 2.4s · 1 failed/);
const interrupted = group.add({name: 'read'}); group.finish();
assert.equal(interrupted.statusNode.textContent, 'Interrupted');
const saved = context.cards.create({saved: true});
saved.add({id: 'a', name: 'read'}); saved.result({id: 'a', output: 'history'});
assert.match(saved.node.textContent, /timing unavailable/);
assert.doesNotMatch(saved.node.textContent, /OK/);
console.log('tool cards: ok');
