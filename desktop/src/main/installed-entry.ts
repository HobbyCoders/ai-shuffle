import { app, dialog } from 'electron';
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
  // useNewerBaseline clears source state only when the installed baseline is
  // newer than every build that may have migrated this database. Otherwise a
  // blocked first boot must fail closed, never downgrade to bundled code.
  if (updates.hasSourceState) {
    if (updates.blocked && !await retryBlocked(updates)) { app.quit(); return; }
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

/**
 * A failed first boot blocks every build. Offer to run that build again rather
 * than only an error: nothing can check for updates while the app cannot open.
 * False when the user quits; a build that cannot run again is left to select().
 */
async function retryBlocked(updates: SourceUpdates): Promise<boolean> {
  await app.whenReady();
  const { response } = await dialog.showMessageBox({
    type: 'warning', title: 'AI Shuffle', message: 'The last update did not finish starting',
    detail: 'AI Shuffle will not go back to an older version, because the update may already have changed your data. ' +
      'Try the update again, or quit and install the latest AI Shuffle installer. Your files and data have been kept.',
    buttons: ['Try Again', 'Quit'], defaultId: 0, cancelId: 1, noLink: true,
  });
  if (response !== 0) return false;
  await updates.retryFailed();
  return true;
}
