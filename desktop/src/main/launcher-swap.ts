/**
 * Replace the installed Windows launcher (resources/app.asar) from verified
 * source (#1821 D5 steps 3 and 4). desktop/LAUNCHER.md describes the protocol
 * and every file this writes.
 *
 * Runs in three places:
 * - replaceLauncher(): the updated app, after its revision is healthy, when the
 *   launcher that started it is older than the release's desktop.launcher.
 * - confirmLauncher(): the updated app at every start, right after attach.
 * - launcherTrial()/countLauncherStart()/restoreLauncher(): the new launcher.
 * Nothing here imports electron; launcher-self-test.js loads it in node mode.
 */
import { createHash, randomBytes } from 'node:crypto';
import { spawn } from 'node:child_process';
import type * as FS from 'node:fs';
import path from 'node:path';
import { packAsar, rawFs, readAsarMember, type AsarMember } from './asar';
import { buildEnvironment } from './source-build';
import { requiredLauncher, type SourceApplication, type SourceManifest, type SourceUpdates } from './source-updates';
import type { InstalledHost } from './installed-host';

export const LAUNCHER_FILE = 'launcher.json';
export const SWAP_MARKER = 'launcher-swap.json';
export const ASAR = 'app.asar';
/** Staged next to app.asar. Electron opens an archive only through a path segment ending in .asar, so not app.asar.new. */
export const NEW_ASAR = 'app.new.asar';
export const OLD_ASAR = 'app.asar.old';
const ENTRY = 'dist/main/managed-entry.js';
export const SELF_TEST = 'dist/main/launcher-self-test.js';
const SELF_TEST_TIMEOUT_MS = 60_000;

/** Same contract as scripts/launcher-contract.mjs (tested to agree). */
export function parseLauncher(text: string): { version: number; minimum: number } {
  const value = JSON.parse(text);
  const count = (n: unknown): n is number => Number.isSafeInteger(n) && (n as number) >= 0;
  if (!value || typeof value !== 'object' || !count(value.version) || value.version < 1 || !count(value.minimum) || value.minimum > value.version) {
    throw new Error('desktop/launcher.json must hold {"version": n >= 1, "minimum": 0..version}');
  }
  return { version: value.version, minimum: value.minimum };
}

export type SwapPhase = 'trial' | 'restoring' | 'confirmed' | 'restored' | 'rejected';
export interface SwapMarker { format: 1; phase: SwapPhase; revision: string; from: number; to: number; starts: number; old?: string; new?: string; reason?: string }
const PHASES: SwapPhase[] = ['trial', 'restoring', 'confirmed', 'restored', 'rejected'];
const HASH = /^[a-f0-9]{64}$/;

export function readMarker(root: string, fs: typeof FS = rawFs()): SwapMarker | null {
  let text: string;
  try { text = fs.readFileSync(path.join(root, SWAP_MARKER), 'utf8'); }
  catch (error) { if ((error as NodeJS.ErrnoException).code === 'ENOENT') return null; throw error; }
  const marker = JSON.parse(text) as SwapMarker;
  const count = (n: unknown) => Number.isSafeInteger(n) && (n as number) >= 0;
  if (!marker || marker.format !== 1 || !PHASES.includes(marker.phase) || !/^[a-f0-9]{40}$/.test(marker.revision) ||
      !count(marker.from) || !count(marker.to) || !count(marker.starts) ||
      (['trial', 'restoring'].includes(marker.phase) && (!HASH.test(marker.old ?? '') || !HASH.test(marker.new ?? '')))) {
    throw new Error('Invalid launcher swap record');
  }
  return marker;
}
function writeMarker(root: string, marker: SwapMarker, fs: typeof FS): void {
  const file = path.join(root, SWAP_MARKER);
  fs.writeFileSync(file + '.tmp', JSON.stringify(marker), { mode: 0o600 });
  fs.renameSync(file + '.tmp', file);
}
function removeMarker(root: string, fs: typeof FS): void { fs.rmSync(path.join(root, SWAP_MARKER), { force: true }); }

