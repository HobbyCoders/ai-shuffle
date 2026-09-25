/** The installed EXE remains the entry point after locally built source updates. */
import { promises as fs, readFileSync } from 'node:fs';
import path from 'node:path';
import { spawn } from 'node:child_process';
import { buildEnvironment } from './source-build';
import type { SourceApplication } from './source-updates';

export interface InstalledHost { executable: string; userData: string; name: string }
/** Preserve an explicitly requested debugger across a restart; never enable one. */
export function installedLaunchArgs(args: string[], debuggingPort = ''): string[] {
  // Chromium consumes some switches before process.argv reaches the app.
  // The caller reads its explicit debugging port from app.commandLine.
  const candidates = [...args, ...(debuggingPort ? [`--remote-debugging-port=${debuggingPort}`] : [])];
  // No URL scheme is registered any more, so no aishuffle: argument is forwarded.
  return [...new Set(candidates.filter(arg => /^--remote-debugging-port=(?:0|[1-9]\d{0,4})$/.test(arg)))];
}
export function validateInstalledHost(root: string, host: InstalledHost): InstalledHost {
  if (!host || !path.isAbsolute(host.executable ?? '') || !path.isAbsolute(host.userData ?? '') ||
      !host.executable.toLowerCase().endsWith('.exe') || typeof host.name !== 'string' || !host.name ||
      path.resolve(root).toLowerCase() !== path.resolve(host.userData, 'updates').toLowerCase()) {
    throw new Error('Invalid installed desktop host');
  }
  return host;
}
export function readInstalledHostSync(root: string): InstalledHost | null {
  try { return validateInstalledHost(root, JSON.parse(readFileSync(path.join(root, 'installed-host.json'), 'utf8'))); }
  catch (error) { if ((error as NodeJS.ErrnoException).code === 'ENOENT') return null; throw error; }
}
export async function saveInstalledHost(root: string, host: InstalledHost): Promise<void> {
  validateInstalledHost(root, host);
  const file = path.join(root, 'installed-host.json');
  await fs.writeFile(file + '.tmp', JSON.stringify(host));
  await fs.rename(file + '.tmp', file);
}
export function sourceLaunch(application: SourceApplication, root: string, args: string[] = []) {
  return {
    executable: application.electron,
    args: [application.desktopDir, ...args],
    options: {
      // Electron is a GUI executable. windowsHide also sets SW_HIDE for GUI
      // windows, suppressing the splash until a later tray activation.
      cwd: application.desktopDir, windowsHide: false, detached: true,
      env: { ...buildEnvironment(application.node, application.python),
        SHUFFLE_SOURCE_ROOT: root, SHUFFLE_SOURCE_REVISION: application.revision, SHUFFLE_SOURCE_NODE: application.node },
    },
  };
}
export async function launchInstalledSource(application: SourceApplication, root: string, args: string[]): Promise<void> {
  const launch = sourceLaunch(application, root, args);
  const log = await fs.open(path.join(root, 'launch.log'), 'a');
  try {
    await new Promise<void>((resolve, reject) => {
      const child = spawn(launch.executable, launch.args, { ...launch.options, stdio: ['ignore', log.fd, log.fd] });
      child.once('error', reject);
      child.once('spawn', () => { child.unref(); resolve(); });
    });
  } finally { await log.close(); }
}
