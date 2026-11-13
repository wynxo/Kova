'use strict';
const fs = require('node:fs');
const path = require('node:path');
const os = require('node:os');

function settings(env = process.env) {
  const home = env.KOVA_HOME || path.join(os.homedir(), '.kova');
  let config = '';
  try { config = fs.readFileSync(path.join(home, 'config.toml'), 'utf8'); } catch (e) {
    if (e.code !== 'ENOENT') throw e;
  }
  // port is a top-level TOML integer, never a provider's port.
  const top = config.split(/^\s*\[/m)[0];
  const match = top.match(/^\s*port\s*=\s*(\d+)\s*(?:#.*)?$/m);
  const port = match ? Number(match[1]) : 7700;
  if (port < 1 || port > 65535) throw new Error('Invalid Kova port');
  return {home, origin: `http://127.0.0.1:${port}`};
}
function executable(name) {
  const dirs = (process.env.PATH || '').split(path.delimiter);
  dirs.push(path.join(os.homedir(), '.local', 'bin'), path.join(os.homedir(), '.cargo', 'bin'));
  for (const dir of dirs) {
    const file = path.join(dir, name + (process.platform === 'win32' ? '.exe' : ''));
    try { fs.accessSync(file, fs.constants.X_OK); return file; } catch {}
  }
}
function external(url) {
  try { return ['https:', 'http:', 'mailto:'].includes(new URL(url).protocol); } catch { return false; }
}
module.exports = {settings, executable, external};
