import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import { promises as fs } from 'node:fs';
import { createRequire } from 'node:module';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { test } from 'node:test';

const require = createRequire(import.meta.url);
const { sourceLaunch } = require('../dist/main/installed-host.js');
const desktop = fileURLToPath(new URL('..', import.meta.url));

// Opt in because this exercises real Windows foreground activation.
test('Windows source launches show the splash after closing and reopening', {
  skip: process.platform !== 'win32' || process.env.SHUFFLE_DESKTOP_WINDOW_TEST !== '1',
  timeout: 60000,
}, async t => {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), 'shuffle-window-test-'));
  t.after(() => fs.rm(root, { recursive: true, force: true }));
  const entry = path.join(root, 'main.cjs');
  const report = path.join(root, 'report.json');
  await fs.writeFile(entry, `
    const { app, ipcMain } = require('electron');
    const fs = require('node:fs');
    const { MainWindow } = require(${JSON.stringify(path.join(desktop, 'dist/main/window.js'))});
    app.disableHardwareAcceleration();
    app.setPath('userData', ${JSON.stringify(path.join(root, 'profile'))});
    app.setAppPath(${JSON.stringify(desktop)});
    ipcMain.on('desktop:info', event => { event.returnValue = { platform: process.platform, version: 'test' }; });
    ipcMain.handle('desktop:worker-state', () => ({ phase: 'starting', postgres: { phase: 'starting' } }));
    app.whenReady().then(async () => {
      const main = new MainWindow(app.getPath('userData'));
      await main.showPage('status');
      await new Promise(resolve => setTimeout(resolve, 500));
      const win = main.browserWindow;
      const splash = { visible: win.isVisible(), focused: win.isFocused(), url: win.webContents.getURL() };
      win.hide();
      main.focus();
      const restored = { visible: win.isVisible(), focused: win.isFocused() };
      fs.writeFileSync(${JSON.stringify(report)}, JSON.stringify({ splash, restored }));
      win.close();
      app.quit();
    }).catch(error => { console.error(error); app.exit(1); });
  `);
  const launch = sourceLaunch({
    electron: require('electron'), desktopDir: entry, revision: 'a'.repeat(40),
    node: process.execPath, python: process.execPath,
  }, root);
  // The fixture is an entry script, while production uses an application directory.
  launch.options.cwd = root;
  // Existing installers retain their bundled launcher after a source update.
  // Exercise both that legacy hidden launch and the corrected launch options.
  for (const windowsHide of [launch.options.windowsHide, true]) {
    for (let attempt = 0; attempt < 2; attempt++) {
      await fs.rm(report, { force: true });
      await new Promise((resolve, reject) => {
        const child = spawn(launch.executable, launch.args, { ...launch.options, windowsHide, stdio: 'pipe', timeout: 20000 });
        let stderr = '';
        child.stdout.resume();
        child.stderr.on('data', chunk => { stderr += chunk; });
        child.once('error', reject);
        child.once('exit', code => code === 0 ? resolve() : reject(Error(`Electron exited ${code}: ${stderr}`)));
      });
      const result = JSON.parse(await fs.readFile(report, 'utf8'));
      const context = `windowsHide=${windowsHide}, launch ${attempt + 1}`;
      assert.equal(result.splash.visible, true, `${context}: splash must be visible without a tray click`);
      assert.equal(result.splash.focused, true, `${context}: splash must have focus`);
      assert.match(result.splash.url, /worker\.html\?mode=status$/);
      assert.deepEqual(result.restored, { visible: true, focused: true });
    }
  }
});
