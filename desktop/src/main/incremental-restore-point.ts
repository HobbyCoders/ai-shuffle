/** Incremental app-side cold copies. Installed launchers still hash the entire ready point before select. */
import { createHash } from 'node:crypto';
import { createReadStream, createWriteStream, promises as fs } from 'node:fs';
import { pipeline } from 'node:stream/promises';
import path from 'node:path';
import { ColdClusterCopy, durableJson, durableRename, syncDirectory } from './database-restore';
import { privateDirectory } from './private-file';

type Shape = Record<string, string>;
interface Receipt { format: 1; source: Shape; copy: Shape; manifest: Record<string, string> }

/** File identity and change time also count: replacing a same-size file or resetting mtime is not a cache hit. */
async function shape(root: string): Promise<Shape> {
  const entries: Shape = {};
  const walk = async (relative: string): Promise<void> => {
    const file = path.join(root, relative), stat = await fs.lstat(file, { bigint: true });
    if (stat.isSymbolicLink()) throw new Error('Database restore points do not support symbolic links or external tablespaces');
    if (stat.isDirectory()) {
      entries[relative + '/'] = 'directory';
      for (const name of (await fs.readdir(file)).sort()) await walk(path.join(relative, name));
    } else if (stat.isFile()) entries[relative] = [stat.dev, stat.ino, stat.size, stat.mtimeNs, stat.ctimeNs, stat.birthtimeNs].join(':');
    else throw new Error('Unsupported database file');
  };
  await walk('');
  return entries;
}

/**
 * A successful update retains one private, immutable cold copy as a disposable cache.
 * Never link it to the live cluster. Move it back under the journal's snapshot path,
 * replace changed files, and emit the legacy SHA manifest. The installed launcher
 * MUST fully validate that manifest before selecting code (including cached files).
 * Recovery uses ColdClusterCopy's full hash checks and restartable copy/rename path.
 * Cache receipts are performance hints, never authority to restore a database.
 */
export class IncrementalClusterCopy extends ColdClusterCopy {
  constructor(private readonly cluster: string, private readonly stop: (snapshot: boolean) => Promise<void>) { super(cluster, stop); }

  override async snapshot(): Promise<void> {
    const started = Date.now();
    await this.stop(true);
    await privateDirectory(this.cluster);
    const source = path.join(this.cluster, 'data'), snapshot = path.join(this.cluster, 'update-snapshot');
    const cache = path.join(this.cluster, 'update-copy-cache');
    const before = await shape(source);
    if (!before.PG_VERSION || !before[path.join('global', 'pg_control')] || before['postmaster.pid']) throw new Error('Not a stopped managed PostgreSQL cluster');
    // Never overwrite a point owned by an unfinished journal.
    await fs.mkdir(snapshot, { mode: 0o700 });
    let receipt: Receipt | undefined, cached: Shape = {};
    try {
      const cacheStat = await fs.lstat(cache), receiptStat = await fs.lstat(path.join(cache, 'incremental.json'));
      if (!cacheStat.isDirectory() || cacheStat.isSymbolicLink() || !receiptStat.isFile() || receiptStat.isSymbolicLink()) throw new Error('Unsafe cold-copy cache');
      const candidate = JSON.parse(await fs.readFile(path.join(cache, 'incremental.json'), 'utf8')) as Receipt;
      if (candidate?.format === 1 && candidate.source && candidate.copy && candidate.manifest) {
        cached = await shape(path.join(cache, 'data'));
        receipt = candidate;
      }
    } catch { /* Missing or damaged cache: build a complete new point, never trust a partial one. */ }
    if (receipt) {
      await fs.rmdir(snapshot); // our empty reservation, not an earlier point
      await durableRename(cache, snapshot);
      await fs.rm(path.join(snapshot, 'incremental.json'), { force: true });
    } else {
      // It is only a cache, never a recovery point. Do not need space for two cold copies.
      await fs.rm(cache, { recursive: true, force: true });
    }
    const destination = path.join(snapshot, 'data');
    const manifest: Record<string, string> = {};
    let copied = 0, reused = 0, bytes = 0;
    // Remove obsolete entries without ever following a link. A cache with links was rejected above.
    for (const name of Object.keys(cached).reverse()) {
      if (!(name in before)) await fs.rm(path.join(destination, name), { recursive: true, force: true });
    }
    const copy = async (relative: string): Promise<void> => {
      const from = path.join(source, relative), to = path.join(destination, relative);
      if (before[relative + '/'] === 'directory') {
        manifest[relative + '/'] = 'directory';
        await fs.mkdir(to, { recursive: true, mode: 0o700 });
        for (const name of (await fs.readdir(from)).sort()) await copy(path.join(relative, name));
        await syncDirectory(to);
      } else {
        const hash = receipt?.manifest[relative];
        if (receipt && receipt.source[relative] === before[relative] && cached[relative] !== undefined && receipt.copy[relative] === cached[relative] && typeof hash === 'string' && /^[a-f0-9]{64}$/.test(hash)) {
          manifest[relative] = hash; reused++; return;
        }
        await fs.rm(to, { force: true });
        const digest = createHash('sha256'), read = createReadStream(from);
        read.on('data', chunk => { digest.update(chunk); bytes += chunk.length; });
        await pipeline(read, createWriteStream(to, { flags: 'wx', mode: 0o600 }));
        const fd = await fs.open(to, 'r+');
        try { await fd.sync(); } finally { await fd.close(); }
        manifest[relative] = digest.digest('hex'); copied++;
      }
    };
    await copy('');
    if (JSON.stringify(before) !== JSON.stringify(await shape(source))) throw new Error('Database changed while taking its restore point');
    await durableJson(path.join(snapshot, 'manifest.json'), manifest);
    await durableJson(path.join(snapshot, 'incremental.json'), { format: 1, source: before, copy: await shape(destination), manifest });
    await syncDirectory(this.cluster);
    // Do not re-read the copy here: launchers 4-7 validate all its hashes before select.
    console.log(`[${new Date().toISOString()}] database restore point: copied ${copied} files (${bytes} bytes), reused ${reused} after ${((Date.now() - started) / 1000).toFixed(3)}s; launcher validation required`);
  }

  override async discard(): Promise<void> {
    const snapshot = path.join(this.cluster, 'update-snapshot'), cache = path.join(this.cluster, 'update-copy-cache');
    let complete = false;
    try { await fs.access(path.join(snapshot, 'incremental.json')); complete = true; }
    catch (error) { if ((error as NodeJS.ErrnoException).code !== 'ENOENT') throw error; }
    if (complete) {
      await fs.rm(cache, { recursive: true, force: true });
      await durableRename(snapshot, cache);
    }
    await super.discard();
  }
}
