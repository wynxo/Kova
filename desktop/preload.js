const {contextBridge, ipcRenderer} = require('electron');
// No bridge is exposed to the chat application.
if (location.protocol === 'file:') {
  contextBridge.exposeInMainWorld('kovaStartup', {retry: () => ipcRenderer.invoke('retry')});
}
