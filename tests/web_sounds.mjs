// The optional sound cues: off by default, quiet, synthesized with WebAudio,
// and gated per event. Nothing here downloads or plays an audio file.
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import vm from 'node:vm';

let focused = false;
let contexts = 0;
const oscillators = [];
const gains = [];
class Param {
  constructor() { this.values = []; }
  setValueAtTime(value, at) { this.values.push([value, at]); }
  exponentialRampToValueAtTime(value, at) { this.values.push([value, at]); }
}
class Context {
  constructor() { contexts++; this.currentTime = 1; this.state = 'running'; this.destination = {}; this.resumed = 0; }
  createOscillator() {
    const node = {type: '', frequency: new Param(), started: null, stopped: null,
                  connect: target => target, start(at) { this.started = at; }, stop(at) { this.stopped = at; }};
    oscillators.push(node);
    return node;
  }
  createGain() { const node = {gain: new Param(), connect: target => target}; gains.push(node); return node; }
  resume() { this.resumed++; return Promise.resolve(); }
}

class Element {
  constructor(tag = 'div') {
    this.tagName = tag; this.children = []; this.className = ''; this.value = '';
    this.checked = false; this.disabled = false; this.attrs = {};
  }
  set textContent(text) { this._text = text; this.children = []; }
  get textContent() { return (this._text || '') + this.children.map(node => node.textContent).join(''); }
  append(...nodes) { for (const node of nodes) this.children.push(node); }
  setAttribute(name, value) { this.attrs[name] = String(value); }
  showModal() { this.open = true; }
  close() { this.open = false; }
}
const document = {
  body: new Element('body'),
  createElement: tag => new Element(tag),
  getElementById: () => new Element('button'),
  hasFocus: () => focused,
};

const store = new Map();
const localStorage = {
  getItem: key => (store.has(key) ? store.get(key) : null),
  setItem: (key, value) => { store.set(key, String(value)); },
  removeItem: key => { store.delete(key); },
};
const sandbox = {AudioContext: Context, document, localStorage, console};
vm.createContext(sandbox);
sandbox.window = sandbox;
vm.runInContext(readFileSync(new URL('../src/kova/web_assets/sounds.js', import.meta.url), 'utf8'), sandbox);
const sounds = sandbox.KovaSounds;
const {play, read, save, DEFAULTS, CUES} = sounds;
const walk = node => [node, ...node.children.flatMap(walk)];

// The module runs in its own realm, so compare its values as JSON.
assert.equal(JSON.stringify(read()), JSON.stringify(DEFAULTS));
assert.equal(read().enabled, false, 'sounds stay off until the user turns them on');
assert.equal(play('done'), false);
assert.equal(play('reminder'), false);
assert.equal(contexts, 0, 'an off switch creates no audio context');

// A finished reply is silent while you are looking at the window; the others
// are not, because they are exactly what you would miss.
save({enabled: true});
focused = true;
assert.equal(play('done'), false);
assert.equal(oscillators.length, 0);
assert.equal(play('needs'), true);
assert.equal(oscillators.map(node => node.frequency.values[0][0]).join(), CUES.needs.map(cue => cue[0]).join());
assert.ok(oscillators.every(node => node.type === 'sine'));
assert.ok(oscillators.every(node => node.stopped - node.started <= 0.35), 'cues stay short');
const peak = Math.max(...gains.flatMap(gain => gain.gain.values.map(([value]) => value)));
assert.ok(peak <= 0.16 * read().volume + 1e-9, `a cue is quiet (peak ${peak})`);

// Unfocused, a finished reply does play; both notes are distinct.
oscillators.length = 0;
focused = false;
assert.equal(play('done'), true);
assert.equal(oscillators.length, CUES.done.length);
assert.notEqual(CUES.done[0][0], CUES.done[1][0], 'the cue is a chime, not a beep');

// Per-event switches, an unknown event and a zero volume all stay silent.
save({needs: false});
oscillators.length = 0;
assert.equal(play('needs'), false);
assert.equal(play('reminder'), true);
assert.equal(play('explosion'), false);
save({volume: 0});
oscillators.length = 0;
assert.equal(play('reminder'), false);
assert.equal(oscillators.length, 0);

// Stored values are normalised rather than trusted.
assert.equal(save({volume: 9}).volume, 1);
assert.equal(save({volume: -2}).volume, 0);
save({volume: 0.4});
localStorage.setItem('kova-sounds', 'not json');
assert.equal(read().enabled, false, 'a corrupt preference falls back to off');
localStorage.setItem('kova-sounds', JSON.stringify({enabled: true, volume: 'loud', done: 'yes'}));
const repaired = read();
assert.equal(repaired.enabled, true);
assert.equal(repaired.volume, DEFAULTS.volume);
assert.equal(repaired.done, DEFAULTS.done);
sandbox.localStorage = {getItem() { throw new Error('blocked'); }, setItem() { throw new Error('blocked'); }};
assert.equal(read().enabled, false);
assert.doesNotThrow(() => save({enabled: true}), 'a browser that blocks storage never breaks a turn');
sandbox.localStorage = localStorage;

// The panel is the only UI: one switch, a volume, one toggle per event, all
// built from the shared dialog classes so a theme carries them.
save({enabled: true, volume: 0.4, done: false, needs: true, reminder: true});
const dialog = sounds.panel();
const inputs = walk(dialog).filter(node => node.tagName === 'input');
assert.equal(inputs.length, 5, 'on/off, volume and three event toggles');
const [on, range, ...toggles] = inputs;
assert.equal(range.type, 'range');
assert.equal(on.checked, true);
assert.equal(range.value, '40');
assert.equal(range.disabled, false);
assert.deepEqual(toggles.map(node => node.checked), [false, true, true]);
assert.equal(dialog.attrs['aria-label'], 'Sounds');
assert.ok(dialog.textContent.includes('not focused'), 'the focus rule is stated where it applies');
assert.ok(!walk(dialog).some(node => 'style' in node && node.style), 'the panel sets no colours or layout itself');

on.checked = false; on.onchange();
assert.equal(read().enabled, false, 'turning it off is remembered');
assert.equal(range.disabled, true, 'the other controls follow the switch');
assert.equal(toggles[1].disabled, true);
on.checked = true; on.onchange();
assert.equal(read().enabled, true);
range.value = '80'; range.oninput();
assert.equal(read().volume, 0.8);
toggles[2].checked = false; toggles[2].onchange();
assert.equal(read().reminder, false);

// The module ships no audio and no hard-coded colours.
const source = readFileSync(new URL('../src/kova/web_assets/sounds.js', import.meta.url), 'utf8');
for (const banned of ['fetch(', 'new Audio(', 'XMLHttpRequest', '.mp3', '.wav', '.ogg', '.base64']) {
  assert.ok(!source.includes(banned), `sounds.js must not use ${banned}`);
}
assert.ok(!/#[0-9a-f]{3,8}\b/i.test(source), 'sounds.js must not hard-code a colour');
console.log('Sound cues: off by default, focus-aware, quiet, per-event and file-free');
