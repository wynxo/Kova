const {contextBridge, ipcRenderer} = require('electron');
// The startup page (file:) gets only Retry. The local app page gets narrow,
// validated channels: native menu/tray actions in and its own aggregate state
// out (kovaDesktop), plus the Quick ask overlay's channels (kovaQuick).
if (location.protocol === 'file:') {
  contextBridge.exposeInMainWorld('kovaStartup', {retry: () => ipcRenderer.invoke('retry')});
} else {
  contextBridge.exposeInMainWorld('kovaDesktop', {
    onAction: callback => {
      if (typeof callback !== 'function') return;
      ipcRenderer.on('app-action', (_event, id) => callback(typeof id === 'string' ? id : ''));
    },
    reportState: state => ipcRenderer.send('app-state', typeof state === 'string' ? state : ''),
    reportChrome: colours => ipcRenderer.send('chrome-colours',
      colours && typeof colours === 'object' ? {
        background: typeof colours.background === 'string' ? colours.background : '',
        symbolColor: typeof colours.symbolColor === 'string' ? colours.symbolColor : '',
        height: Number.isInteger(colours.height) ? colours.height : 0,
      } : {}),
  });
  // Quick ask (#71). Strings are re-checked in the main process before anything
  // is trusted, so a compromised page can at most hide the overlay or open a
  // chat id the store would already accept.
  contextBridge.exposeInMainWorld('kovaQuick', {
    hide: () => ipcRenderer.send('quick-hide'),
    openChat: chatId => ipcRenderer.send('quick-open-chat', typeof chatId === 'string' ? chatId : ''),
    onSelection: callback => {
      if (typeof callback !== 'function') return;
      ipcRenderer.on('quick-selection', (_event, text) => callback(typeof text === 'string' ? text : ''));
    },
    onOpenChat: callback => {
      if (typeof callback !== 'function') return;
      ipcRenderer.on('open-chat', (_event, chatId) => callback(typeof chatId === 'string' ? chatId : ''));
    },
  });
}