const sha256 = (data: Buffer) => createHash('sha256').update(data).digest('hex');
const samePath = (a: string, b: string) => process.platform === 'win32' ? a.toLowerCase() === b.toLowerCase() : a === b;

/**
 * The installed resources folder, refusing anything a link could redirect:
 * the folder must be reached without a symlink or junction and app.asar must
 * be a plain file. The EXE path comes from installed-host.json, which only the
 * launcher writes (with its own process.execPath).
 */
export function installedResources(executable: string, fs: typeof FS = rawFs()): string {
  if (!path.isAbsolute(executable) || !fs.lstatSync(executable).isFile()) throw new Error('The installed launcher is not a file');
  const resources = path.join(path.dirname(path.resolve(executable)), 'resources');
  if (!samePath(fs.realpathSync.native(resources), resources)) throw new Error('The installed app folder is reached through a link; the launcher was not replaced');
  if (!fs.lstatSync(resources).isDirectory()) throw new Error('The installed resources folder is not a directory');
  if (!fs.lstatSync(path.join(resources, ASAR)).isFile()) throw new Error('The installed launcher archive is not a file');
  return resources;
}
/** Remove a leftover file (or a link planted in its place: unlink removes the link itself). */
function removeLeftover(file: string, fs: typeof FS): void {
  try { if (fs.lstatSync(file).isDirectory()) throw new Error(`${file} is a directory`); }
  catch (error) { if ((error as NodeJS.ErrnoException).code === 'ENOENT') return; throw error; }
  fs.unlinkSync(file);
}
/** Create a new file, failing if anything (including a link) appeared at the path. */
function writeNew(file: string, data: Buffer, fs: typeof FS): void {
  removeLeftover(file, fs);
  const fd = fs.openSync(file, 'wx', 0o644);
  try { fs.writeSync(fd, data, 0, data.length, 0); fs.fsyncSync(fd); } finally { fs.closeSync(fd); }
}

function files(root: string, relative: string, fs: typeof FS, out: string[]): void {
  const full = path.join(root, relative);
  const stat = fs.lstatSync(full);
  if (stat.isSymbolicLink()) throw new Error(`Refusing a link inside the launcher files: ${relative}`);
  if (stat.isFile()) { out.push(relative.split(path.sep).join('/')); return; }
  if (!stat.isDirectory()) throw new Error(`Unexpected launcher file ${relative}`);
  for (const name of fs.readdirSync(full).sort()) files(root, path.join(relative, name), fs, out);
}
/**
 * The new archive's members, all read from the verified, built desktop source
 * (the same files electron-builder.yml packs) except package.json, which is
 * the installed archive's own: electron-builder rewrites it, and its name
 * chooses the profile folder. The source must use the same entry.
 */
export function launcherMembers(desktopDir: string, installedAsar: string, fs: typeof FS = rawFs()): AsarMember[] {
  const installedPackage = readAsarMember(fs, installedAsar, 'package.json');
  if (JSON.parse(installedPackage.toString('utf8')).main !== ENTRY || JSON.parse(fs.readFileSync(path.join(desktopDir, 'package.json'), 'utf8')).main !== ENTRY) {
    throw new Error('The installed launcher and the update use different entry points');
  }
  parseLauncher(fs.readFileSync(path.join(desktopDir, LAUNCHER_FILE), 'utf8'));
  const names: string[] = [];
  for (const item of ['dist', 'src/pages', 'build/icon.png', 'build/application-update-key.pem', 'scripts/source-profile.cjs', LAUNCHER_FILE]) files(desktopDir, item, fs, names);
  // Production dependencies only, as electron-builder packs them. desktop/node_modules
  // itself is the build's junction into the managed dependency cache.
  const lock = JSON.parse(fs.readFileSync(path.join(desktopDir, 'package-lock.json'), 'utf8'));
  for (const [key, value] of Object.entries(lock.packages ?? {}) as [string, { dev?: boolean; devOptional?: boolean }][]) {
    if (!/^node_modules\/(?:@[^/]+\/)?[^/]+$/.test(key) || value.dev || value.devOptional) continue;
    files(desktopDir, key, fs, names);
  }
  for (const required of [ENTRY, SELF_TEST, 'build/application-update-key.pem']) if (!names.includes(required)) throw new Error(`The update has no ${required}`);
  return [{ name: 'package.json', data: installedPackage }, ...names.map(name => ({ name, data: fs.readFileSync(path.join(desktopDir, name)) }))];
}

