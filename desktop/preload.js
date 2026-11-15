const {contextBridge, ipcRenderer} = require('electron');
// Expose only explicit native actions; never credentials or arbitrary IPC.
if (location.protocol === 'file:') {
  contextBridge.exposeInMainWorld('kovaStartup', {retry: () => ipcRenderer.invoke('retry')});
}
else if (['http:', 'https:'].includes(location.protocol) && ['/app', '/app/'].includes(location.pathname)) {
  contextBridge.exposeInMainWorld('kovaDesktop', {pickFolder: () => ipcRenderer.invoke('pick-folder')});
}
