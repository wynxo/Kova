'use strict';

// Run with Electron, not Node. Observe the production entry point without
// changing how it starts or requiring a model or a Python server.
const {app} = require('electron');
const {once} = require('node:events');
const fs = require('node:fs');
const http = require('node:http');
const os = require('node:os');
const path = require('node:path');

// The production entry point calls app.quit() if initialization fails.
let loaded = false;
const themed = process.env.KOVA_DESKTOP_TITLEBAR === 'themed';
const home = fs.mkdtempSync(path.join(os.tmpdir(), 'kova-startup-'));
const server = http.createServer((request, response) => {
  const path = new URL(request.url, 'http://127.0.0.1').pathname;
  if (request.method === 'POST' && path === '/app/session') {
    response.end(JSON.stringify({ticket: 'smoke-test'}));
  } else if (path === '/app') {
    response.setHeader('Content-Type', 'text/html');
    response.end('<title>Kova startup smoke</title>');
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
      if (contents.getTitle() !== 'Kova startup smoke') return;
      if (app.getPath('userData') !== path.join(home, 'desktop-data')) return;
      clearTimeout(timeout);
      loaded = true;
      console.log(`Desktop loaded the ${themed ? 'themed' : 'native'} chat page`);
      app.quit();
    });
  });

  require('../src/kova/desktop/main.js');
}

start().catch(error => {
  console.error(error);
  app.quit();
});
