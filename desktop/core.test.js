const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const {settings, external, permissionAllowed, serverCommand} = require('./core');
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
test('the renderer may request notifications and nothing else', () => {
  assert.equal(permissionAllowed('notifications'), true);
  for (const permission of ['geolocation', 'media', 'camera', 'microphone',
    'clipboard-read', 'clipboard-write', 'openExternal', 'unknown'])
    assert.equal(permissionAllowed(permission), false);
});

test('menu launcher can retain a custom home without a terminal environment', () => {
  const home = fs.mkdtempSync(path.join(os.tmpdir(), 'kova-shortcut-'));
  try {
    assert.equal(settings({}, ['--kova-home=' + home]).home, home);
    assert.equal(settings({KOVA_HOME: home}, ['--kova-home=ignored']).home, home);
  } finally { fs.rmSync(home, {recursive:true, force:true}); }
});

test('packaged apps start the installed backend, never uv run in an empty home', () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'kova-backend-'));
  try {
    const find = name => name === 'kova' ? 'installed-kova' : 'installed-uv';
    assert.deepEqual(serverCommand(root, root, find), {
      command: 'installed-kova', args: ['serve'], cwd: root,
    });
    const uvOnly = name => name === 'uv' ? 'installed-uv' : undefined;
    assert.throws(() => serverCommand(root, root, uvOnly), /Install the Kova Python backend/);
    fs.writeFileSync(path.join(root, 'pyproject.toml'), '[project]');
    assert.deepEqual(serverCommand(root, root, uvOnly), {
      command: 'installed-uv', args: ['run', 'kova', 'serve'], cwd: root,
    });
  } finally { fs.rmSync(root, {recursive:true, force:true}); }
});
