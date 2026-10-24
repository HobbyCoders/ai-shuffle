/**
 * The update's database restore point, made by the running app before it
 * restarts into the update instead of by the launcher afterwards.
 *
 * A launcher (4 and later) makes the point in `prepareRollback` before it
 * selects the update, with no window open. `ColdClusterCopy.snapshot` reads
 * the cluster four times (hash, copy, hash the copy, hash the source again),
 * so on a large database the app seems to hang for minutes, and closing it
 * then marks the update failed. When the running app has already made a ready
 * point for the pending revision, the launcher's unchanged `prepareRollback`
 * only validates it (one read). This file is not in the launcher closure
 * (scripts/ci/update_reach.py).
 */
import { createHash } from 'node:crypto';
import { createReadStream, createWriteStream, promises as fs } from 'node:fs';
import { pipeline } from 'node:stream/promises';
import path from 'node:path';
import { ColdClusterCopy, durableJson, syncDirectory } from './database-restore';
import { privateDirectory } from './private-file';

/**
 * Makes exactly the point `ColdClusterCopy.snapshot` makes (same folders, same
 * manifest, checked by the same `validate`), reading each file once while it is
 * copied and once more to check the copy. The source is checked for changes by
 * size and modification time, which a stopped cluster does not change.
 */
export class SinglePassClusterCopy extends ColdClusterCopy {
  constructor(private readonly cluster: string, private readonly stop: (snapshot: boolean) => Promise<void>) { super(cluster, stop); }

  override async snapshot(): Promise<void> {
    await this.stop(true);
    await privateDirectory(this.cluster);
    const source = path.join(this.cluster, 'data');
    const before = await shape(source);
    if (!before.PG_VERSION || !before[path.join('global', 'pg_control')] || before['postmaster.pid']) throw new Error('Not a stopped managed PostgreSQL cluster');
    // The journal owns these paths; a prior snapshot is never overwritten implicitly.
    const snapshot = path.join(this.cluster, 'update-snapshot');
    await fs.mkdir(snapshot, { mode: 0o700 });
    const manifest: Record<string, string> = {};
    // Timings for launch.log (#430); the checks below are unchanged.
    const started = Date.now();
    const stamp = (text: string) => console.log(`[${new Date().toISOString()}] database restore point: ${text} after ${((Date.now() - started) / 1000).toFixed(1)}s`);
    // The same walk and key order as inventory() in database-restore.ts: validate() compares the JSON text.
    const copy = async (relative: string): Promise<void> => {
      const from = path.join(source, relative), to = path.join(snapshot, 'data', relative);
      const stat = await fs.lstat(from);
      if (stat.isSymbolicLink()) throw new Error('Database restore points do not support symbolic links or external tablespaces');
      if (stat.isDirectory()) {
        manifest[relative + '/'] = 'directory';
        await fs.mkdir(to, { mode: 0o700 });
        for (const name of (await fs.readdir(from)).sort()) await copy(path.join(relative, name));
        await syncDirectory(to);
      } else if (stat.isFile()) {
        const hash = createHash('sha256');
        const read = createReadStream(from);
        read.on('data', chunk => hash.update(chunk));
        await pipeline(read, createWriteStream(to, { flags: 'wx', mode: 0o600 }));
        const fd = await fs.open(to, 'r+');
        try { await fd.sync(); } finally { await fd.close(); }
        manifest[relative] = hash.digest('hex');
      } else throw new Error('Unsupported database file');
    };
    await copy('');
    await durableJson(path.join(snapshot, 'manifest.json'), manifest);
    await syncDirectory(this.cluster);
    stamp(`copied ${Object.keys(manifest).length} entries`);
    // The launcher validates the copy the same way before it starts the update.
    await this.validate();
    stamp('copy validated');
    if (JSON.stringify(before) !== JSON.stringify(await shape(source))) throw new Error('Database changed while taking its restore point');
  }
}

