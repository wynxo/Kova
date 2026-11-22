'use strict';
const fs = require('node:fs');
const path = require('node:path');
const os = require('node:os');

function settings(env = process.env, argv = process.argv) {
  const shortcutHome = argv.find(arg => arg.startsWith('--kova-home='));
  const home = env.KOVA_HOME || (shortcutHome && shortcutHome.slice('--kova-home='.length)) || path.join(os.homedir(), '.kova');
  let config = '';
  try { config = fs.readFileSync(path.join(home, 'config.toml'), 'utf8'); } catch (e) {
    if (e.code !== 'ENOENT') throw e;
  }
  // port is a top-level TOML integer, never a provider's port.
  const top = config.split(/^\s*\[/m)[0];
  const match = top.match(/^\s*port\s*=\s*(\d+)\s*(?:#.*)?$/m);
  const port = match ? Number(match[1]) : 7700;
  if (port < 1 || port > 65535) throw new Error('Invalid Kova port');
  return {home, origin: `http://127.0.0.1:${port}`, config};
}
function executable(name) {
  const dirs = (process.env.PATH || '').split(path.delimiter);
  dirs.push(path.join(os.homedir(), '.local', 'bin'), path.join(os.homedir(), '.cargo', 'bin'));
  for (const dir of dirs) {
    if (!path.isAbsolute(dir)) continue;
    const file = path.join(dir, name + (process.platform === 'win32' ? '.exe' : ''));
    try { fs.accessSync(file, fs.constants.X_OK); return file; } catch {}
  }
}
function serverCommand(source, home, find = executable) {
  const shortcutCli = process.argv.find(arg => arg.startsWith('--kova-cli='));
  const installed = process.env.KOVA_DESKTOP_CLI || shortcutCli?.slice('--kova-cli='.length);
  const cli = installed && path.isAbsolute(installed) ? installed : find('kova');
  if (cli) return {command: cli, args: ['serve'], cwd: home};
  const uv = find('uv');
  if (uv && fs.existsSync(path.join(source, 'pyproject.toml'))) {
    return {command: uv, args: ['run', 'kova', 'serve'], cwd: source};
  }
  throw new Error('Install the Kova Python backend using the installer at https://github.com/wynxo/Kova#install, then retry.');
}
function external(url) {
  try { return ['https:', 'http:', 'mailto:'].includes(new URL(url).protocol); } catch { return false; }
}
// The side panel can pop a tab out into its own window (#182). Only a blank,
// same-origin window the panel names `kova-panel-<n>` may open; the page then
// moves the tab into it. Everything else window.open asks for stays denied.
function panelWindow(url, frameName) {
  return url === 'about:blank' && /^kova-panel-\d{1,9}$/.test(String(frameName || ''));
}
// The only permission the local page may request is OS notifications.
function permissionAllowed(permission) {
  return permission === 'notifications';
}
// Native menu and tray actions. The id sent to the renderer over the
// 'app-action' channel is checked against this allowlist first, so an
// arbitrary string can never reach the page.
const APP_ACTION_IDS = new Set([
  'new-conversation', 'new-incognito', 'search-conversations',
  'toggle-sidebar', 'open-settings', 'toggle-theme', 'focus-composer',
  'shortcuts', 'palette',
]);
function isAction(id) {
  return typeof id === 'string' && APP_ACTION_IDS.has(id);
}
// Tray status: the single aggregate the page reports (idle / working /
// waiting) becomes the tooltip and the menu's status line.
const TRAY_STATES = {idle: 'Idle', working: 'Working', waiting: 'Waiting for approval'};
function trayStatus(name, state) {
  const label = Object.prototype.hasOwnProperty.call(TRAY_STATES, state)
    ? TRAY_STATES[state] : TRAY_STATES.idle;
  return {label, tooltip: `${name} · ${label}`};
}
const DEFAULT_WINDOW = {width: 1180, height: 800};
const MIN_WINDOW = {width: 760, height: 540};
// The Quick ask global shortcut (#71), read from config.toml. A missing or
// malformed value falls back to the default: globalShortcut.register throws on
// a bad accelerator, and the shell must never refuse to start over one.
const DEFAULT_QUICK_ASK = 'CommandOrControl+Shift+Space';
function sectionString(text, name, key) {
  const start = new RegExp(`^\\[${name}\\]\\s*$`, 'm').exec(String(text || ''));
  if (!start) return undefined;
  const rest = String(text || '').slice(start.index + start[0].length);
  const next = rest.match(/^\s*\[/m);
  const body = next ? rest.slice(0, next.index) : rest;
  const line = body.match(new RegExp(`^\\s*${key}\\s*=\\s*"([^"]*)"\\s*(?:#.*)?$`, 'm'));
  return line ? line[1] : undefined;
}
function quickAsk(configText) {
  const value = sectionString(configText, 'desktop', 'quick_ask');
  return typeof value === 'string' && value.trim() ? value : DEFAULT_QUICK_ASK;
}
// The window chrome is native by default. `[desktop] title_bar = "themed"` keeps
// the OS window controls but lets the page paint its themed title bar underneath
// them. Anything else, including a typo, stays native rather than guessing.
const TITLE_BAR_MODES = new Set(['native', 'themed']);
function titleBar(configText) {
  const value = sectionString(configText, 'desktop', 'title_bar');
  const mode = typeof value === 'string' ? value.trim() : '';
  return TITLE_BAR_MODES.has(mode) ? mode : 'native';
}
// The page reports the active theme's own colours, so the native window controls
// sit on the same surface as the title bar. Only plain CSS colours the theme
// engine already accepts, plus a small title-bar height, pass through.
const CSS_COLOUR = /^(#[0-9a-f]{3,8}|rgba?\([0-9.,%\s/]+\))$/i;
function titleBarColours(value) {
  if (!value || typeof value !== 'object') return null;
  const {background, symbolColor, height} = value;
  if (typeof background !== 'string' || !CSS_COLOUR.test(background.trim())) return null;
  if (typeof symbolColor !== 'string' || !CSS_COLOUR.test(symbolColor.trim())) return null;
  const clean = {color: background.trim(), symbolColor: symbolColor.trim()};
  if (Number.isInteger(height) && height >= 24 && height <= 80) clean.height = height;
  return clean;
}
// A chat id the page may ask the shell to open in the main window. The store
// itself only deletes ids matching this shape, so an arbitrary string never
// reaches the renderer's navigation.
function quickChatId(id) {
  return typeof id === 'string' && /^[A-Za-z0-9_-]{1,64}$/.test(id);
}
// Remember where the window was, but never trust it blindly: a saved position
// on a display that is no longer connected starts fresh instead of opening
// somewhere the reader cannot reach, and a silly size is clamped.
function windowState(saved, displays, fallback = DEFAULT_WINDOW) {
  const result = {bounds: {...fallback}, maximized: false};
  if (!saved || typeof saved !== 'object') return result;
  const {x, y, width, height} = saved;
  if (![x, y, width, height].every(Number.isFinite)) return result;
  const areas = (displays || []).map(display => display && (display.workArea || display))
    .filter(area => area && [area.x, area.y, area.width, area.height].every(Number.isFinite));
  const visible = !areas.length || areas.some(area =>
    x < area.x + area.width && y < area.y + area.height &&
    x + width > area.x && y + height > area.y);
  if (!visible) return result;
  result.bounds = {
    x, y,
    width: Math.min(Math.max(width, MIN_WINDOW.width), 3840),
    height: Math.min(Math.max(height, MIN_WINDOW.height), 2160),
  };
  result.maximized = saved.maximized === true;
  return result;
}
module.exports = {settings, executable, external, panelWindow, permissionAllowed, serverCommand, windowState, isAction, trayStatus, quickAsk, titleBar, titleBarColours, quickChatId, DEFAULT_QUICK_ASK};
