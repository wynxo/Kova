'use strict';
const {app, BrowserWindow, Menu, Tray, shell, ipcMain, screen} = require('electron');
const {spawn} = require('node:child_process');
const fs = require('node:fs');
const path = require('node:path');
const {settings, serverCommand, external, permissionAllowed, windowState} = require('./core');
let win, tray, child, quitting = false, starting = false, origin, home;
const icon = path.join(__dirname, 'assets/icon.png');
const trayIcon = path.join(__dirname, 'assets/icon-64.png');
if (!app.requestSingleInstanceLock()) app.quit();
else {
  app.on('second-instance', () => show());
  app.whenReady().then(create).catch(e => { console.error(e.message); app.quit(); });
}
function show() { if (win) { if (win.isMinimized()) win.restore(); win.show(); win.focus(); } }
function stateFile() { return path.join(home, 'desktop-window.json'); }
function savedWindow() {
  try { return JSON.parse(fs.readFileSync(stateFile(), 'utf8')); } catch { return null; }
}
let saveTimer = null;
function saveBounds() {
  clearTimeout(saveTimer); saveTimer = null;
  if (!win || win.isDestroyed()) return;
  try {
    // The normal bounds are the ones to remember, even while maximized.
    const bounds = typeof win.getNormalBounds === 'function' ? win.getNormalBounds() : win.getBounds();
    fs.writeFileSync(stateFile(), JSON.stringify({...bounds, maximized: win.isMaximized()}), {mode: 0o600});
  } catch {}
}
// Moving or resizing is saved as it happens, so even a crash or a kill keeps
// the window where the reader put it, once it has stopped moving for a moment.
function scheduleBounds() { clearTimeout(saveTimer); saveTimer = setTimeout(saveBounds, 400); }
async function create() {
  ({home, origin} = settings());
  fs.mkdirSync(home, {recursive: true, mode: 0o700});
  // Isolate Electron caches/cookies as well as Python data under KOVA_HOME.
  app.setPath('userData', path.join(home, 'desktop'));
  const restored = windowState(savedWindow(), screen.getAllDisplays());
  win = new BrowserWindow({...restored.bounds, minWidth: 760, minHeight: 540,
    backgroundColor: '#0d0d10', icon, title: 'Kova', autoHideMenuBar: true, show: false,
    webPreferences: {contextIsolation: true, nodeIntegration: false, sandbox: true,
      preload: path.join(__dirname, 'preload.js')}});
  Menu.setApplicationMenu(null);
  win.once('ready-to-show', () => { if (restored.maximized) win.maximize(); show(); });
  win.on('resize', scheduleBounds);
  win.on('move', scheduleBounds);
  win.webContents.on('page-title-updated', (_event, title) => {
    if (tray && title) {
      tray.setToolTip(title);
      tray.setContextMenu(Menu.buildFromTemplate([{label: 'Open ' + title, click: show},
        {type: 'separator'}, {label: 'Quit', click: () => app.quit()}]));
    }
  });
  // The page publishes its theme as <meta name="theme-color">; no bridge needed.
  win.webContents.on('did-change-theme-color', (_event, color) => {
    if (/^#[0-9a-f]{6}$/i.test(color || '')) win.setBackgroundColor(color);
  });
  win.on('close', e => { saveBounds(); if (!quitting && tray) { e.preventDefault(); win.hide(); } });
  win.webContents.setWindowOpenHandler(({url}) => {
    if (external(url) && new URL(url).origin !== origin) shell.openExternal(url);
    return {action: 'deny'};
  });
  win.webContents.on('will-navigate', (event, url) => {
    if (new URL(url).origin !== origin) { event.preventDefault(); if (external(url)) shell.openExternal(url); }
  });
  win.webContents.session.setPermissionRequestHandler((_wc, permission, callback) => callback(permissionAllowed(permission)));
  win.webContents.session.webRequest.onBeforeRequest((details, callback) => {
    const url = new URL(details.url);
    callback({cancel: !['file:', 'devtools:'].includes(url.protocol) && url.origin !== origin});
  });
  win.webContents.on('will-attach-webview', e => e.preventDefault());
  win.webContents.on('before-input-event', (e, input) => {
    const mod = input.control || input.meta;
    if (mod && input.key.toLowerCase() === 'q') { e.preventDefault(); app.quit(); }
    if (mod && input.key.toLowerCase() === 'r') { e.preventDefault(); start(); }
    if (mod && input.key.toLowerCase() === 'w') { e.preventDefault(); win.close(); }
    if (input.key === 'F11') win.setFullScreen(!win.isFullScreen());
  });
  try {
    tray = new Tray(trayIcon); tray.setToolTip('Kova');
    tray.setContextMenu(Menu.buildFromTemplate([{label: 'Open Kova', click: show},
      {type: 'separator'}, {label: 'Quit', click: () => app.quit()}]));
    tray.on('click', show);
  } catch (e) { console.error('Tray unavailable:', e.message); }
  ipcMain.handle('retry', event => {
    if (event.sender === win.webContents && event.senderFrame.url.startsWith('file:')) start();
  });
  await start();
}
async function status(error = false) {
  await win.loadFile(path.join(__dirname, 'status.html'), {query: {state: error ? 'error' : 'starting', log: path.join(home, 'server.log')}});
}
async function ticket() {
  const token = fs.readFileSync(path.join(home, 'token'), 'utf8').trim();
  const response = await fetch(origin + '/app/session', {method: 'POST',
    headers: {Authorization: 'Bearer ' + token}, signal: AbortSignal.timeout(2500)});
  if (!response.ok) throw new Error('Server authentication failed');
  const data = await response.json();
  if (typeof data.ticket !== 'string' || !data.ticket) throw new Error('Invalid session response');
  return data.ticket;
}
function launchServer() {
  if (child) return;
  const backend = serverCommand(path.resolve(__dirname, '..'), home);
  const log = fs.openSync(path.join(home, 'server.log'), 'a', 0o600);
  try {
    child = spawn(backend.command, backend.args, {
      cwd: backend.cwd,
      env: {...process.env, KOVA_HOME: home}, stdio: ['ignore', log, log], windowsHide: true});
    child.on('error', () => { child = null; });
    child.on('exit', () => { child = null; });
  } finally { fs.closeSync(log); }
}
async function start() {
  if (starting) return;
  starting = true;
  try {
    await status();
    let value;
    // Authentication, not model health, determines whether this is our server.
    try { value = await ticket(); } catch { launchServer(); }
    const deadline = Date.now() + 30000;
    while (!value && Date.now() < deadline && !quitting) {
      await new Promise(resolve => setTimeout(resolve, 400));
      try { value = await ticket(); } catch {}
    }
    if (!value) throw new Error('Kova did not become ready');
    if (!quitting) {
      try {
        const token = fs.readFileSync(path.join(home, 'token'), 'utf8').trim();
        const response = await fetch(origin + '/persona', {headers: {Authorization: 'Bearer ' + token}});
        if (response.ok) {
          const persona = await response.json();
          if (typeof persona.name === 'string' && persona.name) {
            win.setTitle(persona.name);
            if (tray) { tray.setToolTip(persona.name); tray.setContextMenu(Menu.buildFromTemplate([
              {label: 'Open ' + persona.name, click: show}, {type: 'separator'},
              {label: 'Quit', click: () => app.quit()}])); }
          }
        }
      } catch (e) { console.error('Could not load assistant identity:', e.message); }
    }
    if (!quitting) await win.loadURL(origin + '/app#ticket=' + encodeURIComponent(value));
  } catch (e) {
    console.error(e.message);
    if (!quitting) await status(true);
  } finally { starting = false; }
}
process.on('SIGINT', () => app.quit());
process.on('SIGTERM', () => app.quit());
app.on('activate', show);
app.on('window-all-closed', () => { if (!tray) app.quit(); });
app.on('before-quit', () => {
  quitting = true; saveBounds();
  // Never terminate a server that was already running.
  if (child) child.kill('SIGTERM');
});
