const { packager } = require('@electron/packager');
const fs = require('node:fs');
const path = require('node:path');
const crypto = require('node:crypto');
const { execFileSync } = require('node:child_process');
(async () => {
  const version = require('./node_modules/electron/package.json').version;
  const name = `electron-v${version}-win32-x64.zip`;
  const runtimeDir = path.resolve(__dirname, '../output/desktop-runtime');
  fs.mkdirSync(runtimeDir, { recursive: true });
  const archive = path.join(runtimeDir, name);
  if (!fs.existsSync(archive)) {
    execFileSync('curl.exe', ['-L', '--fail', '--retry', '2', '--max-time', '180', '-o', archive,
      `https://github.com/electron/electron/releases/download/v${version}/${name}`], { windowsHide: true, stdio: 'inherit' });
  }
  const expected = require('./node_modules/electron/checksums.json')[name];
  const actual = crypto.createHash('sha256').update(fs.readFileSync(archive)).digest('hex');
  if (!expected || actual !== expected) throw new Error('Electron runtime checksum mismatch');
  const [output] = await packager({
    dir: __dirname, name: 'YunOS', platform: 'win32', arch: 'x64',
    out: path.resolve(__dirname, '../output/desktop-build'), overwrite: true,
    electronZipDir: path.resolve(__dirname, '../output/desktop-runtime'),
    icon: path.join(__dirname, 'yunos.ico'), asar: true,
    ignore: [/^\/test($|\/)/, /^\/package\.cjs$/, /^\/install\.ps1$/],
    win32metadata: { CompanyName: 'YunOS', FileDescription: 'YunOS Desktop', ProductName: 'YunOS' },
  });
  fs.writeFileSync(path.join(output, 'resources', 'host-location.json'), JSON.stringify({ projectRoot: path.resolve(__dirname, '..') }, null, 2));
  console.log(output);
})().catch(error => { console.error(error); process.exitCode = 1; });
