'use strict';

// Run with Electron, not Node. Observe the production entry point without
// changing how it starts or requiring a model or a Python server.
const {app, BrowserWindow, Menu, Tray} = require('electron');
const assert = require('node:assert/strict');
const {once} = require('node:events');
const fs = require('node:fs');
const http = require('node:http');
const os = require('node:os');
const path = require('node:path');

// The production entry point calls app.quit() if initialization fails.
let loaded = false;
const themed = process.env.KOVA_DESKTOP_TITLEBAR === 'themed';
const home = fs.mkdtempSync(path.join(os.tmpdir(), 'kova-startup-'));
let trayMenu, overlay;
const setContextMenu = Tray.prototype.setContextMenu;
Tray.prototype.setContextMenu = function (menu) {
  setContextMenu.call(this, menu);
  trayMenu = menu;
};
const setTitleBarOverlay = BrowserWindow.prototype.setTitleBarOverlay;
BrowserWindow.prototype.setTitleBarOverlay = function (options) {
  setTitleBarOverlay.call(this, options);
  overlay = options;
};
const server = http.createServer((request, response) => {
  const path = new URL(request.url, 'http://127.0.0.1').pathname;
  if (request.method === 'POST' && path === '/app/session') {
    response.end(JSON.stringify({ticket: 'smoke-test'}));
  } else if (path === '/app') {
    response.setHeader('Content-Type', 'text/html');
    response.end('<title>Kova startup smoke</title><main>Demo chat</main>');
  } else {
    response.writeHead(404).end();
  }
});

app.on('will-quit', event => {
  event.preventDefault();
  server.close();
  fs.rmSync(home, {recursive: true, force: true});
  app.exit(loaded ? 0 : 1);
});

const timeout = setTimeout(() => {
  console.error('Desktop did not load the chat page');
  app.quit();
}, 15000);

async function start() {
  server.listen(0, '127.0.0.1');
  await once(server, 'listening');
  const origin = `http://127.0.0.1:${server.address().port}`;
  process.env.KOVA_HOME = home;
  fs.writeFileSync(path.join(home, 'config.toml'),
    `port = ${server.address().port}\n[desktop]\ntitle_bar = "${themed ? 'themed' : 'native'}"\n`);
  fs.writeFileSync(path.join(home, 'token'), 'smoke-test');
  const expected = origin + '/app' + (themed ? '?titlebar=themed' : '') + '#ticket=smoke-test';

  app.on('web-contents-created', (_event, contents) => {
    contents.on('did-finish-load', () => {
      if (contents.getURL() !== expected) return;
      verify(contents).catch(error => {
        console.error(error);
        app.quit();
      });
    });
  });

  require('../src/kova/desktop/main.js');
}

async function verify(contents) {
  assert.equal(contents.getTitle(), 'Kova startup smoke');
  assert.equal(app.getPath('userData'), path.join(home, 'desktop-data'));
  const nativeMenu = Menu.getApplicationMenu();
  const item = (menu, label) => {
    const result = menu?.items.find(entry => entry.label === label);
    assert.ok(result, `Missing native menu item: ${label}`);
    return result;
  };
  const file = item(nativeMenu, 'File').submenu;
  const view = item(nativeMenu, 'View').submenu;
  assert.ok(item(file, 'New incognito chat'));
  assert.ok(item(file, 'Quick ask'));
  assert.ok(item(view, 'Focus message box'));
  assert.ok(item(trayMenu, 'Quick ask'));
  assert.ok(item(trayMenu, 'Idle'));

  await contents.executeJavaScript(`
    window.received = [];
    kovaDesktop.onAction(action => window.received.push(action));
    kovaDesktop.reportState('working');
    kovaDesktop.reportState('waiting');
    kovaDesktop.reportChrome({background: '#242424', symbolColor: '#eeeeee', height: 40});
  `);
  item(view, 'Command palette…').click();
  item(trayMenu, 'New chat').click();
  // The clicks reach the page over IPC, so the actions arrive a moment later.
  let received = [];
  for (const deadline = Date.now() + 5000; Date.now() < deadline;) {
    received = await contents.executeJavaScript('window.received');
    if (received.length >= 2) break;
    await new Promise(resolve => setTimeout(resolve, 50));
  }
  assert.deepEqual(received, ['palette', 'new-conversation']);
  assert.ok(item(trayMenu, 'Waiting for approval'));
  if (process.platform !== 'darwin') {
    assert.deepEqual(overlay, themed ? {color: '#242424', symbolColor: '#eeeeee', height: 40} : undefined);
    const nativeControls = await contents.executeJavaScript(`
      ({visible: navigator.windowControlsOverlay.visible,
        right: navigator.windowControlsOverlay.getTitlebarAreaRect().right, width: innerWidth})
    `);
    assert.equal(nativeControls.visible, themed, 'native overlay only appears in themed mode');
    if (themed) assert.ok(nativeControls.right < nativeControls.width, 'the overlay reserves space for native controls');
  }
  clearTimeout(timeout);
  loaded = true;
  console.log(`Desktop verified the ${themed ? 'themed' : 'native'} title bar, menu and tray`);
  app.quit();
}

start().catch(error => {
  console.error(error);
  app.quit();
});
