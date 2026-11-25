/**
 * Whether an install that updates itself in-app should still run the latest
 * installer (desktop/LAUNCHER.md, "Telling an install it needs a newer installer").
 *
 * The in-app update replaces the app but not the installed launcher
 * (`app.asar`). A release that changes the launcher raises
 * `desktop/launcher.json` "version" without raising "minimum", so the update
 * still starts and installed launchers keep their older code: launcher 12's
 * database-copy skip, for example, reaches an install only through the
 * installer. Linux never replaces its launcher in-app. The Windows app does
 * only when its launcher is below "minimum", and then takes this source's
 * launcher itself.
 *
 * The owner is pointed at the installer only when it can help: the hub's
 * installer for this platform is a different build from the one installed and
 * is not an older release than the one running.
 *
 * This file is not in the launcher closure (scripts/ci/update_reach.py): it
 * only runs in the updated app, so installed launchers need no change.
 */
import { promises as fs } from 'node:fs';
import path from 'node:path';
import { INSTALLERS_URL, isNewerVersion } from './installer-updates';

/** The launcher contract a source carries (desktop/launcher.json). */
export interface LauncherContract { version: number; minimum: number }

const PLATFORMS: Partial<Record<NodeJS.Platform, string>> = { win32: 'win', linux: 'linux' };
const RELEASE = /^\d{1,6}\.\d{1,6}\.\d{1,6}$/;
const RUNNING = /^\d{1,6}\.\d{1,6}\.\d{1,6}(?:[-+].*)?$/;
const REVISION = /^[a-f0-9]{40}$/;

/**
 * True when the installed launcher stays older than the one this source packs.
 * `running` is undefined for profile and development builds, which have no
 * installed launcher.
 */
export function launcherBehind(running: number | undefined, contract: LauncherContract, platform: NodeJS.Platform = process.platform): boolean {
  if (running === undefined || !PLATFORMS[platform] || running >= contract.version) return false;
  // Windows replaces a launcher below the minimum from this source after a healthy start (launcher-swap.ts).
  return !(platform === 'win32' && running < contract.minimum);
}

/** desktop/launcher.json's version and minimum, or null when the file is missing or invalid. */
export async function readContract(file: string): Promise<LauncherContract | null> {
  try {
    const value = JSON.parse(await fs.readFile(file, 'utf8'));
    const count = (n: unknown): n is number => Number.isSafeInteger(n) && (n as number) >= 0;
    return value && count(value.version) && count(value.minimum) && value.minimum <= value.version
      ? { version: value.version, minimum: value.minimum } : null;
  } catch { return null; }
}

/**
 * The installed launcher this app runs under, read the way managed-entry.ts
 * reads it: the launcher passes its version in the environment (always on
 * Windows); the installed Linux app runs this source in its own process, so it
 * reads the installed app.asar's launcher.json, then the installer's stamp.
 */
export async function runningLauncher(passed: string | undefined, appPath: string, buildFile: string, platform: NodeJS.Platform = process.platform): Promise<number> {
  const parse = (value: unknown) => typeof value === 'string' && /^(?:0|[1-9]\d{0,8})$/.test(value) ? Number(value) : 0;
  if (passed !== undefined || platform === 'win32') return parse(passed);
  const count = (n: unknown) => Number.isSafeInteger(n) && (n as number) >= 0 ? n as number : null;
  try { const version = count(JSON.parse(await fs.readFile(path.join(appPath, 'launcher.json'), 'utf8'))?.version); if (version !== null) return version; }
  catch { /* Installers before #1821 D5 have no launcher.json. */ }
  try { return count(JSON.parse(await fs.readFile(buildFile, 'utf8'))?.launcher) ?? 0; }
  catch { return 0; }
}

/** The revision the installed installer was built from (resources/worker/desktop-build.json), or null. */
export async function installedRevision(file: string): Promise<string | null> {
  try {
    const revision = JSON.parse(await fs.readFile(file, 'utf8'))?.revision;
    return typeof revision === 'string' && REVISION.test(revision) ? revision : null;
  } catch { return null; }
}

export interface AdviceOptions {
  /** The installed launcher this app runs under; undefined without one. */
  running: number | undefined;
  /** This source's desktop/launcher.json. */
  contract: LauncherContract | null;
  /** The running release (app.getVersion()). */
  version: string;
  /** The installed installer's build revision, when known. */
  installed: string | null;
  platform?: NodeJS.Platform;
  request?: typeof fetch;
}

/**
 * Whether to recommend the latest installer: true or false, or null when the
 * hub could not be read. Reads the hub only when the launcher is behind.
 */
export async function installerRecommended(options: AdviceOptions): Promise<boolean | null> {
  const platform = options.platform ?? process.platform;
  if (!options.contract || !launcherBehind(options.running, options.contract, platform)) return false;
  try {
    const response = await (options.request ?? fetch)(INSTALLERS_URL, { method: 'GET', redirect: 'error', credentials: 'omit',
      signal: AbortSignal.timeout(15_000), headers: { 'User-Agent': 'AI-Shuffle-Installer-Check' } });
    if (!response.ok) return null;
    const text = await response.text();
    if (text.length > 64 * 1024) return null;
    const entry = JSON.parse(text)?.platforms?.[PLATFORMS[platform]!];
    const version = entry?.version, revision = entry?.revision;
    if (typeof version !== 'string' || !RELEASE.test(version) || typeof revision !== 'string' || !REVISION.test(revision)) return null;
    // The installed build already: running it again changes nothing.
    if (revision === options.installed) return false;
    // An older release than the one running may not carry this launcher.
    // (A nightly's suffix is ignored: 1.4.0-nightly.3 counts as 1.4.0.)
    return !(RUNNING.test(options.version) && isNewerVersion(options.version, version));
  } catch { return null; }
}
