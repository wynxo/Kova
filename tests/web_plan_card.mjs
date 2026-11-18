// The pinned plan card (#193): one row above the message box while a turn
// runs, the same plan handed back to the transcript when it ends.
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import vm from 'node:vm';

class Element {
  constructor(tag) {
    this.tagName = tag; this.children = []; this.parentNode = null;
    this.className = ''; this.hidden = false; this.attrs = {}; this.style = {};
    this.listeners = {}; this._text = '';
    this.classList = {
      toggle: (name, force) => {
        const names = new Set(this.className.split(' ').filter(Boolean));
        const add = force ?? !names.has(name);
        if (add) names.add(name); else names.delete(name);
        this.className = [...names].join(' ');
      },
      contains: name => this.className.split(' ').includes(name),
    };
  }
  set textContent(value) { this._text = String(value); this.children = []; }
  get textContent() { return this._text + this.children.map(child => child.textContent).join(''); }
  append(...nodes) { for (const node of nodes) { node.parentNode = this; this.children.push(node); } }
  replaceChildren(...nodes) { this._text = ''; this.children = []; this.append(...nodes); }
  insertBefore(node, before) {
    const at = this.children.indexOf(before);
    node.parentNode = this;
    this.children.splice(at < 0 ? this.children.length : at, 0, node);
  }
  setAttribute(key, value) { this.attrs[key] = String(value); }
  getAttribute(key) { return this.attrs[key]; }
  removeAttribute(key) { delete this.attrs[key]; }
  addEventListener(type, fn) { this.listeners[type] = fn; }
  dispatch(type, event = {}) { this.listeners[type]?.(event); }
  focus() { this.focused = true; }
}

const composer = new Element('form');
const wrap = new Element('div');
wrap.append(composer);
const context = {
  window: {},
  document: {
    createElement: tag => new Element(tag),
    getElementById: id => (id === 'composer' ? composer : null),
    querySelector: selector => (selector === '.compose-wrap' ? wrap : null),
  },
};
vm.createContext(context);
vm.runInContext(readFileSync(new URL('../src/kova/web_assets/plan-card.js', import.meta.url), 'utf8')
  + '\nglobalThis.planCard = KovaPlanCard;', context);
const cardModule = context.planCard;

const parts = () => {
  const slot = wrap.children[0];
  const card = slot.children[0];
  const head = card.children[0];
  return {
    slot, card,
    toggle: head.children[0],
    count: head.children[0].children[0],
    step: head.children[0].children[2],
    dismiss: head.children[1],
    bar: card.children[1],
    fill: card.children[1].children[0],
    list: card.children[2],
    status: card.children[3],
  };
};
const plan = (statuses, texts = []) => statuses.map((status, index) => ({text: texts[index] || `Step ${index + 1}`, status}));

// The transcript plan stands aside while the pinned card is up.
const transcript = new Element('section');
cardModule.update(plan(['done', 'in_progress', 'pending'], ['Read the issue', 'Write the card', 'Ship it']), transcript);
const view = parts();
assert.equal(view.slot.parentNode, wrap, 'the card lives inside the composer wrapper');
assert.equal(wrap.children[1], composer, 'and sits just above the form');
assert.equal(view.card.hidden, false, 'an unfinished plan pins the card');
assert.equal(transcript.hidden, true, 'the transcript copy waits its turn');
assert.equal(view.count.textContent, 'Step 2 of 3');
assert.equal(view.step.textContent, 'Write the card');
assert.equal(view.bar.attrs.role, 'progressbar');
assert.equal(view.bar.attrs['aria-valuenow'], '1');
assert.equal(view.bar.attrs['aria-valuemax'], '3');
assert.equal(view.fill.style.width, '33%', 'one of three is a third of the bar');
assert.match(view.status.textContent, /Step 2 of 3\. Write the card/);
assert.ok(view.list.children[1].classList.contains('plan-in_progress'));
assert.ok(view.list.children[0].classList.contains('plan-done'), 'done rows reuse the transcript classes');
assert.equal(view.toggle.attrs['aria-expanded'], 'false');
assert.equal(view.list.hidden, true);

// A reworded step is the same transition: no second announcement.
const reworded = plan(['done', 'in_progress', 'pending'], ['Read the issue', 'Write the small card', 'Ship it']);
cardModule.update(reworded, transcript);
assert.match(parts().status.textContent, /Write the card/, 'the status keeps the announced step');
assert.equal(parts().count.textContent, 'Step 2 of 3', 'the visible row still follows the plan');

// Ticking a step off is a transition and is announced once.
cardModule.update(plan(['done', 'done', 'in_progress'], ['Read the issue', 'Write the card', 'Ship it']), transcript);
assert.match(parts().status.textContent, /Step 3 of 3\. Ship it/);
assert.equal(parts().bar.attrs['aria-valuenow'], '2');

// The disclosure opens the full checklist, and Escape folds it again.
parts().toggle.onclick();
assert.equal(parts().list.hidden, false);
assert.equal(parts().toggle.attrs['aria-expanded'], 'true');
assert.ok(parts().card.classList.contains('is-open'), 'the phone sheet opens from the same class');
let stopped = false;
parts().card.dispatch('keydown', {key: 'Escape', preventDefault() {}, stopPropagation() { stopped = true; }});
assert.equal(parts().list.hidden, true);
assert.equal(stopped, true, 'Escape belongs to the sheet, not the running turn');

// Dismissing hides the card for this turn and lets the transcript plan stand in.
parts().dismiss.onclick();
assert.equal(parts().card.hidden, true);
assert.equal(transcript.hidden, false, 'the dismissed card leaves the transcript plan visible');
const nextTranscript = new Element('section');
cardModule.update(plan(['done', 'done', 'in_progress']), nextTranscript);
assert.equal(parts().card.hidden, true, 'a dismissed card stays away for the rest of the turn');
assert.equal(nextTranscript.hidden, false);

// The next turn gets a fresh card; finishing hands the plan back for good.
cardModule.finish();
assert.equal(parts().card.hidden, true);
const third = new Element('section');
cardModule.update(plan(['pending', 'pending'], ['One', 'Two']), third);
assert.equal(parts().card.hidden, false, 'finish resets the dismissal for the next turn');
assert.equal(third.hidden, true);
assert.equal(parts().count.textContent, 'Step 1 of 2');
cardModule.finish();
assert.equal(parts().card.hidden, true);
assert.equal(third.hidden, false, 'the transcript owns the plan once the turn ends');

// All steps done needs no pinned card at all.
const fourth = new Element('section');
cardModule.update(plan(['done', 'done']), fourth);
assert.equal(parts().card.hidden, true);
assert.equal(fourth.hidden, false);

// Navigating away clears the card without resurrecting an old transcript.
cardModule.reset();
assert.equal(parts().card.hidden, true);
assert.equal(cardModule.isOpen(), false);
