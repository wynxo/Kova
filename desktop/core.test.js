const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const {settings, external, permissionAllowed, serverCommand, windowState, isAction, trayStatus} = require('./core');
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

test('the app-action allowlist admits only the palette actions', () => {
  for (const id of ['new-conversation', 'new-incognito', 'search-conversations',
    'toggle-sidebar', 'open-settings', 'toggle-theme', 'focus-composer', 'shortcuts'])
    assert.equal(isAction(id), true);
  for (const id of ['', 'new-chat', 'quit', 'settings', 'open-settings ', '../open-settings',
    'open-settings\n', null, 42, undefined])
    assert.equal(isAction(id), false);
});

test('tray status turns a page state into a tooltip and menu label', () => {
  assert.deepEqual(trayStatus('Kova', 'idle'), {label: 'Idle', tooltip: 'Kova · Idle'});
  assert.deepEqual(trayStatus('Kova', 'working'), {label: 'Working', tooltip: 'Kova · Working'});
  assert.deepEqual(trayStatus('Alex', 'waiting'),
    {label: 'Waiting for approval', tooltip: 'Alex · Waiting for approval'});
  // Anything the page cannot have sent falls back to idle rather than leaking it.
  assert.deepEqual(trayStatus('Kova', 'blocked-on-approval'), {label: 'Idle', tooltip: 'Kova · Idle'});
  assert.deepEqual(trayStatus('Kova', 'nonsense'), {label: 'Idle', tooltip: 'Kova · Idle'});
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


test('window memory keeps a sane place, size and maximized state', () => {
  const displays = [{workArea: {x: 0, y: 0, width: 1920, height: 1080}}];
  assert.deepEqual(windowState(null, displays),
    {bounds: {width: 1180, height: 800}, maximized: false});
  assert.deepEqual(windowState({x: 120, y: 80, width: 1280, height: 900, maximized: true}, displays),
    {bounds: {x: 120, y: 80, width: 1280, height: 900}, maximized: true});
  assert.deepEqual(windowState({x: 120, y: 80, width: 200, height: 200}, displays).bounds,
    {x: 120, y: 80, width: 760, height: 540}, 'a tiny window is clamped');
  // A monitor that is no longer connected starts fresh instead of off-screen.
  assert.deepEqual(windowState({x: 5000, y: 5000, width: 900, height: 700}, displays),
    {bounds: {width: 1180, height: 800}, maximized: false});
  assert.deepEqual(windowState({x: 0, y: 0, width: 800, height: 600}).bounds.x, 0,
    'with no display information the saved place is trusted');
  assert.deepEqual(windowState('nonsense', displays).bounds, {width: 1180, height: 800});
  assert.deepEqual(windowState({x: 0, y: 0, width: '900', height: 700}, displays).bounds,
    {width: 1180, height: 800}, 'strings are not coordinates');
});