/** Audited Windows launchers that hash a ready point in prepareRollback before select.
 * Launcher 7 adds optional voice dependency installation; like launcher 6 it retains
 * the ColdClusterCopy factory and full ready-journal hash check. source-updates.test
 * exercises that factory against same-size file corruption. Launcher 8 changes only the
 * PostgreSQL start options and the Windows process-identity probe; the factory and
 * ready-journal path are unchanged.
 * A new version is NOT trusted automatically: review its factory and ready-journal path. */
export const RESTORE_POINT_VALIDATING_LAUNCHERS: ReadonlySet<number> = new Set([4, 5, 6, 7, 8]);

/** Unknown handoffs and fenced recovery must keep full validation. */
export function launcherValidatedRestorePoint(platform: string, launcher: number, installedHandoff: boolean, migrationRisk: boolean): boolean {
  return platform === 'win32' && Number.isSafeInteger(launcher) && RESTORE_POINT_VALIDATING_LAUNCHERS.has(launcher) && installedHandoff && !migrationRisk;
}

/**
 * Only for a point the installed launcher just hashed. Compare every file and
 * directory with its manifest without reading the database contents again.
 * requireRestorePoint still checks the journal's phase and booting revision.
 */
export async function checkRestorePointStructure(root: string): Promise<void> {
  const snapshot = path.join(root, 'update-snapshot');
  const manifest = JSON.parse(await fs.readFile(path.join(snapshot, 'manifest.json'), 'utf8'));
  const invalid = () => new Error('Database restore point failed validation');
  if (!manifest || typeof manifest !== 'object' || Array.isArray(manifest)) throw invalid();
  const expected = Object.entries(manifest).map(([key, value]) => {
    if (value !== 'directory' && (typeof value !== 'string' || !/^[a-f0-9]{64}$/.test(value))) throw invalid();
    return `${key}\0${value === 'directory'}`;
  }).sort();
  const actual: string[] = [];
  const walk = async (relative: string): Promise<void> => {
    const file = path.join(snapshot, 'data', relative);
    const stat = await fs.lstat(file);
    if (stat.isSymbolicLink() || (!stat.isDirectory() && !stat.isFile())) throw invalid();
    actual.push(`${relative}${stat.isDirectory() ? '/' : ''}\0${stat.isDirectory()}`);
    if (stat.isDirectory()) for (const name of (await fs.readdir(file)).sort()) await walk(path.join(relative, name));
  };
  await walk('');
  if (JSON.stringify(expected) !== JSON.stringify(actual.sort())) throw invalid();
}

/** Every entry below `root`: "directory", or the file's size and modification time. */
async function shape(root: string): Promise<Record<string, string>> {
  const result: Record<string, string> = {};
  const walk = async (relative: string): Promise<void> => {
    const stat = await fs.lstat(path.join(root, relative));
    if (stat.isDirectory()) {
      result[relative + '/'] = 'directory';
      for (const name of (await fs.readdir(path.join(root, relative))).sort()) await walk(path.join(relative, name));
    } else result[relative] = `${stat.isFile() ? 'file' : 'other'}:${stat.size}:${stat.mtimeMs}`;
  };
  await walk('');
  return result;
}

/**
 * True while a restore point made for another revision than `running` may
 * still be restored: the update is about to start (or is being put back).
 * This process must not open the database then; anything it wrote would be
 * lost when the launcher restores that copy. A journal that cannot be read is
 * treated the same way: the launcher refuses to start anything over it.
 */
export async function pointHeldFor(root: string, running: string | null): Promise<boolean> {
  let text: string;
  try { text = await fs.readFile(path.join(root, 'restore-point.json'), 'utf8'); }
  catch (error) { return (error as NodeJS.ErrnoException).code !== 'ENOENT'; }
  try {
    const journal = JSON.parse(text);
    if (journal?.phase === 'committed' || journal?.phase === 'preparing') return false;
    return journal?.revision !== running;
  } catch { return true; }
}

/** What the status page says while such a point is held. */
export const HELD_POINT_MESSAGE = 'An update is ready to start and a copy of your database was made for it. Quit AI Shuffle and open it again to finish installing the update. This version does not reopen the database until then, so that copy stays exact.';
/** Why an install stopped before the restart because the app could not copy the database. */
export class RestorePointCopyError extends Error {}
