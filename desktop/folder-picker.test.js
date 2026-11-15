const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const {registerFolderPicker} = require('./folder-picker');

test('folder dialog is restricted to the owned top-level app frame', async () => {
  let handler, calls = 0, result = {canceled: false, filePaths: ['/project']};
  const frame = {url: 'http://127.0.0.1:7891/app'};
  const win = {webContents: {mainFrame: frame}};
  registerFolderPicker({ipcMain: {handle: (_name, fn) => { handler = fn; }},
    dialog: {showOpenDialog: async (owner, options) => {
      assert.equal(owner, win); assert.deepEqual(options.properties, ['openDirectory']);
      calls++; return result;
    }}, getWindow: () => win, getOrigin: () => 'http://127.0.0.1:7891'});
  const valid = {sender: win.webContents, senderFrame: frame};
  assert.equal(await handler(valid), '/project');
  result = {canceled: true, filePaths: []};
  assert.equal(await handler(valid), null);
  for (const event of [{...valid, sender: {}}, {...valid, senderFrame: {...frame}},
    {...valid, senderFrame: {url: 'https://example.com/app'}}]) {
    assert.equal(await handler(event), null);
  }
  for (const url of ['https://example.com/app', 'file:///app', 'http://127.0.0.1:7891/health', 'not a URL']) {
    frame.url = url; assert.equal(await handler(valid), null);
  }
  assert.equal(calls, 2, 'rejected senders never open a dialog');
});

test('preload exposes only the folder action to the app and retry to startup', () => {
  const source = fs.readFileSync(require.resolve('./preload.js'), 'utf8');
  for (const [protocol, pathname, expected] of [['http:', '/app', 'kovaDesktop'], ['http:', '/app/', 'kovaDesktop'],
    ['file:', '/status.html', 'kovaStartup'], ['https:', '/other', null]]) {
    const exposed = {}, invoked = [];
    vm.runInNewContext(source, {location: {protocol, pathname}, require: () => ({
      contextBridge: {exposeInMainWorld: (name, value) => { exposed[name] = value; }},
      ipcRenderer: {invoke: (...args) => { invoked.push(args); }}
    })});
    assert.deepEqual(Object.keys(exposed), expected ? [expected] : []);
    if (expected === 'kovaDesktop') {
      assert.deepEqual(Object.keys(exposed.kovaDesktop), ['pickFolder']);
      exposed.kovaDesktop.pickFolder(); assert.deepEqual(invoked, [['pick-folder']]);
    }
  }
});
