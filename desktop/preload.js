const {contextBridge, ipcRenderer} = require('electron');
// The startup page (file:) gets only Retry. The local app page gets two
// narrow channels: native menu/tray actions in, its own aggregate state out.
if (location.protocol === 'file:') {
  contextBridge.exposeInMainWorld('kovaStartup', {retry: () => ipcRenderer.invoke('retry')});
} else {
  contextBridge.exposeInMainWorld('kovaDesktop', {
    onAction: callback => {
      if (typeof callback !== 'function') return;
      ipcRenderer.on('app-action', (_event, id) => callback(typeof id === 'string' ? id : ''));
    },
    reportState: state => ipcRenderer.send('app-state', typeof state === 'string' ? state : ''),
  });
}
