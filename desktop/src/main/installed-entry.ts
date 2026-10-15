import { app, dialog, shell } from 'electron';
import { promises as fs } from 'node:fs';
import path from 'node:path';
import { SourceUpdates, type RestoreDatabaseFactory } from './source-updates';
import { PostgresSupervisor, resolvePostgresBinaries } from './postgres';
import { quiesceRecordedWorker } from './worker';
import { saveInstalledHost, launchInstalledSource, installedLaunchArgs, installedExecutable, requireInstalledElectron, InstallerRequired } from './installed-host';
import { countLauncherStart, launcherTrial, parseLauncher, restoreLauncher, LAUNCHER_FILE } from './launcher-swap';

let installed: { updates: SourceUpdates; executable: string; root: string; installersOnly?: boolean } | null = null;
export const installedContext = () => installed;

/** Never download at startup: run the bundled app offline until the user updates. */
export async function startInstalled(): Promise<void> {
  // Take the same lock as the running app before reading first-boot state.
  // Opening an existing shortcut must focus that app, not mark its boot failed.
  if (!app.requestSingleInstanceLock()) { app.quit(); return; }
  const userData = app.getPath('userData');
  const root = path.join(userData, 'updates');
  let baseline;
  try {
    baseline = JSON.parse(await fs.readFile(path.join(process.resourcesPath, 'worker/desktop-build.json'), 'utf8'));
  } catch (error) {
    // Older Linux installers have no trusted baseline. Keep their bundled
    // worker and signed installer offers; never invent a source-update baseline.
    if (process.platform !== 'linux' || (error as NodeJS.ErrnoException).code !== 'ENOENT') throw error;
    app.releaseSingleInstanceLock();
    require('./index');
    return;
  }
  if (!/^[a-f0-9]{40}$/.test(baseline.revision) || !Number.isSafeInteger(baseline.sequence) || baseline.sequence <= 0) throw Error('Invalid installed desktop revision');
  // Installers built before the launcher stamp (#1821 D5) have none: version 0.
  let launcher = baseline.launcher === undefined ? 0 : baseline.launcher;
  if (!Number.isSafeInteger(launcher) || launcher < 0) throw Error('Invalid installed desktop launcher version');
  // Launcher swaps are Windows-only; Linux keeps its installed Electron in process.
  if (process.platform === 'win32') {
    // A launcher the updated app swapped in carries its own version; the installer's stamp is older.
    try { launcher = parseLauncher(await fs.readFile(path.join(app.getAppPath(), LAUNCHER_FILE), 'utf8')).version; }
    catch (error) { if ((error as NodeJS.ErrnoException).code !== 'ENOENT') throw error; }
  }
  // A swapped-in launcher on trial (desktop/LAUNCHER.md): if its first start never
  // reached the updated app, put the previous launcher back before anything else.
  const trial = process.platform === 'win32' ? launcherTrial(root, launcher) : 'none';
  if (trial === 'restore') { restoreLauncher(root, process.execPath); app.quit(); return; }
  try { await startWith(root, userData, baseline, launcher, trial === 'trial'); }
  catch (error) {
    if (trial === 'trial') try { restoreLauncher(root, process.execPath); } catch { /* the error below is the one to show */ }
    throw error;
  }
}

