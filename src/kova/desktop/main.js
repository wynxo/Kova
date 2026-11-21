'use strict';
const {app, BrowserWindow, Menu, Tray, shell, ipcMain, screen, globalShortcut, clipboard} = require('electron');
const {spawn} = require('node:child_process');
const fs = require('node:fs');
const path = require('node:path');
const {settings, serverCommand, external, panelWindow, permissionAllowed, windowState, isAction, trayStatus, quickAsk, titleBar, titleBarColours, quickChatId} = require('./core');
let win, tray, child, quick, quitting = false, starting = false, origin, home;
let appName = 'Kova', trayState = 'idle', chrome = 'native';
const icon = path.join(__dirname, 'assets/icon.png');
const trayIcon = path.join(__dirname, 'assets/icon-64.png');
// Tray state -> asset. The tray icon is OS chrome, so the status dot is baked
// into the mark at build time (scripts/build_icons.py:tray + DOT_STATUS): idle
// shows the mark alone, working a white dot, waiting amber.
const TRAY_ASSETS = {
  idle: trayIcon,
  working: path.join(__dirname, 'assets/tray-working.png'),
  waiting: path.join(__dirname, 'assets/tray-waiting.png'),
};
async function initialize() {
  const desktopSettings = settings();
  ({home, origin} = desktopSettings);
  // Set the profile before Electron takes its instance lock or opens Chromium.
  const profile = path.join(home, 'desktop-data');
  fs.mkdirSync(profile, {recursive: true, mode: 0o700});
  app.setPath('userData', profile);
  if (!app.requestSingleInstanceLock()) {
    app.quit();
    return;
  }
  app.on('second-instance', () => show());
  await app.whenReady();
  await create(desktopSettings.config);
}
initialize().catch(e => { console.error(e.message); app.quit(); });
function show() { if (win) { if (win.isMinimized()) win.restore(); win.show(); win.focus(); } }
// Quick ask (#71): a small, always-on-top window over /app?quick=1, toggled by a
// global shortcut. The selection chip reads the clipboard in this process and
// hands it to the page over a narrow channel.
const QUICK_SELECTION_LIMIT = 8192;
function readSelection() {
  try {
    const text = clipboard.readText();
    return typeof text === 'string' && text.trim() && text.length <= QUICK_SELECTION_LIMIT ? text : '';
  } catch { return ''; }
}
function hideQuick() {
  if (quick && !quick.isDestroyed()) quick.hide();
}
async function ensureQuick() {
  if (quick && !quick.isDestroyed()) return quick;
  const placement = screen.getPrimaryDisplay().workArea;
  quick = new BrowserWindow({
    width: 720, height: 220, minWidth: 360, minHeight: 160,
    x: placement.x + Math.round((placement.width - 720) / 2),
    y: placement.y + 64,
    frame: false, alwaysOnTop: true, skipTaskbar: true, show: false, resizable: true,
    backgroundColor: '#0d0d10', icon, title: 'Quick ask',
    webPreferences: {contextIsolation: true, nodeIntegration: false, sandbox: true,
      preload: path.join(__dirname, 'preload.js')}});
  quick.setAlwaysOnTop(true, 'pop-up-menu');
  quick.webContents.setWindowOpenHandler(({url}) => {
    if (external(url) && new URL(url).origin !== origin) shell.openExternal(url);
    return {action: 'deny'};
  });
  quick.webContents.on('will-navigate', (event, url) => {
    if (new URL(url).origin !== origin) { event.preventDefault(); if (external(url)) shell.openExternal(url); }
  });
  quick.webContents.session.setPermissionRequestHandler((_wc, permission, callback) => callback(permissionAllowed(permission)));
  quick.webContents.session.webRequest.onBeforeRequest((details, callback) => {
    const url = new URL(details.url);
    callback({cancel: !['file:', 'devtools:'].includes(url.protocol) && url.origin !== origin});
  });
  quick.webContents.on('will-attach-webview', e => e.preventDefault());
  // It should get out of the way; the turn keeps running server-side.
  quick.on('blur', () => hideQuick());
  const value = await ticket();
  await quick.loadURL(origin + '/app?quick=1#ticket=' + encodeURIComponent(value));
  return quick;
}
async function showQuick() {
  try {
    const window = await ensureQuick();
    window.webContents.send('quick-selection', readSelection());
    if (window.isMinimized()) window.restore();
    window.show(); window.focus();
  } catch (e) {
    console.error('Quick ask unavailable:', e.message);
    show();  // fall back to the main window
  }
}
function toggleQuick() {
  if (quick && quick.isVisible()) hideQuick();
  else showQuick();
}
// Native menu and tray items send an allowlisted action id to the page; the
// page maps each id to the same handler the palette uses. Show and Quit stay
// in this process.
function sendAction(id) {
  if (!win || win.isDestroyed() || !isAction(id)) return;
  show();
  win.webContents.send('app-action', id);
}
function refreshTray() {
  if (!tray) return;
  tray.setImage(TRAY_ASSETS[trayState] || trayIcon);
  const status = trayStatus(appName, trayState);
  tray.setToolTip(status.tooltip);
  tray.setContextMenu(Menu.buildFromTemplate([
    {label: status.label, enabled: false},
    {type: 'separator'},
    {label: 'Show Kova', click: show},
    {label: 'New chat', click: () => sendAction('new-conversation')},
    {label: 'Quick ask', click: toggleQuick},
    {label: 'Command palette', click: () => sendAction('palette')},
    {type: 'separator'},
    {label: 'Quit', click: () => app.quit()},
  ]));
}
function buildAppMenu() {
  const action = (label, id) => ({label, click: () => sendAction(id)});
  const mac = process.platform === 'darwin';
  const template = [];
  if (mac) {
    template.push({label: app.name, submenu: [
      {role: 'about'}, {type: 'separator'},
      action('Settings…', 'open-settings'), {type: 'separator'},
      {role: 'hide'}, {role: 'hideOthers'}, {role: 'unhide'},
      {type: 'separator'}, {role: 'quit'},
    ]});
  }
  template.push(
    {label: 'File', submenu: [
      action('New chat', 'new-conversation'),
      action('New incognito chat', 'new-incognito'),
      {label: 'Quick ask', click: toggleQuick},
      ...(mac ? [] : [{type: 'separator'}, action('Settings…', 'open-settings'),
        {type: 'separator'}, {role: 'quit', label: 'Quit Kova'}]),
    ]},
    {role: 'editMenu', label: 'Edit'},
    {label: 'View', submenu: [
      action('Command palette…', 'palette'),
      action('Search conversations', 'search-conversations'),
      action('Toggle sidebar', 'toggle-sidebar'),
      action('Focus message box', 'focus-composer'),
      action('Toggle theme', 'toggle-theme'),
    ]},
    {role: 'windowMenu', label: 'Window'},
    {role: 'help', submenu: [action('Keyboard shortcuts', 'shortcuts')]},
  );
  return Menu.buildFromTemplate(template);
}
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
async function create(config) {
  chrome = titleBar(config);
  const restored = windowState(savedWindow(), screen.getAllDisplays());
  // Native is the default. The themed bar keeps the OS window controls: macOS
  // keeps its traffic lights with `hiddenInset`, Windows/Linux get Electron's
  // native overlay. The page only paints the strip behind them.
  const bar = chrome !== 'themed' ? {} : process.platform === 'darwin'
    ? {titleBarStyle: 'hiddenInset'}
    : {titleBarStyle: 'hidden', titleBarOverlay: true};
  win = new BrowserWindow({...restored.bounds, ...bar, minWidth: 760, minHeight: 540,
    backgroundColor: '#0d0d10', icon, title: 'Kova', autoHideMenuBar: true, show: false,
    webPreferences: {contextIsolation: true, nodeIntegration: false, sandbox: true,
      preload: path.join(__dirname, 'preload.js')}});
  Menu.setApplicationMenu(buildAppMenu());
  win.once('ready-to-show', () => { if (restored.maximized) win.maximize(); show(); });
  win.on('resize', scheduleBounds);
  win.on('move', scheduleBounds);
  // The page publishes its theme as <meta name="theme-color">; no bridge needed.
  win.webContents.on('did-change-theme-color', (_event, color) => {
    if (/^#[0-9a-f]{6}$/i.test(color || '')) win.setBackgroundColor(color);
  });
  win.on('close', e => { saveBounds(); if (!quitting && tray) { e.preventDefault(); win.hide(); } });
  win.webContents.setWindowOpenHandler(({url, frameName}) => {
    // A side-panel tab popped out into its own window: a blank page the app fills.
    if (panelWindow(url, frameName)) {
      return {action: 'allow', overrideBrowserWindowOptions: {minWidth: 320, minHeight: 240, icon,
        autoHideMenuBar: true, backgroundColor: win.getBackgroundColor?.() || '#0d0d10'}};
    }
    if (external(url) && new URL(url).origin !== origin) shell.openExternal(url);
    return {action: 'deny'};
  });
  // A popped-out panel window may not open more windows or navigate anywhere.
  win.webContents.on('did-create-window', child => {
    child.setMenu?.(null);
    child.webContents.setWindowOpenHandler(({url}) => {
      if (external(url) && new URL(url).origin !== origin) shell.openExternal(url);
      return {action: 'deny'};
    });
    child.webContents.on('will-navigate', event => event.preventDefault());
    child.webContents.on('will-attach-webview', event => event.preventDefault());
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
    tray = new Tray(trayIcon); tray.on('click', show); refreshTray();
  } catch (e) { console.error('Tray unavailable:', e.message); }
  ipcMain.handle('retry', event => {
    if (event.sender === win.webContents && event.senderFrame.url.startsWith('file:')) start();
  });
  // The app page reports its aggregate state (idle / working / waiting) for the
  // tray; anything else is ignored so the renderer cannot drive arbitrary text.
  ipcMain.on('app-state', (event, state) => {
    if (event.sender !== win.webContents) return;
    trayState = ['idle', 'working', 'waiting'].includes(state) ? state : 'idle';
    refreshTray();
  });
  // The themed title bar reports the active theme's own background, text and
  // height, so the native window controls match it without hard-coded colours.
  ipcMain.on('chrome-colours', (event, colours) => {
    if (event.sender !== win.webContents || chrome !== 'themed') return;
    const overlay = titleBarColours(colours);
    if (!overlay) return;
    try {
      if (process.platform !== 'darwin') win.setTitleBarOverlay(overlay);
    } catch (e) { console.error('Could not theme the title bar:', e.message); }
  });
  // Quick ask (#71): the overlay hides itself, or asks the main window to open
  // the chat it just created. Sender and chat id are both checked first.
  ipcMain.on('quick-hide', event => {
    if (event.sender === quick?.webContents) hideQuick();
  });
  ipcMain.on('quick-open-chat', (event, chatId) => {
    if (event.sender !== quick?.webContents || !quickChatId(chatId)) return;
    hideQuick();
    show();
    if (win && !win.isDestroyed()) win.webContents.send('open-chat', chatId);
  });
  await start();
  // The global shortcut is a convenience; a platform that refuses it (Wayland
  // often does) still has the tray's Quick ask item and the composer focus key.
  try {
    const accelerator = quickAsk(config);
    if (!globalShortcut.register(accelerator, toggleQuick)) {
      console.error(`Could not register the Quick ask shortcut ${accelerator}; another app may own it.`);
    }
  } catch (e) { console.error('Could not register the Quick ask shortcut:', e.message); }
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
  const backend = serverCommand(path.resolve(__dirname, '../../..'), home);
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
            appName = persona.name;
            refreshTray();
          }
        }
      } catch (e) { console.error('Could not load assistant identity:', e.message); }
    }
    if (!quitting) await win.loadURL(origin + '/app' + (chrome === 'themed' ? '?titlebar=themed' : '') + '#ticket=' + encodeURIComponent(value));
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
  globalShortcut.unregisterAll();
  // Never terminate a server that was already running.
  if (child) child.kill('SIGTERM');
});