/** Bounded, minimal environment for the node-mode self-test; never the app's own. */
function selfTestEnvironment(executable: string): NodeJS.ProcessEnv {
  return { ...buildEnvironment(executable, executable), ELECTRON_RUN_AS_NODE: '1' };
}
/**
 * Run the candidate archive's own self-test with the installed EXE in node
 * mode (ELECTRON_RUN_AS_NODE). Resolves only when it exits 0 and prints the
 * verdict for this nonce and version; anything else (including an EXE that
 * ignores the script and exits 0) is a failure.
 */
export function selfTest(executable: string, candidate: string, expect: number, root: string, timeoutMs = SELF_TEST_TIMEOUT_MS): Promise<void> {
  const nonce = randomBytes(16).toString('hex');
  return new Promise((resolve, reject) => {
    const child = spawn(executable, [path.join(candidate, SELF_TEST), `--nonce=${nonce}`, `--expect=${expect}`, `--root=${root}`],
      { env: selfTestEnvironment(executable), cwd: path.dirname(candidate), windowsHide: true, stdio: ['ignore', 'pipe', 'pipe'] });
    let output = ''; let errors = '';
    const timer = setTimeout(() => { child.kill(); reject(new Error('The launcher self-test did not finish in time')); }, timeoutMs);
    child.stdout!.on('data', chunk => { if (output.length < 65536) output += chunk; });
    child.stderr!.on('data', chunk => { if (errors.length < 4096) errors += chunk; });
    child.on('error', error => { clearTimeout(timer); reject(error); });
    child.on('close', code => {
      clearTimeout(timer);
      const verdict = output.split(/\r?\n/).map(line => { try { return JSON.parse(line); } catch { return null; } })
        .find(value => value?.launcherSelfTest === 1 && value.nonce === nonce);
      if (code === 0 && verdict?.ok === true && verdict.version === expect) resolve();
      else reject(new Error(`The launcher self-test failed (exit ${code}): ${verdict?.error ?? (errors.trim().slice(0, 500) || 'no verdict')}`));
    });
  });
}

export type SwapResult = 'current' | 'unavailable' | 'skipped' | 'rejected' | 'busy' | 'swapped';
export interface SwapOptions {
  root: string; host: InstalledHost; application: SourceApplication; manifest: SourceManifest;
  /** The version of the launcher that started this app (SHUFFLE_LAUNCHER_VERSION). */
  running: number;
  selfTest?: typeof selfTest;
  /** Tests: stop after a named step, as a crash or power loss would. */
  interrupt?: (step: 'written' | 'tested' | 'kept' | 'marked') => void;
  fs?: typeof FS;
}
/**
 * Build app.asar from the verified source and swap it in when the running
 * launcher is older than the release needs. Order: write app.new.asar, run its
 * self-test, copy app.asar to app.asar.old, record the trial, rename. app.asar
 * is a complete file at every step.
 */