async function startWith(root: string, userData: string, baseline: { revision: string; sequence: number }, launcher: number, trial: boolean): Promise<void> {
  const updates = new SourceUpdates({ root, baseline: { revision: baseline.revision, sequence: baseline.sequence },
    publicKey: await fs.readFile(path.join(app.getAppPath(), 'build/application-update-key.pem'), 'utf8'),
    bootstrapNode: path.join(process.resourcesPath, process.platform === 'win32' ? 'worker/node/node.exe' : 'worker/node/bin/node'),
  });
  await updates.load();
  const database: RestoreDatabaseFactory = async previous => {
    // Never use the failed update's native runtime to open the previous cluster.
    const resources = previous === baseline.revision ? process.resourcesPath : (await updates.application(previous)).resources;
    const binaries = resolvePostgresBinaries({ packaged: true, resourcesPath: resources });
    if ('error' in binaries) throw new Error(binaries.error);
    const postgres = new PostgresSupervisor({ binaries, root: path.join(userData, 'postgres'), logDir: path.join(userData, 'logs'),
      credentials: { load: () => null, save: async () => { throw new Error('Rollback must never change database credentials'); } } });
    const copy = postgres.updateRestorePoint();
    return {
      validate: () => copy.validate(),
      snapshot: async () => { await quiesceRecordedWorker(path.join(userData, 'worker')); await copy.snapshot(); },
      restore: async () => { await quiesceRecordedWorker(path.join(userData, 'worker')); await copy.restore(); },
      discard: () => copy.discard(),
    };
  };
  try { await updates.recoverRollback(database); }
  catch (error) {
    // Keep the journal and migration fence. The owner can retry restoration, never a source-only rollback.
    await app.whenReady();
    await dialog.showMessageBox({ type: 'warning', title: 'AI Shuffle database recovery', message: 'Could not restore the previous version', detail: String(error), buttons: ['OK'] });
    if (!await retryBlocked(updates, database)) { app.quit(); return; }
  }
  await updates.useNewerBaseline();
  if (updates.recoveryRequired || (updates.recoveredState && updates.blocked)) {
    if (!await offerInstaller(updates)) { app.quit(); return; }
  }
  try {
    const executable = installedExecutable();
    await saveInstalledHost(root, { executable, userData, name: app.getName() });
    // Staged builds survive cancelInstall and restarts. Recheck their compatibility
    // before choosing the updater mode, even if no source revision is active.
    let installersOnly = false;
    if (process.platform === 'linux' && updates.pendingVersion) {
      const revision = updates.pendingVersion;
      try { await requireInstalledElectron(await updates.application(revision), process.versions.electron); }
      catch (error) {
        if (error instanceof InstallerRequired) {
          updates.stageDownloads();
          await updates.cancelInstall();
          if (updates.hasMigrationRisk) throw error;
          installersOnly = true;
        } else if (!await updates.discardStaged(revision)) throw error;
        // An incomplete, never-activated download can be rebuilt by the normal
        // updater. Active/failed/pending code still fails closed, never rolls back.
      }
    }
    // useNewerBaseline clears source state only when the installed baseline is
    // newer than every build that may have migrated this database. Otherwise a
    // blocked first boot must fail closed, never downgrade to bundled code.
    if (updates.blocked && !await retryBlocked(updates, database)) { app.quit(); return; }
    await updates.prepareRollback(database);
    const notice = await updates.rollbackNotice();
    if (notice) {
      await app.whenReady();
      await dialog.showMessageBox({ type: 'warning', title: 'AI Shuffle update', message: notice, buttons: ['OK'] });
      await fs.rm(path.join(root, 'rollback-notice.json'), { force: true });
    }
    if (updates.hasSourceState) {
      if (process.platform === 'linux') {
        let startSource: typeof import('./managed-entry').startManagedSource | undefined;
        const preflight = async (application: Awaited<ReturnType<SourceUpdates['application']>>) => {
          await requireInstalledElectron(application, process.versions.electron);
          startSource = require(path.join(application.desktopDir, 'dist/main/managed-entry.js')).startManagedSource;
          if (typeof startSource !== 'function') throw new InstallerRequired('Install the latest AI Shuffle installer to run this update safely.');
        };
        let selected;
        try { selected = await updates.select(preflight); }
        catch (error) {
          if (!(error instanceof InstallerRequired)) throw error;
          await updates.cancelPreparedRollback(database);
          // This attempt ran no source, but a previous retry may already have
          // migrated the database. Cancellation must preserve that recovery fence.
          // Retain the staged build, but offer installers rather than retry it.
          updates.stageDownloads();
          await updates.cancelInstall();
          if (updates.hasMigrationRisk) throw error;
          if (!updates.hasSourceState) {
            app.releaseSingleInstanceLock();
            require('./index');
            return;
          }
          // Never downgrade an already-migrated source install to bundled code.
          selected = await updates.select(preflight);
          installersOnly = true;
        }
        await startSource!(root, selected.revision, selected.node, installersOnly);
        return;
      }
      const selected = await updates.select();
      app.releaseSingleInstanceLock();
      if (trial) countLauncherStart(root);
      // Launch the GUI executable directly; no console shell is needed.
      await launchInstalledSource(selected, root, installedLaunchArgs(process.argv.slice(1), app.commandLine.getSwitchValue('remote-debugging-port')), launcher);
      app.quit();
      return;
    }
    installed = { updates, executable, root, installersOnly };
    require('./index');
  } catch (error) {
    if (!updates.recoveredState) throw error;
    await offerInstaller();
    app.quit();
  }
}

