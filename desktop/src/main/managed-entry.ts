/** Entry for locally built desktop code; development/legacy builds keep their old entry. */
import { app, dialog } from 'electron';
import { promises as fs } from 'node:fs';
import path from 'node:path';
import { SourceUpdates } from './source-updates';
import { setManagedContext } from './managed-context';
import { readProfile, localFeed } from './source-profile';
import { readInstalledHostSync } from './installed-host';
import { buildEnvironment } from './source-build';

async function start(): Promise<void> {
  // Packaged Windows/Linux launches must select pending updates even when restarted
  // by an older source-managed app that passes its own revision in the environment.
  if (app.isPackaged && (process.platform === 'win32' || process.platform === 'linux')) {
    await require('./installed-entry').startInstalled();
    return;
  }
  const root = process.env.SHUFFLE_SOURCE_ROOT;
  if (!root) {
    require('./index');
    return;
  }
  await startManagedSource(root, process.env.SHUFFLE_SOURCE_REVISION ?? '', process.env.SHUFFLE_SOURCE_NODE ?? '');
}

/** Linux's installed Electron delegates here without launching a user-owned Electron binary. */
export async function startManagedSource(root: string, revision: string, bootstrapNode: string, installersOnly = false): Promise<void> {
  if (!/^[a-f0-9]{40}$/.test(revision) || !path.isAbsolute(root) || !path.isAbsolute(bootstrapNode)) throw new Error('Invalid source-managed launch');
  const profile = readProfile(root);
  // Set Chromium's existing profile paths before Electron's ready event.
  const host = profile ? null : readInstalledHostSync(root);
  if (host) {
    app.setName(host.name);
    app.setPath('userData', host.userData);
    app.setPath('sessionData', host.userData);
  }
  const publicKey = profile?.publicKey ?? await fs.readFile(path.resolve(__dirname, '../../build/application-update-key.pem'), 'utf8');
  const updates = new SourceUpdates({ root, publicKey, bootstrapNode, fetch: localFeed(profile) });
  await updates.attach(revision);
  const application = await updates.application(revision);
  // In-process Linux delegation must give userData runtimes the same clean
  // environment as a sourceLaunch child, without changing the host's AppImage target.
  const subprocessEnv = process.platform === 'linux' ? buildEnvironment(application.node, application.python) : undefined;
  setManagedContext({ updates, application, root, profile, host, installersOnly, subprocessEnv });
  require('./index');
}
if (require.main === module) void start().catch(async error => {
  await app.whenReady();
  dialog.showErrorBox('AI Shuffle could not start', String(error));
  app.quit();
});
