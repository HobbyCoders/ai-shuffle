import { app } from 'electron';
import { promises as fs } from 'node:fs';
import path from 'node:path';
import { SourceUpdates } from './source-updates';
import { saveInstalledHost, launchInstalledSource, installedLaunchArgs } from './installed-host';

let installed: { updates: SourceUpdates; executable: string } | null = null;
export const installedContext = () => installed;

/** Never download at startup: run the bundled app offline until the user updates. */
export async function startInstalled(): Promise<void> {
  // Take the same lock as the running app before reading first-boot state.
  // Opening an existing shortcut must focus that app, not mark its boot failed.
  if (!app.requestSingleInstanceLock()) { app.quit(); return; }
  const userData = app.getPath('userData');
  const root = path.join(userData, 'updates');
  const baseline = JSON.parse(await fs.readFile(path.join(process.resourcesPath, 'worker/desktop-build.json'), 'utf8'));
  if (!/^[a-f0-9]{40}$/.test(baseline.revision) || !Number.isSafeInteger(baseline.sequence) || baseline.sequence <= 0) throw Error('Invalid installed desktop revision');
  const updates = new SourceUpdates({ root, baseline,
    publicKey: await fs.readFile(path.join(app.getAppPath(), 'build/application-update-key.pem'), 'utf8'),
    bootstrapNode: path.join(process.resourcesPath, 'worker/node/node.exe'),
  });
  await updates.load();
  await updates.useNewerBaseline();
  await saveInstalledHost(root, { executable: process.execPath, userData, name: app.getName() });
  if (updates.hasSourceState) {
    const selected = await updates.select();
    app.releaseSingleInstanceLock();
    // Launch the GUI executable directly; no console shell is needed.
    await launchInstalledSource(selected, root, installedLaunchArgs(process.argv.slice(1), app.commandLine.getSwitchValue('remote-debugging-port')));
    app.quit();
    return;
  }
  installed = { updates, executable: process.execPath };
  require('./index');
}