/**
 * A failed first boot blocks every build. Offer to run that build again rather
 * than only an error: nothing can check for updates while the app cannot open.
 * False when the user quits or no safe build remains; then offer an installer.
 */
async function retryBlocked(updates: SourceUpdates, database: RestoreDatabaseFactory): Promise<boolean> {
  await app.whenReady();
  const previous = await updates.rollbackVersion();
  if (previous) {
    const { response } = await dialog.showMessageBox({
      type: 'warning', title: 'AI Shuffle', message: 'The last update did not finish starting',
      detail: 'Restore the verified database copy and run the previous version. If restoration fails, no worker will start and the restore point will be kept.',
      buttons: [`Go back to ${previous}`, 'Quit'], defaultId: 0, cancelId: 1, noLink: true,
    });
    if (response !== 0) return false;
    try { return await updates.recoverRollback(database, true); }
    catch (error) { dialog.showErrorBox('AI Shuffle could not restore the database', String(error)); return false; }
  }
  const { response } = await dialog.showMessageBox({
    type: 'warning', title: 'AI Shuffle', message: 'The last update did not finish starting',
    detail: 'AI Shuffle will not go back to an older version, because the update may already have changed your data. ' +
      'Try the update again, or quit and install the latest AI Shuffle installer. Your files and data have been kept.',
    buttons: ['Try Again', 'Quit'], defaultId: 0, cancelId: 1, noLink: true,
  });
  if (response !== 0) return false;
  if (!await updates.retryFailed()) { await offerInstaller(); return false; }
  return true;
}

/** Recovery must offer an installer without starting a worker on uncertain data. */
async function offerInstaller(updates?: SourceUpdates): Promise<boolean> {
  await app.whenReady();
  const { response } = await dialog.showMessageBox({
    type: 'warning', title: 'AI Shuffle needs an installer', message: 'Your update state could not be recovered safely',
    detail: 'The update state and your data have been kept. AI Shuffle will not start older code on a database that may have been migrated. Get the latest installer to recover. This offer will remain available on the next launch.' +
      (updates?.canConfirmBaselineRecovery ? '\nIf you have installed the latest installer, you can explicitly choose to use its bundled code. The last database version cannot be proven; choosing an older installer could damage your data.' : ''),
    buttons: ['Get Latest Installer', 'Quit', ...(updates?.canConfirmBaselineRecovery ? ['Use This Installer'] : [])], defaultId: 0, cancelId: 1, noLink: true,
  });
  if (response === 2 && updates?.canConfirmBaselineRecovery) {
    await updates.useNewerBaseline(true);
    return !updates.recoveryRequired && !updates.blocked;
  }
  if (response !== 0) return false;
  try {
    const { InstallerUpdates, installerPlatform } = await import('./installer-updates');
    const platform = installerPlatform();
    if (!platform) { await shell.openExternal('https://hobbycoders.com'); return false; }
    const installers = new InstallerUpdates({ platform, arch: process.arch, currentVersion: '0.0.0',
      publicKey: await fs.readFile(path.join(app.getAppPath(), 'build/application-update-key.pem'), 'utf8'), directory: app.getPath('downloads') });
    if ((await installers.check()).kind !== 'available') throw new Error('No signed installer is available for this platform yet.');
    shell.showItemInFolder((await installers.download()).file);
  } catch (error) {
    await dialog.showMessageBox({ type: 'warning', title: 'AI Shuffle installer', message: 'The installer could not be downloaded',
      detail: `${String(error)} Your data is unchanged. Try again on the next launch, or get the latest installer from hobbycoders.com.`, buttons: ['Quit'] });
  }
  return false;
}
