// The composer's model picker data, with no browser: model-picker.js groups
// the configured models by connection, keeps every connected provider visible,
// and restores a welcome-screen choice only while it still exists.
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import vm from 'node:vm';

const sandbox = {console, URL};
vm.runInNewContext(readFileSync(new URL('../src/kova/web_assets/model-picker.js', import.meta.url), 'utf8'), sandbox);
const P = sandbox.KovaModelPicker;
assert.equal(typeof P, 'object', 'model-picker.js exposes KovaModelPicker');

const models = [
  {name: 'openai/gpt-5', model: 'gpt-5', provider: 'openai', provider_name: 'OpenAI', base_url: 'https://api.openai.com/v1', local: false, reasoning_efforts: ['low', 'high']},
  {name: 'openai/gpt-5-mini', model: 'gpt-5-mini', provider: 'openai', provider_name: 'OpenAI', base_url: 'https://api.openai.com/v1', local: false},
  {name: 'lab/qwen', model: 'qwen3', provider: 'lab', provider_name: 'Lab server', base_url: 'http://127.0.0.1:11434/v1', local: true},
  {name: 'placeholder', model: '', provider: 'local', provider_name: 'local', base_url: 'http://127.0.0.1:11434/v1', local: true},
];
const providers = [
  {id: 'openai', name: 'OpenAI', base_url: 'https://api.openai.com/v1'},
  {id: 'lab', name: 'Lab server', base_url: 'http://127.0.0.1:11434/v1'},
  {id: 'empty', name: 'Unused account', base_url: 'https://example.test/v1'},
];

// Groups: local first, then cloud; a model entry with no model id is not offered.
const plan = P.groups(models, {providers});
const shape = plan.groups.map(group => [group.label, group.location, group.models.length]);
assert.equal(JSON.stringify(shape),
  JSON.stringify([['Lab server', 'Local', 1], ['OpenAI', 'Cloud', 2], ['Unused account', 'Cloud', 0]]),
  'every connected provider appears, Local before Cloud, in a stable order');
assert.equal(plan.groups[1].models[0].name, 'openai/gpt-5', 'models keep the order the server gave');
assert.equal(plan.groups[2].empty, true, 'a provider with no configured model is flagged, not dropped');
assert.equal(plan.favourites.length, 0, 'no favourites by default');

// Favourites move to their own list and never repeat inside a provider group.
const starred = P.groups(models, {providers, favourites: ['lab/qwen', 'gone/model']});
assert.equal(JSON.stringify(starred.favourites.map(entry => entry.name)), JSON.stringify(['lab/qwen']));
assert.equal(JSON.stringify(starred.groups.map(group => group.models.length)), JSON.stringify([2, 0]),
  'a favourite is not listed twice');

// A provider the configured models already cover does not appear twice even if
// its kind makes the location string differ from the entry's own local flag.
const mixed = P.groups([{...models[0], local: true}], {providers});
const labels = mixed.groups.map(group => group.label);
assert.equal(labels.filter(label => label === 'OpenAI').length, 1, 'a provider never appears twice');
assert.equal(JSON.stringify(labels), JSON.stringify(['Lab server', 'OpenAI', 'Unused account']));

// The active model must be one the server can send to.
assert.equal(P.active(models, 'openai/gpt-5').model, 'gpt-5');
assert.equal(P.active(models, 'placeholder'), null, 'an empty placeholder is not a model');
assert.equal(P.label(P.active(models, 'openai/gpt-5')), 'gpt-5');
assert.equal(P.label(null), 'No model');

// The welcome-screen choice survives a reload, and only while it is still valid.
const stored = P.serialize({model: 'openai/gpt-5', reasoning_effort: 'high'});
assert.equal(stored, '{"model":"openai/gpt-5","reasoning_effort":"high"}');
assert.equal(JSON.stringify(P.draft(stored, models)),
  JSON.stringify({model: 'openai/gpt-5', reasoning_effort: 'high'}));
assert.equal(JSON.stringify(P.draft(P.serialize({model: 'lab/qwen', reasoning_effort: 'high'}), models)),
  JSON.stringify({model: 'lab/qwen', reasoning_effort: null}),
  'an effort the model does not offer is dropped');
assert.equal(P.draft(P.serialize({model: 'gone/model'}), models), null, 'a removed model is not restored');
assert.equal(P.draft(P.serialize({model: 'placeholder'}), models), null);
assert.equal(P.draft('not json', models), null);
assert.equal(P.draft(null, models), null);
assert.equal(P.serialize(null), null);

// Local detection for providers the server has never seen configured models for.
assert.equal(JSON.stringify(P.groups([], {providers}).groups.map(group => group.location)),
  JSON.stringify(['Local', 'Cloud', 'Cloud']));
console.log('model-picker ok');
