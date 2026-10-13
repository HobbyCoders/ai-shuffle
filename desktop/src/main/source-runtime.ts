import { createHash, randomUUID } from 'node:crypto';
import { promises as fs } from 'node:fs';
import path from 'node:path';
import { spawn } from 'node:child_process';
import { buildEnvironment, type SourceRuntime } from './source-build';

export async function prepareNativeRuntime(source: string, root: string, bootstrapNode: string, progress: (message: string) => void): Promise<{ key: string; runtime: SourceRuntime }> {
  if (!['win32', 'linux'].includes(process.platform) || process.arch !== 'x64') throw new Error('Source-managed desktop currently supports Windows and Linux x64');
  const hash = createHash('sha256');
  for (const file of ['desktop/runtime-contract.json', 'desktop/scripts/build-worker.mjs', 'desktop/scripts/postgres-binaries.mjs']) hash.update(file + '\0' + (await fs.readFile(path.join(source, file), 'utf8')).replace(/\r\n/g, '\n'));
  const key = hash.digest('hex');
  // Windows DLL loading still has path limits even when Node supports long
  // paths. Keep native caches compact; the full fingerprint stays in build.json.
  const target = path.join(root, 'runtimes', key.slice(0, 24));
  const manifestFile = path.join(target, 'worker/manifest.json');
  try { await fs.access(manifestFile); }
  catch {
    progress('Downloading and preparing Python, Node and PostgreSQL. This only runs when those runtimes change.');
    const temporary = path.join(root, 'runtimes', `build-${randomUUID().slice(0, 8)}`);
    await fs.mkdir(temporary, { recursive: true });
    const log = await fs.open(path.join(root, 'build.log'), 'a');
    try {
      await new Promise<void>((resolve, reject) => {
        const child = spawn(bootstrapNode, [path.join(source, 'desktop/scripts/build-worker.mjs'), '--runtime-only', '--python-from', path.resolve(path.dirname(bootstrapNode), process.platform === 'win32' ? '..' : '../..'), '--out', path.join(temporary, 'worker'), '--postgres-out', path.join(temporary, 'postgres'), '--cache', path.join(root, 'downloads')], { cwd: source, env: buildEnvironment(bootstrapNode, 'python'), windowsHide: true, stdio: ['ignore', log.fd, log.fd], timeout: 20 * 60_000 });
        child.once('error', reject);
        child.once('exit', (code, signal) => code === 0 ? resolve() : reject(new Error(`Native runtime setup failed (${signal ?? code}). See ${path.join(root, 'build.log')}`)));
      });
      await fs.rename(temporary, target);
    } finally { await log.close(); await fs.rm(temporary, { recursive: true, force: true }); }
  }
  const manifest = JSON.parse(await fs.readFile(manifestFile, 'utf8'));
  const windows = process.platform === 'win32';
  return { key, runtime: { root: path.join(target, 'worker'),
    python: path.join(target, windows ? 'worker/python/python.exe' : 'worker/python/bin/python3'),
    node: path.join(target, windows ? 'worker/node/node.exe' : 'worker/node/bin/node'),
    npm: path.join(target, windows ? 'worker/node/node_modules/npm/bin/npm-cli.js' : 'worker/node/lib/node_modules/npm/bin/npm-cli.js'),
    pythonDependencies: manifest.pythonDependencies } };
}