export async function replaceLauncher(options: SwapOptions): Promise<SwapResult> {
  const fs = options.fs ?? rawFs();
  const { root, application, manifest, running } = options;
  const required = requiredLauncher(manifest);
  if (manifest.revision !== application.revision) throw new Error('The release does not match the running application');
  if (running >= required) return 'current';
  const source = parseLauncher(fs.readFileSync(path.join(application.desktopDir, LAUNCHER_FILE), 'utf8')).version;
  if (source < required || source <= running) return 'unavailable';
  const marker = readMarker(root, fs);
  // An unconfirmed swap is settled at start (confirmLauncher) before another is tried;
  // a revision whose launcher failed is never tried again, only a newer one.
  if (marker && (['trial', 'restoring'].includes(marker.phase) || (['restored', 'rejected'].includes(marker.phase) && marker.revision === application.revision))) return 'skipped';
  const resources = installedResources(options.host.executable, fs);
  const asar = path.join(resources, ASAR); const candidate = path.join(resources, NEW_ASAR); const old = path.join(resources, OLD_ASAR);
  const data = packAsar(launcherMembers(application.desktopDir, asar, fs));
  writeNew(candidate, data, fs);
  options.interrupt?.('written');
  try { await (options.selfTest ?? selfTest)(options.host.executable, candidate, source, root); }
  catch (error) {
    removeLeftover(candidate, fs);
    writeMarker(root, { format: 1, phase: 'rejected', revision: application.revision, from: running, to: source, starts: 0, reason: String(error).slice(0, 500) }, fs);
    return 'rejected';
  }
  options.interrupt?.('tested');
  const current = fs.readFileSync(asar);
  writeNew(old + '.tmp', current, fs); fs.renameSync(old + '.tmp', old);
  options.interrupt?.('kept');
  writeMarker(root, { format: 1, phase: 'trial', revision: application.revision, from: running, to: source, starts: 0, old: sha256(current), new: sha256(data) }, fs);
  options.interrupt?.('marked');
  try { fs.renameSync(candidate, asar); }
  catch (error) {
    // A second launcher still holds app.asar open (Windows): leave everything as it was and try at a later healthy start.
    if (!['EBUSY', 'EPERM', 'EACCES'].includes((error as NodeJS.ErrnoException).code ?? '')) throw error;
    removeMarker(root, fs); removeLeftover(candidate, fs);
    return 'busy';
  }
  return 'swapped';
}

let attempt: Promise<SwapResult> | null = null;
/** Once per app run, after the worker reported ready; a busy or failed attempt waits for the next start. */
export function replaceInstalledLauncher(context: { updates: SourceUpdates; application: SourceApplication; root: string; host: InstalledHost }, running: number): Promise<SwapResult> {
  return attempt ??= context.updates.release(context.application.revision)
    .then(manifest => replaceLauncher({ root: context.root, host: context.host, application: context.application, manifest, running }));
}

export type ConfirmResult = 'none' | 'confirmed' | 'retired' | 'retry' | 'restored' | 'cleared' | 'unknown';
/**
 * Called by the updated app at each start, after attach. Settles a swap: the
 * new launcher started this app (confirmed), the swap never reached its rename
 * (retry later), or the new launcher was restored. A confirmed swap keeps
 * app.asar.old until a later release runs.
 */
export function confirmLauncher(root: string, executable: string, running: number, revision: string, fs: typeof FS = rawFs()): ConfirmResult {
  const marker = readMarker(root, fs);
  if (!marker) return 'none';
  const resources = path.join(path.dirname(executable), 'resources');
  if (marker.phase === 'confirmed') {
    if (marker.revision === revision) return 'confirmed';
    removeLeftover(path.join(resources, OLD_ASAR), fs); removeMarker(root, fs);
    return 'retired';
  }
  if (!['trial', 'restoring'].includes(marker.phase)) return 'none';
  if (running >= marker.to) { writeMarker(root, { ...marker, phase: 'confirmed' }, fs); return 'confirmed'; }
  let current: string | null = null;
  try { current = sha256(fs.readFileSync(path.join(resources, ASAR))); } catch { /* reported below as unknown */ }
  if (current === marker.old && marker.phase === 'trial') { removeLeftover(path.join(resources, NEW_ASAR), fs); removeMarker(root, fs); return 'retry'; }
  if (current === marker.old) { writeMarker(root, { ...marker, phase: 'restored' }, fs); return 'restored'; }
  // A reinstall replaced app.asar: nothing of the swap is left to settle.
  if (current && current !== marker.new) { removeMarker(root, fs); return 'cleared'; }
  return 'unknown';
}

