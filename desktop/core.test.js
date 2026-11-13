const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const {settings, external} = require('./core');
test('isolated home and top-level port only', () => {
  const home = fs.mkdtempSync(path.join(os.tmpdir(), 'kova-electron-test-'));
  try {
    assert.equal(settings({KOVA_HOME: home}).origin, 'http://127.0.0.1:7700');
    fs.writeFileSync(path.join(home, 'config.toml'), 'port = 7891 # isolated\n[provider]\nport = 1234');
    assert.equal(settings({KOVA_HOME: home}).origin, 'http://127.0.0.1:7891');
    fs.writeFileSync(path.join(home, 'config.toml'), 'port = 99999');
    assert.throws(() => settings({KOVA_HOME: home}));
  } finally { fs.rmSync(home, {recursive:true, force:true}); }
});
test('external links cannot launch arbitrary protocols', () => {
  assert.ok(external('https://example.com'));
  for (const url of ['file:///etc/passwd', 'javascript:alert(1)', 'bad', 'shell:foo'])
    assert.equal(external(url), false);
});
