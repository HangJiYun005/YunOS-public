const { contextBridge, ipcRenderer } = require('electron');
contextBridge.exposeInMainWorld('launcher', {
  state: () => ipcRenderer.invoke('launcher:get-state'),
  open: () => ipcRenderer.invoke('launcher:open'),
  retry: () => ipcRenderer.invoke('launcher:retry'),
  logs: () => ipcRenderer.invoke('launcher:logs'),
  onState: callback => ipcRenderer.on('launcher:state', (_event, value) => callback(value)),
});