/**
 * The new launcher, before it starts anything: 'restore' when it already
 * started the updated app once without that app confirming it (or a restore
 * is still due), 'trial' for its first start, 'none' otherwise.
 */
export function launcherTrial(root: string, own: number, fs: typeof FS = rawFs()): 'none' | 'trial' | 'restore' {
  let marker: SwapMarker | null;
  // An unreadable record must never stop the launcher itself from starting.
  try { marker = readMarker(root, fs); } catch { return 'none'; }
  if (!marker || marker.to !== own) return 'none';
  if (marker.phase === 'restoring') return 'restore';
  if (marker.phase !== 'trial') return 'none';
  return marker.starts >= 1 ? 'restore' : 'trial';
}
/** Recorded just before the launcher starts the updated app. */
export function countLauncherStart(root: string, fs: typeof FS = rawFs()): void {
  const marker = readMarker(root, fs);
  if (marker?.phase === 'trial') writeMarker(root, { ...marker, starts: marker.starts + 1 }, fs);
}

/**
 * Runs in a detached copy of the EXE in node mode once the launcher has
 * exited (Windows keeps app.asar open while it runs): copy app.asar.old over
 * app.asar if it is the file the swap kept, then start the EXE again.
 * argv: pid, resources folder, sha256 of app.asar.old, EXE.
 */
export const RESTORE_HELPER = `
const fs = process.versions.electron ? require('original-fs') : require('fs');
const path = require('path'); const crypto = require('crypto'); const cp = require('child_process');
const [pid, resources, expected, exe] = process.argv.slice(1);
const deadline = Date.now() + 600000;
const alive = () => { try { process.kill(Number(pid), 0); return true; } catch (error) { return error.code === 'EPERM'; } };
const sleep = ms => Atomics.wait(new Int32Array(new SharedArrayBuffer(4)), 0, 0, ms);
while (alive()) { if (Date.now() > deadline) process.exit(2); sleep(200); }
const old = path.join(resources, 'app.asar.old'); const temporary = path.join(resources, 'app.restore.tmp');
if (fs.lstatSync(old).isSymbolicLink()) process.exit(3);
const data = fs.readFileSync(old);
if (crypto.createHash('sha256').update(data).digest('hex') !== expected) process.exit(3);
try { fs.unlinkSync(temporary); } catch {}
const fd = fs.openSync(temporary, 'wx'); fs.writeSync(fd, data, 0, data.length, 0); fs.fsyncSync(fd); fs.closeSync(fd);
for (let attempt = 0; ; attempt++) {
  try { fs.renameSync(temporary, path.join(resources, 'app.asar')); break; }
  catch (error) { if (attempt >= 50) process.exit(4); sleep(200); }
}
const env = { ...process.env }; delete env.ELECTRON_RUN_AS_NODE;
cp.spawn(exe, [], { detached: true, stdio: 'ignore', env, cwd: path.dirname(exe) }).unref();
`;
/** Mark the restore due and hand it to the helper; the caller then quits. */
export function restoreLauncher(root: string, executable: string, pid = process.pid, fs: typeof FS = rawFs(), helper = executable): void {
  const marker = readMarker(root, fs);
  if (!marker || !['trial', 'restoring'].includes(marker.phase)) return;
  const resources = installedResources(executable, fs);
  writeMarker(root, { ...marker, phase: 'restoring' }, fs);
  spawn(helper, ['-e', RESTORE_HELPER, String(pid), resources, marker.old!, executable],
    { detached: true, stdio: 'ignore', windowsHide: true, cwd: path.dirname(executable), env: { ...process.env, ELECTRON_RUN_AS_NODE: '1' } }).unref();
}
