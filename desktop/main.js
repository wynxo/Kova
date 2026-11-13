'use strict';
const {app, BrowserWindow, Menu, Tray, shell, ipcMain, screen} = require('electron');
const {spawn} = require('node:child_process');
const fs = require('node:fs');
const path = require('node:path');
const {settings, executable, external} = require('./core');
let win, tray, child, quitting = false, starting = false, origin, home;
const icon = path.join(__dirname, 'assets/icon.png');
if (!app.requestSingleInstanceLock()) app.quit();
else {
  app.on('second-instance', () => show());
  app.whenReady().then(create).catch(e => { console.error(e.message); app.quit(); });
}
function show() { if (win) { if (win.isMinimized()) win.restore(); win.show(); win.focus(); } }
function stateFile() { return path.join(home, 'desktop-window.json'); }
function saveBounds() {
  if (!win || win.isDestroyed() || win.isMinimized() || win.isMaximized()) return;
  try { fs.writeFileSync(stateFile(), JSON.stringify(win.getBounds()), {mode: 0o600}); } catch {}
}
async function create() {
  ({home, origin} = settings());
  fs.mkdirSync(home, {recursive: true, mode: 0o700});
  // Isolate Electron caches/cookies as well as Python data under KOVA_HOME.
  app.setPath('userData', path.join(home, 'desktop'));
  let bounds = {width: 1180, height: 800};
  try {
    const saved = JSON.parse(fs.readFileSync(stateFile(), 'utf8'));
    if (['x','y','width','height'].every(k => Number.isFinite(saved[k])) &&
        screen.getAllDisplays().some(d => saved.x < d.workArea.x + d.workArea.width &&
          saved.y < d.workArea.y + d.workArea.height && saved.x + saved.width > d.workArea.x &&
          saved.y + saved.height > d.workArea.y)) {
      bounds = {...saved, width: Math.max(760, Math.min(saved.width, 3840)),
        height: Math.max(540, Math.min(saved.height, 2160))};
    }
  } catch {}
  win = new BrowserWindow({...bounds, minWidth: 760, minHeight: 540,
    backgroundColor: '#131419', icon, autoHideMenuBar: true, show: false,
    webPreferences: {contextIsolation: true, nodeIntegration: false, sandbox: true,
      preload: path.join(__dirname, 'preload.js')}});
  Menu.setApplicationMenu(null);
  win.once('ready-to-show', show);
  win.on('close', e => { saveBounds(); if (!quitting && tray) { e.preventDefault(); win.hide(); } });
  win.webContents.setWindowOpenHandler(({url}) => {
    if (external(url) && new URL(url).origin !== origin) shell.openExternal(url);
    return {action: 'deny'};
  });
  win.webContents.on('will-navigate', (event, url) => {
    if (new URL(url).origin !== origin) { event.preventDefault(); if (external(url)) shell.openExternal(url); }
  });
  win.webContents.session.setPermissionRequestHandler((_wc, _permission, callback) => callback(false));
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
    tray = new Tray(icon); tray.setToolTip('Kova');
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
  const cli = executable('kova');
  const uv = executable('uv');
  if (!cli && !uv) throw new Error('Install Kova or uv first');
  const cwd = path.resolve(__dirname, '..');
  const log = fs.openSync(path.join(home, 'server.log'), 'a', 0o600);
  try {
    child = spawn(cli || uv, cli ? ['serve'] : ['run', 'kova', 'serve'], {
      cwd: !cli && fs.existsSync(path.join(cwd, 'pyproject.toml')) ? cwd : home,
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
