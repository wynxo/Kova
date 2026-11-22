import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';

const source = fs.readFileSync(new URL('../src/kova/web_assets/blur.js', import.meta.url), 'utf8');

async function run({blurred, solid, enabled = true, hidden = false, interruptAfter, resumeWithoutHiddenFrames = false,
  activateOn}) {
  const callbacks = [];
  const listeners = {};
  const attributes = new Map(enabled ? [['data-background', 'gradient']] : []);
  let switched = false;
  const document = {
    hidden,
    documentElement: {
      hasAttribute: name => attributes.has(name),
      setAttribute(name, value) {
        attributes.set(name, value);
        switched = true;
      },
      removeAttribute: name => attributes.delete(name),
    },
    querySelector: () => ({}),
    addEventListener(type, callback) { listeners[type] = callback; },
  };
  vm.runInNewContext(source, {
    document,
    getComputedStyle: () => ({backdropFilter: enabled ? 'blur(16px)' : 'none'}),
    requestAnimationFrame: callback => callbacks.push(callback),
  });

  let now = 0;
  async function frames(limit = Infinity) {
    for (let count = 0; callbacks.length && count < limit; count++) {
      now += attributes.has('data-blur') ? solid : blurred;
      callbacks.shift()(now);
      await Promise.resolve();
    }
  }
  if (interruptAfter) {
    await frames(interruptAfter);
    document.hidden = true;
    listeners.visibilitychange();
    if (!resumeWithoutHiddenFrames) {
      await frames();
      assert.equal(attributes.has('data-blur'), false, 'an interrupted measurement is discarded');
    }
    document.hidden = false;
    listeners.visibilitychange();
  }
  await frames();
  if (activateOn) {
    enabled = true;
    attributes.set('data-background', 'gradient');
    listeners[activateOn]?.();
    await frames();
  }
  return {switched, slow: attributes.has('data-blur')};
}

assert.deepEqual(await run({blurred: 16, solid: 16}), {switched: false, slow: false});
assert.deepEqual(await run({blurred: 40, solid: 16}), {switched: true, slow: true});
assert.deepEqual(await run({blurred: 40, solid: 38}), {switched: true, slow: false});
assert.deepEqual(await run({blurred: 40, solid: 16, enabled: false}), {switched: false, slow: false});
assert.deepEqual(await run({blurred: 40, solid: 16, hidden: true}), {switched: false, slow: false});
assert.deepEqual(await run({blurred: 40, solid: 16, interruptAfter: 4}), {switched: true, slow: true});
assert.deepEqual(await run({blurred: 40, solid: 16, interruptAfter: 16}), {switched: true, slow: true});
assert.deepEqual(await run({blurred: 40, solid: 16, interruptAfter: 4, resumeWithoutHiddenFrames: true}),
  {switched: true, slow: true});
for (const activateOn of ['kova:typography', 'kova:motion']) {
  assert.deepEqual(await run({blurred: 40, solid: 16, enabled: false, activateOn}), {switched: true, slow: true});
}
console.log('blur fallback: ok');
