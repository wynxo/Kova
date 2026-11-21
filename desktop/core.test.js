const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const {settings, external, panelWindow, permissionAllowed, serverCommand, windowState, isAction, trayStatus, quickAsk, titleBar, titleBarColours, quickChatId, DEFAULT_QUICK_ASK} = require('./core');
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
    'toggle-sidebar', 'open-settings', 'toggle-theme', 'focus-composer', 'shortcuts', 'palette'])
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

test('the Quick ask accelerator is read from [desktop] and never throws', () => {
  assert.equal(quickAsk(''), DEFAULT_QUICK_ASK, 'no config means the default');
  assert.equal(quickAsk('port = 7700\n[memory]\npath = "/x"'), DEFAULT_QUICK_ASK,
    'a missing [desktop] section means the default');
  assert.equal(quickAsk('[desktop]\nquick_ask = "CommandOrControl+Space"'), 'CommandOrControl+Space');
  assert.equal(quickAsk('[desktop]\nquick_ask = "CommandOrControl+Shift+Space" # my pick'),
    'CommandOrControl+Shift+Space');
  assert.equal(quickAsk('[memory]\npath = "/x"\n[desktop]\nquick_ask = "Alt+Q"'), 'Alt+Q',
    'the section is found wherever it sits');
  assert.equal(quickAsk('[desktop]\n'), DEFAULT_QUICK_ASK, 'an empty value means the default');
});

test('the title bar is native unless [desktop] asks for the themed one', () => {
  assert.equal(titleBar(''), 'native');
  assert.equal(titleBar('port = 7700\n[memory]\npath = "/x"'), 'native');
  assert.equal(titleBar('[desktop]\nquick_ask = "Alt+Q"'), 'native');
  assert.equal(titleBar('[desktop]\ntitle_bar = "native"'), 'native');
  assert.equal(titleBar('[desktop]\ntitle_bar = "themed"'), 'themed');
  assert.equal(titleBar('[memory]\npath = "/x"\n[desktop]\ntitle_bar = "themed" # mine'), 'themed');
  // A typo or an empty value must never make the shell guess.
  for (const value of ['', 'Themed', 'custom', 'theme'])
    assert.equal(titleBar(`[desktop]\ntitle_bar = "${value}"`), 'native');
  assert.equal(titleBar('[desktop]\ntitle_bar = "themed "'), 'themed', 'surrounding space is trimmed');
});

test('the themed title bar accepts only theme-shaped colours and a sane height', () => {
  assert.deepEqual(titleBarColours({background: '#14121c', symbolColor: '#e8e6f0', height: 40}),
    {color: '#14121c', symbolColor: '#e8e6f0', height: 40});
  assert.deepEqual(titleBarColours({background: 'rgba(20, 18, 28, 0.9)', symbolColor: '#fff'}),
    {color: 'rgba(20, 18, 28, 0.9)', symbolColor: '#fff'}, 'rgba themes are allowed');
  for (const value of [null, {}, {background: 'red', symbolColor: '#fff'},
    {background: '#14121c'}, {background: 'url(x)', symbolColor: '#fff'},
    {background: '#14121c', symbolColor: 'var(--fg-default)'}, 'nonsense'])
    assert.equal(titleBarColours(value), null, JSON.stringify(value));
  assert.equal(titleBarColours({background: '#14121c', symbolColor: '#fff', height: 4}).height,
    undefined, 'an impossible height is dropped, not trusted');
  assert.equal(titleBarColours({background: '#14121c', symbolColor: '#fff', height: 999}).height,
    undefined);
});

test('only store-shaped chat ids may be asked to open in the main window', () => {
  for (const id of ['abc123', 'a1b2c3d4e5f6', 'incognito-abc123', 'with_underscore-2'])
    assert.equal(quickChatId(id), true);
  for (const id of ['', 'a b', '../etc/passwd', 'a'.repeat(65), null, 42, undefined, 'a\nb'])
    assert.equal(quickChatId(id), false);
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

test('only blank side-panel windows may open', () => {
  assert.ok(panelWindow('about:blank', 'kova-panel-1'));
  assert.ok(panelWindow('about:blank', 'kova-panel-42'));
  for (const [url, name] of [['about:blank', ''], ['about:blank', '_blank'], ['about:blank', 'kova-panel-'],
                             ['about:blank', 'kova-panel-1x'], ['https://example.com', 'kova-panel-1'],
                             ['http://127.0.0.1:7700/app', 'kova-panel-1'], ['about:blank#x', 'kova-panel-1']])
    assert.equal(panelWindow(url, name), false, `${url} ${name}`);
});
