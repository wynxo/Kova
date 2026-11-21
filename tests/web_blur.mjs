import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';

const source = fs.readFileSync(new URL('../src/kova/web_assets/blur.js', import.meta.url), 'utf8');

async function run({blurred, solid, enabled = true, hidden = false}) {
  const callbacks = [];
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
    addEventListener() {},
  };
  vm.runInNewContext(source, {
    document,
    getComputedStyle: () => ({backdropFilter: 'blur(16px)'}),
    requestAnimationFrame: callback => callbacks.push(callback),
  });

  let now = 0;
  while (callbacks.length) {
    now += attributes.has('data-blur') ? solid : blurred;
    callbacks.shift()(now);
    await Promise.resolve();
  }
  return {switched, slow: attributes.has('data-blur')};
}

assert.deepEqual(await run({blurred: 16, solid: 16}), {switched: false, slow: false});
assert.deepEqual(await run({blurred: 40, solid: 16}), {switched: true, slow: true});
assert.deepEqual(await run({blurred: 40, solid: 38}), {switched: true, slow: false});
assert.deepEqual(await run({blurred: 40, solid: 16, enabled: false}), {switched: false, slow: false});
assert.deepEqual(await run({blurred: 40, solid: 16, hidden: true}), {switched: false, slow: false});
console.log('blur fallback: ok');
