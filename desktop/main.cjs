const { app, BrowserWindow, Menu, ipcMain, shell, session, dialog } = require('electron');
const { execFile } = require('node:child_process');
const fs = require('node:fs');
const path = require('node:path');
const { pathToFileURL } = require('node:url');
const { readHost, probe } = require('./host.cjs');

app.setName('YunOS');
app.setAppUserModelId('local.yunos.desktop');
const hasLock = app.requestSingleInstanceLock();
if (!hasLock) app.quit();
else {
  let launcher, workspace, config, pending;
  let state = { phase: 'starting', message: '正在检查本机主机…' };
  const root = app.isPackaged
    ? JSON.parse(fs.readFileSync(path.join(process.resourcesPath, 'host-location.json'), 'utf8')).projectRoot
    : path.resolve(__dirname, '..');
  const launcherUrl = pathToFileURL(path.join(__dirname, 'launcher.html')).href;
  const logPath = path.join(app.getPath('userData'), 'launcher.log');
  fs.mkdirSync(path.dirname(logPath), { recursive: true });
  if (fs.existsSync(logPath) && fs.statSync(logPath).size > 2 * 1024 * 1024) {
    fs.copyFileSync(logPath, `${logPath}.previous`);
    fs.writeFileSync(logPath, '');
  }
  function log(text) { fs.appendFileSync(logPath, `${new Date().toISOString()} ${text}\n`); }
  function update(phase, message) {
    state = { phase, message };
    if (launcher && !launcher.isDestroyed()) launcher.webContents.send('launcher:state', state);
    log(`${phase}: ${message}`);
  }
  function showLauncher() {
    if (!launcher || launcher.isDestroyed()) {
      launcher = new BrowserWindow({
        title: 'YunOS 启动器', width: 640, height: 460, minWidth: 480, minHeight: 420,
        backgroundColor: '#f4f2ed', icon: path.join(__dirname, 'yunos.ico'),
        autoHideMenuBar: true,
        webPreferences: { preload: path.join(__dirname, 'preload.cjs'), contextIsolation: true, nodeIntegration: false, sandbox: true },
      });
      launcher.webContents.setWindowOpenHandler(() => ({ action: 'deny' }));
      launcher.webContents.on('will-navigate', (event, url) => { if (url !== launcherUrl) event.preventDefault(); });
      launcher.loadFile(path.join(__dirname, 'launcher.html'));
      launcher.on('closed', () => { launcher = null; });
    }
    launcher.show(); launcher.focus();
  }
  async function ensureService() {
    if (pending) return pending;
    pending = (async () => {
      update('starting', '正在检查本机主机…');
      config = readHost(root);
      if (!await probe(config)) {
        update('starting', '正在启动 YunOS…');
        await new Promise((resolve, reject) => {
          execFile(path.join(process.env.SystemRoot, 'System32', 'WindowsPowerShell', 'v1.0', 'powershell.exe'),
            ['-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', path.join(root, 'scripts', 'start_yunos_primary_host.ps1')],
            { cwd: root, windowsHide: true, timeout: 45000, maxBuffer: 1024 * 1024 },
            (error, stdout, stderr) => {
              if (error) { log(`${error.message}\n${stderr}`); reject(new Error('本机服务启动失败，可重试或查看日志。')); }
              else resolve();
            });
        });
      }
      if (!await probe(config)) throw new Error('主机尚未就绪，请重试。');
      update('ready', '本机主机已就绪');
      return config;
    })().catch(error => { update('error', error.message); throw error; }).finally(() => { pending = null; });
    return pending;
  }
  async function openWorkspace() {
    try {
      await ensureService();
      if (workspace && !workspace.isDestroyed()) {
        if (workspace.isMinimized()) workspace.restore();
        workspace.show(); workspace.focus();
        launcher?.hide(); return;
      }
      workspace = new BrowserWindow({
        title: 'YunOS', width: 1320, height: 900, minWidth: 800, minHeight: 600,
        backgroundColor: '#faf9f6', show: false, icon: path.join(__dirname, 'yunos.ico'),
        webPreferences: { contextIsolation: true, nodeIntegration: false, sandbox: true, partition: 'persist:yunos-workspace' },
      });
      workspace.webContents.setWindowOpenHandler(() => ({ action: 'deny' }));
      workspace.webContents.on('will-navigate', (event, url) => {
        if (new URL(url).origin !== config.origin) event.preventDefault();
      });
      workspace.webContents.on('page-title-updated', event => { event.preventDefault(); workspace.setTitle('YunOS'); });
      workspace.on('closed', () => {
        workspace = null;
        if (launcher && !launcher.isVisible()) launcher.close();
      });
      await workspace.loadURL(config.origin);
      workspace.show(); workspace.focus(); launcher?.hide();
      log('workspace opened');
    } catch (error) {
      if (workspace && !workspace.isDestroyed()) workspace.destroy();
      workspace = null; showLauncher(); update('error', error.message);
    }
  }
  function validateSender(event) {
    if (!launcher || event.sender !== launcher.webContents || event.senderFrame.url !== launcherUrl) {
      throw new Error('Invalid launcher sender');
    }
  }
  app.on('second-instance', (_event, argv) => {
    if (argv.includes('--launcher')) showLauncher();
    else openWorkspace();
  });
  app.on('window-all-closed', () => app.quit());
  app.whenReady().then(async () => {
    for (const ses of [session.defaultSession, session.fromPartition('persist:yunos-workspace')]) {
      ses.setPermissionRequestHandler((_wc, _permission, callback) => callback(false));
      ses.setPermissionCheckHandler(() => false);
      ses.on('will-download', event => event.preventDefault());
    }
    Menu.setApplicationMenu(Menu.buildFromTemplate([
      { label: 'YunOS', submenu: [
        { label: '打开工作区', click: openWorkspace },
        { label: '启动器', click: showLauncher },
        { type: 'separator' },
        { label: '退出', role: 'quit' },
      ] },
      { label: '编辑', submenu: [{ role: 'undo' }, { role: 'redo' }, { type: 'separator' }, { role: 'cut' }, { role: 'copy' }, { role: 'paste' }, { role: 'selectAll' }] },
      { label: '视图', submenu: [{ role: 'reload' }, { role: 'resetZoom' }, { role: 'zoomIn' }, { role: 'zoomOut' }, { role: 'togglefullscreen' }] },
    ]));
    ipcMain.handle('launcher:get-state', event => { validateSender(event); return state; });
    ipcMain.handle('launcher:open', event => { validateSender(event); openWorkspace(); });
    ipcMain.handle('launcher:retry', async event => { validateSender(event); try { await ensureService(); } catch {} return state; });
    ipcMain.handle('launcher:logs', async event => { validateSender(event); return shell.openPath(logPath); });
    showLauncher();
    if (process.argv.includes('--launcher')) { try { await ensureService(); } catch {} }
    else await openWorkspace();
  }).catch(error => { log(error.stack); dialog.showErrorBox('YunOS 启动失败', '请重新启动 YunOS。'); app.quit(); });
}
