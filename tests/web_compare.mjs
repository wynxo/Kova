// Compare answers (#96): which models can be compared, the default picks,
// how stream events fold into columns, and the speed line. Runs the real
// module with no browser or packages.
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import vm from 'node:vm';

const sandbox = {console};
sandbox.globalThis = sandbox;
sandbox.window = sandbox;
vm.runInNewContext(readFileSync(new URL('../src/kova/web_assets/compare.js', import.meta.url), 'utf8'), sandbox);
const {eligible, defaults, apply, stats, MIN, MAX} = sandbox.KovaCompare;
const plain = value => JSON.parse(JSON.stringify(value));

assert.equal(MIN, 2);
assert.equal(MAX, 4);

// Only configured models count; Local only explains why a cloud one is off.
{
  const models = [
    {name: 'lab', model: 'lab-id', local: true},
    {name: 'cloud', model: 'cloud-id', local: false},
    {name: 'unset', model: ''},
  ];
  assert.deepEqual(plain(eligible(models)).map(m => [m.name, m.blocked]), [['lab', ''], ['cloud', '']]);
  const locked = plain(eligible(models, {localOnly: true}));
  assert.equal(locked[0].blocked, '');
  assert.match(locked[1].blocked, /Local only/);
}

// The composer's model comes first, then the next open one, never a blocked one.
{
  const choices = [{name: 'a', blocked: ''}, {name: 'b', blocked: 'off'}, {name: 'c', blocked: ''}];
  assert.deepEqual(plain(defaults(choices, 'c')), ['c', 'a']);
  assert.deepEqual(plain(defaults(choices, 'b')), ['a', 'c']);
  assert.deepEqual(plain(defaults(choices, null)), ['a', 'c']);
}

// Events go to the column they name; a finished column can be kept.
{
  const view = {columns: {}};
  apply(view, {type: 'start', compare_id: 'x1', models: ['a', 'b', 'c']});
  assert.equal(view.compareId, 'x1');
  assert.equal(view.phase, 'running');
  apply(view, {type: 'reasoning', model: 'a', text: 'hmm'});
  assert.equal(view.columns.a.phase, 'thinking');
  apply(view, {type: 'text', model: 'a', text: 'Hel'});
  apply(view, {type: 'text', model: 'b', text: 'Other'});
  apply(view, {type: 'text', model: 'a', text: 'lo'});
  assert.equal(view.columns.a.text, 'Hello');
  assert.equal(view.columns.a.phase, 'writing');
  assert.equal(view.columns.b.text, 'Other');
  assert.equal(apply(view, {type: 'text', model: 'ghost', text: 'x'}), null);
  const usage = {tokens_per_second: 41.5, completion_tokens: 1234, ms_total: 3200, estimated: false};
  const done = apply(view, {type: 'model_done', model: 'a', text: 'Hello', usage});
  assert.equal(done.phase, 'done');
  assert.equal(done.keepable, true);
  apply(view, {type: 'model_error', model: 'c', message: 'Provider said no'});
  assert.equal(view.columns.c.phase, 'error');
  assert.equal(view.columns.c.error, 'Provider said no');
  // Stopping ends the still-writing column; only the server's kept list can be kept.
  apply(view, {type: 'stopped', compare_id: 'x1', kept: ['a']});
  assert.equal(view.phase, 'stopped');
  assert.equal(view.columns.b.phase, 'stopped');
  assert.equal(view.columns.b.keepable, false);
  assert.equal(view.columns.a.keepable, true);
  assert.equal(view.columns.c.phase, 'error');
}

// A failed request stops every column that was still going.
{
  const view = {columns: {}};
  apply(view, {type: 'start', compare_id: 'x2', models: ['a', 'b']});
  apply(view, {type: 'error', message: 'Local only is on'});
  assert.equal(view.phase, 'error');
  assert.deepEqual(Object.values(view.columns).map(c => c.phase), ['stopped', 'stopped']);
}

// The speed line shows only what the server measured.
assert.equal(stats(null), '');
assert.equal(stats({tokens_per_second: 41.5, completion_tokens: 1234, ms_total: 3200}), '41.5 tok/s · 1.2k tokens · 3.2 s');
assert.equal(stats({completion_tokens: 12, estimated: true}), '~12 tokens');

console.log('web_compare ok');
