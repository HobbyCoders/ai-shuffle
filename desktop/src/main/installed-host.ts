/** The installed executable remains the entry point after locally built source updates. */
import { promises as fs, readFileSync } from 'node:fs';
import path from 'node:path';
import { spawn } from 'node:child_process';
import { buildEnvironment } from './source-build';
import { DISCLOSURE_ACCEPTED_ENV } from './disclosure';
import type { SourceApplication } from './source-updates';

export interface InstalledHost { executable: string; userData: string; name: string }
export class InstallerRequired extends Error {}
/** Keep the installed AppArmor/setuid sandbox; never execute npm Electron on Linux. */
export async function requireInstalledElectron(application: SourceApplication, installedVersion: string): Promise<void> {
  const lock = JSON.parse(await fs.readFile(path.join(application.desktopDir, 'package-lock.json'), 'utf8'));
  const version = lock.packages?.['node_modules/electron']?.version;
  if (!/^\d+\.\d+\.\d+$/.test(version ?? '') || version !== installedVersion) {
    throw new InstallerRequired('This update requires a newer AI Shuffle installer to preserve the Linux Electron sandbox.');
  }
  // Do not import an older auto-starting entry while probing its exported API.
  const entry = await fs.readFile(path.join(application.desktopDir, 'dist/main/managed-entry.js'), 'utf8');
  if (!/exports\.startManagedSource\s*=/.test(entry) || !/if\s*\(require\.main\s*===\s*module\)/.test(entry)) {
    throw new InstallerRequired('This source build needs a newer AI Shuffle installer entry.');
  }
}
/** AppImage's execPath is in an ephemeral, read-only squashfs mount. */
export function installedExecutable(): string {
  const executable = process.platform === 'linux' && process.env.APPIMAGE ? process.env.APPIMAGE : process.execPath;
  if (!path.isAbsolute(executable)) throw new Error('Invalid installed desktop executable');
  return executable;
}
/**
 * The installed launcher's version (desktop-build.json `launcher`, from
 * desktop/launcher.json), passed to the updated app it starts (#1821 D5).
 * Launchers built before the stamp pass nothing, which reads as 0.
 */
export const LAUNCHER_VERSION_ENV = 'SHUFFLE_LAUNCHER_VERSION';
export function launcherVersion(value: unknown): number {
  return typeof value === 'string' && /^(?:0|[1-9]\d{0,8})$/.test(value) ? Number(value) : 0;
}
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
      (process.platform === 'win32' && !host.executable.toLowerCase().endsWith('.exe')) || typeof host.name !== 'string' || !host.name ||
      (process.platform === 'win32' ? path.resolve(root).toLowerCase() !== path.resolve(host.userData, 'updates').toLowerCase()
        : path.resolve(root) !== path.resolve(host.userData, 'updates'))) {
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
export function sourceLaunch(application: SourceApplication, root: string, args: string[] = [], launcher = 0) {
  return {
    executable: application.electron,
    args: [application.desktopDir, ...args],
    options: {
      // Electron is a GUI executable. windowsHide also sets SW_HIDE for GUI
      // windows, suppressing the splash until a later tray activation.
      cwd: application.desktopDir, windowsHide: false, detached: true,
      env: { ...buildEnvironment(application.node, application.python),
        // An unattended acceptance run accepts the first-run disclosure through
        // this variable. The allowlisted environment above would drop it, and the
        // updated app would wait on the dialog before starting its worker (#1801).
        ...(process.env[DISCLOSURE_ACCEPTED_ENV] === '1' ? { [DISCLOSURE_ACCEPTED_ENV]: '1' } : {}),
        SHUFFLE_SOURCE_ROOT: root, SHUFFLE_SOURCE_REVISION: application.revision, SHUFFLE_SOURCE_NODE: application.node,
        [LAUNCHER_VERSION_ENV]: String(launcher) },
    },
  };
}
export async function launchInstalledSource(application: SourceApplication, root: string, args: string[], launcher = 0): Promise<void> {
  const launch = sourceLaunch(application, root, args, launcher);
  const log = await fs.open(path.join(root, 'launch.log'), 'a');
  try {
    await new Promise<void>((resolve, reject) => {
      const child = spawn(launch.executable, launch.args, { ...launch.options, stdio: ['ignore', log.fd, log.fd] });
      child.once('error', reject);
      child.once('spawn', () => { child.unref(); resolve(); });
    });
  } finally { await log.close(); }
}
