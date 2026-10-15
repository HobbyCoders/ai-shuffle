/** Durable cold-cluster copies. Never follow tablespace/WAL links outside the managed cluster. */
import { promises as fs, createReadStream } from 'node:fs';
import { createHash } from 'node:crypto';
import path from 'node:path';
import { privateDirectory } from './private-file';
import { execFile } from 'node:child_process';
import { promisify } from 'node:util';

export async function syncDirectory(directory: string): Promise<void> {
  // Windows flushes files but does not permit opening directories with Node.
  if (process.platform === 'win32') return;
  const fd = await fs.open(directory, 'r');
  try { await fd.sync(); } finally { await fd.close(); }
}
export async function durableRename(from: string, to: string): Promise<void> {
  if (process.platform === 'win32') {
    // Node has no MOVEFILE_WRITE_THROUGH flag. Flush the rename itself before
    // advancing the journal; paths are passed as environment values, not code.
    const script = [
      '$ErrorActionPreference = "Stop"',
      `Add-Type -TypeDefinition 'using System; using System.Runtime.InteropServices; public static class DurableMove { [DllImport("kernel32.dll", CharSet=CharSet.Unicode, SetLastError=true)] public static extern bool MoveFileEx(string from, string to, int flags); }'`,
      'if (![DurableMove]::MoveFileEx($env:SHUFFLE_MOVE_FROM, $env:SHUFFLE_MOVE_TO, 9)) { throw (New-Object ComponentModel.Win32Exception) }',
    ].join('; ');
    await promisify(execFile)('powershell.exe', ['-NoProfile', '-NonInteractive', '-Command', script], {
      env: { ...process.env, SHUFFLE_MOVE_FROM: from, SHUFFLE_MOVE_TO: to }, windowsHide: true, timeout: 30_000,
    });
  } else {
    await fs.rename(from, to);
    await syncDirectory(path.dirname(to));
    if (path.dirname(from) !== path.dirname(to)) await syncDirectory(path.dirname(from));
  }
}
export async function durableJson(file: string, value: unknown): Promise<void> {
  const fd = await fs.open(file + '.tmp', 'w', 0o600);
  try { await fd.writeFile(JSON.stringify(value, null, 2) + '\n'); await fd.sync(); }
  finally { await fd.close(); }
  await durableRename(file + '.tmp', file);
}
async function digest(file: string): Promise<string> {
  const hash = createHash('sha256');
  for await (const chunk of createReadStream(file)) hash.update(chunk);
  return hash.digest('hex');
}
async function inventory(root: string): Promise<Record<string, string>> {
  const result: Record<string, string> = {};
  async function walk(relative: string): Promise<void> {
    const file = path.join(root, relative);
    const stat = await fs.lstat(file);
    if (stat.isSymbolicLink()) throw new Error('Database restore points do not support symbolic links or external tablespaces');
    if (stat.isDirectory()) {
      result[relative + '/'] = 'directory';
      for (const name of (await fs.readdir(file)).sort()) await walk(path.join(relative, name));
    } else if (stat.isFile()) result[relative] = await digest(file);
    else throw new Error('Unsupported database file');
  }
  await walk('');
  if (!result.PG_VERSION || !result[path.join('global', 'pg_control')] || result['postmaster.pid']) throw new Error('Not a stopped managed PostgreSQL cluster');
  return result;
}
async function copyTree(source: string, target: string): Promise<void> {
  const stat = await fs.lstat(source);
  if (stat.isSymbolicLink()) throw new Error('Database links are not supported');
  if (stat.isDirectory()) {
    await fs.mkdir(target, { mode: 0o700 });
    for (const name of await fs.readdir(source)) await copyTree(path.join(source, name), path.join(target, name));
    await syncDirectory(target);
  } else if (stat.isFile()) {
    await fs.copyFile(source, target);
    const fd = await fs.open(target, 'r+');
    try { await fd.sync(); } finally { await fd.close(); }
  } else throw new Error('Unsupported database file');
}
export interface RollbackDatabase {
  validate(): Promise<void>;
  snapshot(): Promise<void>;
  restore(): Promise<void>;
  discard(): Promise<void>;
}
/** Fixed sibling paths bound space to one restore point, one failed cluster and one restore staging copy. */
export class ColdClusterCopy implements RollbackDatabase {
  private readonly snapshotDir: string;
  private readonly stage: string;
  private readonly discarded: string;
  constructor(private readonly root: string, private readonly quiesce: (snapshot: boolean) => Promise<void>) {
    this.snapshotDir = path.join(root, 'update-snapshot');
    this.stage = path.join(root, 'update-restore');
    this.discarded = path.join(root, 'update-failed');
  }
  async snapshot(): Promise<void> {
    await this.quiesce(true);
    await privateDirectory(this.root);
    // The journal owns these paths; a prior snapshot is never overwritten implicitly.
    await fs.mkdir(this.snapshotDir, { mode: 0o700 });
    const source = path.join(this.root, 'data');
    const before = await inventory(source);
    await copyTree(source, path.join(this.snapshotDir, 'data'));
    const after = await inventory(path.join(this.snapshotDir, 'data'));
    if (JSON.stringify(before) !== JSON.stringify(after) || JSON.stringify(before) !== JSON.stringify(await inventory(source))) throw new Error('Database changed while taking its restore point');
    await durableJson(path.join(this.snapshotDir, 'manifest.json'), before);
    await syncDirectory(this.root);
  }
  async validate(): Promise<void> {
    const expected = JSON.parse(await fs.readFile(path.join(this.snapshotDir, 'manifest.json'), 'utf8'));
    if (JSON.stringify(expected) !== JSON.stringify(await inventory(path.join(this.snapshotDir, 'data')))) throw new Error('Database restore point failed validation');
  }
  async restore(): Promise<void> {
    await this.quiesce(false);
    await this.validate();
    const source = path.join(this.snapshotDir, 'data');
    const expected = JSON.parse(await fs.readFile(path.join(this.snapshotDir, 'manifest.json'), 'utf8'));
    await fs.rm(this.stage, { recursive: true, force: true });
    await copyTree(source, this.stage);
    if (JSON.stringify(expected) !== JSON.stringify(await inventory(this.stage))) throw new Error('Restored database failed validation');
    const data = path.join(this.root, 'data');
    // Repeating any interrupted rename is safe: the immutable snapshot remains until state/floor promotion.
    await fs.rm(this.discarded, { recursive: true, force: true });
    let present = true;
    try { await fs.lstat(data); } catch (error) { if ((error as NodeJS.ErrnoException).code !== 'ENOENT') throw error; present = false; }
    if (present) await durableRename(data, this.discarded);
    await syncDirectory(this.root);
    await durableRename(this.stage, data);
    await syncDirectory(this.root);
  }
  async discard(): Promise<void> {
    for (const directory of [this.snapshotDir, this.stage, this.discarded]) await fs.rm(directory, { recursive: true, force: true });
    await syncDirectory(this.root);
  }
}
