// Refresh only the bootstrap code, never the source install's state or user data.
import { cp, lstat, mkdir, mkdtemp, readFile, rename, rm } from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { randomUUID } from 'node:crypto';
import { readLauncher } from './launcher-contract.mjs';

const files = ['src', 'scripts', 'build', 'package.json', 'package-lock.json', 'tsconfig.json', 'runtime-contract.json', 'launcher.json'];
async function exists(file) {
  try { await lstat(file); return true; }
  catch (error) { if (error.code === 'ENOENT') return false; throw error; }
}

export async function refreshStarter(desktop, root) {
  desktop = path.resolve(desktop);
  root = path.resolve(root);
  const starter = path.join(root, 'starter');
  const shipped = readLauncher(await readFile(path.join(desktop, 'launcher.json'), 'utf8')).version;
  const stamp = path.join(starter, 'launcher.json');
  // Older bootstrap installers did not copy launcher.json at all.
  const installed = await exists(stamp) ? readLauncher(await readFile(stamp, 'utf8')).version : 0;
  if (installed >= shipped && await exists(path.join(starter, 'scripts/bootstrap.mjs'))) return false;
  if (desktop === starter) throw new Error('Run the bootstrap installer from the newer downloaded source, not the installed starter');
  await mkdir(root, { recursive: true });
  const staged = await mkdtemp(path.join(root, '.starter-'));
  let backup;
  try {
    // A fresh directory avoids stale compiled code and nested directory copies.
    for (const name of files) await cp(path.join(desktop, name), path.join(staged, name), { recursive: true });
    if (await exists(starter)) {
      backup = path.join(root, `starter-backup-${randomUUID()}`);
      await rename(starter, backup);
    }
    try { await rename(staged, starter); }
    catch (error) {
      if (backup) await rename(backup, starter);
      throw error;
    }
    // Keep the old starter for recovery; state.json, versions and data stay put.
    return true;
  } finally {
    await rm(staged, { recursive: true, force: true });
  }
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  if (process.argv.length !== 3) throw new Error('Usage: node refresh-starter.mjs <install-root>');
  await refreshStarter(fileURLToPath(new URL('..', import.meta.url)), process.argv[2]);
}
