'use strict';
// No renderer-supplied options or paths: only the owned top-level chat page
// can open a native directory dialog. Cancel never changes the chat's folder.
function registerFolderPicker({ipcMain, dialog, getWindow, getOrigin}) {
  ipcMain.handle('pick-folder', async event => {
    const win = getWindow();
    if (!win || event.sender !== win.webContents || event.senderFrame !== win.webContents.mainFrame) return null;
    let url;
    try { url = new URL(event.senderFrame.url); } catch { return null; }
    if (url.origin !== getOrigin() || !['/app', '/app/'].includes(url.pathname)) return null;
    const result = await dialog.showOpenDialog(win, {title: 'Choose working folder', properties: ['openDirectory']});
    return result.canceled ? null : result.filePaths[0] || null;
  });
}
module.exports = {registerFolderPicker};
