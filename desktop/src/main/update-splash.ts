/** Source-only notice between quitting and the new build attaching; never part of the launcher. */
import { execFile, spawn } from 'node:child_process';
import path from 'node:path';
import type { SourceState } from './source-updates';

export type SplashPhase = 'checking' | 'starting' | 'attached' | 'stopped';
export function splashPhase(state: SourceState | null, revision: string): SplashPhase {
  if (!state) return 'checking';
  if (state.booting === revision) return state.waiting === revision || state.mayHaveMigrated?.[revision] !== undefined ? 'attached' : 'starting';
  if (state.active === revision) return 'attached';
  if (state.pending === revision && !state.failed?.includes(revision)) return 'checking';
  return 'stopped';
}
export const SPLASH_TEXT = {
  checking: 'Checking the copy of your database. This can take a few minutes. AI Shuffle opens by itself when it is ready.',
  starting: 'Opening the new version. Its first start can take a few minutes. Please keep AI Shuffle open.',
};
export const SPLASH_LIMIT_MS = 30 * 60_000;
export const LAUNCHER_GRACE_MS = 20_000;
export const LAUNCHER_GONE_MS = 10_000;
/** Checking leaves state.json unchanged, even when the launcher exits with an error. */
export function launcherGaveUp(phase: SplashPhase, started: number, lastSeen: number | null, now: number): boolean {
  return phase === 'checking' && now - started > LAUNCHER_GRACE_MS && now - (lastSeen ?? started) > LAUNCHER_GONE_MS;
}
export function launcherImage(executable: string | undefined): string | null {
  const name = executable ? path.win32.basename(executable) : '';
  return /^[\w .()-]{1,100}\.exe$/i.test(name) ? name : null;
}
/** A failed process query is unknown, not proof the launcher exited. */
export function imageRunning(image: string, run: typeof execFile = execFile): Promise<boolean> {
  return new Promise(resolve => {
    try {
      run('tasklist', ['/FI', `IMAGENAME eq ${image}`, '/FO', 'CSV', '/NH'], { windowsHide: true, timeout: 5000 }, (error, stdout) => {
        resolve(!!error || String(stdout).toLowerCase().includes(`"${image.toLowerCase()}"`));
      });
    } catch { resolve(true); }
  });
}
export interface SplashOptions {
  electron: string;
  mainDir: string;
  root: string;
  revision: string;
  release: string;
  profile: string;
  launcher: string | null;
}
export function splashArgs(options: SplashOptions): string[] {
  return [path.join(options.mainDir, 'update-splash-main.js'), `--root=${options.root}`, `--revision=${options.revision}`,
    `--release=${options.release}`, `--profile=${options.profile}`, ...(options.launcher ? [`--launcher=${options.launcher}`] : [])];
}
/** Independent profile and lifetime. The caller closes it if relaunch fails. */
export function launchUpdateSplash(options: SplashOptions, run: typeof spawn = spawn): (() => void) | null {
  try {
    const env = { ...process.env };
    for (const key of ['ELECTRON_RUN_AS_NODE', 'SHUFFLE_SOURCE_ROOT', 'SHUFFLE_SOURCE_REVISION', 'SHUFFLE_SOURCE_NODE']) delete env[key];
    const child = run(options.electron, splashArgs(options), { detached: true, stdio: 'ignore', env, windowsHide: false });
    child.on('error', error => console.error('Could not show the update window', error));
    child.unref();
    return () => { try { child.kill(); } catch { /* already closed */ } };
  } catch (error) {
    console.error('Could not show the update window', error);
    return null;
  }
}
