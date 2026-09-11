/** Build an immutable GitHub source snapshot using installed runtimes and cached dependencies. */
import { createHash, randomUUID } from 'node:crypto';
import { spawn } from 'node:child_process';
import { promises as fs } from 'node:fs';
import path from 'node:path';

export interface SourceRuntime {
  root: string;
  python: string;
  node: string;
  npm: string;
  pythonDependencies: string;
}
export interface BuiltSource { revision: string; pythonKey: string | null; electron: string }
export interface BuildOptions {
  source: string;
  server: string;
  cache: string;
  runtime: SourceRuntime;
  revision: string;
  log: string;
  progress: (message: string) => void;
}

async function exists(file: string): Promise<boolean> { try { await fs.access(file); return true; } catch { return false; } }
async function fingerprint(root: string, names: string[]): Promise<string> {
  const hash = createHash('sha256');
  for (const name of names) hash.update(name + '\0' + (await fs.readFile(path.join(root, name), 'utf8')).replace(/\r\n/g, '\n') + '\0');
  return hash.digest('hex');
}
export function buildEnvironment(node: string, python: string): NodeJS.ProcessEnv {
  const env: NodeJS.ProcessEnv = {};
  for (const [key, value] of Object.entries(process.env)) {
    if (/^(path|systemroot|windir|comspec|pathext|userprofile|home|appdata|localappdata|temp|tmp|programfiles|programfiles\(x86\)|programdata|processor_architecture|number_of_processors)$/i.test(key)) env[key] = value;
  }
  const pathKey = Object.keys(env).find(k => k.toLowerCase() === 'path') ?? 'PATH';
  env[pathKey] = [path.dirname(node), path.dirname(python), env[pathKey] ?? ''].join(path.delimiter);
  return { ...env, CI: 'true', ELECTRON_SKIP_BINARY_DOWNLOAD: '1', PIP_DISABLE_PIP_VERSION_CHECK: '1', PYTHONNOUSERSITE: '1' };
}
async function run(executable: string, args: string[], cwd: string, opts: BuildOptions, python = opts.runtime.python, electronDownload = false): Promise<void> {
  await fs.mkdir(path.dirname(opts.log), { recursive: true });
  const log = await fs.open(opts.log, 'a');
  try {
    await log.write(`\n$ ${path.basename(executable)} ${args.join(' ')}\n`);
    await new Promise<void>((resolve, reject) => {
      const env: NodeJS.ProcessEnv = { ...buildEnvironment(opts.runtime.node, python), GITHUB_SHA: opts.revision };
      if (electronDownload) delete env.ELECTRON_SKIP_BINARY_DOWNLOAD;
      const child = spawn(executable, args, { cwd, env, windowsHide: true, stdio: ['ignore', log.fd, log.fd], timeout: 15 * 60_000 });
      child.once('error', reject);
      child.once('exit', (code, signal) => code === 0 ? resolve() : reject(new Error(`${path.basename(executable)} failed (${signal ?? code}). Build log: ${opts.log}`)));
    });
  } finally { await log.close(); }
}

async function pythonRuntime(opts: BuildOptions): Promise<string | null> {
  const key = await fingerprint(opts.source, ['requirements.txt', 'requirements-native.txt', 'fluid/pyproject.toml']);
  if (key === opts.runtime.pythonDependencies) return null;
  const target = path.join(opts.cache, 'python', key.slice(0, 24));
  if (await exists(path.join(target, '.complete'))) return key;
  opts.progress('Installing updated Python dependencies. The running application is unchanged.');
  const temporary = `${target}.tmp-${randomUUID().slice(0, 8)}`;
  await fs.mkdir(path.dirname(target), { recursive: true });
  try {
    await run(opts.runtime.python, ['-m', 'venv', temporary], opts.source, opts);
    const python = path.join(temporary, process.platform === 'win32' ? 'Scripts/python.exe' : 'bin/python');
    await run(python, ['-m', 'pip', 'install', '--prefer-binary', '-r', 'requirements.txt', '-r', 'requirements-native.txt', './fluid'], opts.source, opts, python);
    await fs.writeFile(path.join(temporary, '.complete'), key);
    await fs.rename(temporary, target);
    return key;
  } finally { await fs.rm(temporary, { recursive: true, force: true }); }
}

async function nodePackage(name: string, opts: BuildOptions, python: string): Promise<string> {
  const source = path.join(opts.source, name);
  const key = await fingerprint(source, ['package.json', 'package-lock.json']);
  const cache = path.join(opts.cache, 'node', name, key.slice(0, 24));
  if (!(await exists(path.join(cache, '.complete')))) {
    opts.progress(`Installing ${name} dependencies. They will be reused for later updates.`);
    const temporary = `${cache}.tmp-${randomUUID().slice(0, 8)}`;
    await fs.mkdir(temporary, { recursive: true });
    try {
      for (const file of ['package.json', 'package-lock.json']) await fs.copyFile(path.join(source, file), path.join(temporary, file));
      await run(opts.runtime.node, [opts.runtime.npm, 'ci', '--ignore-scripts', '--no-audit', '--no-fund'], temporary, opts, python);
      await fs.writeFile(path.join(temporary, '.complete'), key);
      await fs.mkdir(path.dirname(cache), { recursive: true });
      await fs.rename(temporary, cache);
    } finally { await fs.rm(temporary, { recursive: true, force: true }); }
  }
  const dependencies = path.join(cache, 'node_modules');
  await fs.symlink(dependencies, path.join(source, 'node_modules'), process.platform === 'win32' ? 'junction' : 'dir');
  opts.progress(`Building ${name} from source…`);
  await run(opts.runtime.node, [opts.runtime.npm, 'run', 'build'], source, opts, python);
  return dependencies;
}

export async function buildSource(opts: BuildOptions): Promise<BuiltSource> {
  const pythonKey = await pythonRuntime(opts);
  const python = pythonKey ? path.join(opts.cache, 'python', pythonKey.slice(0, 24), process.platform === 'win32' ? 'Scripts/python.exe' : 'bin/python') : opts.runtime.python;
  const dependencies = new Map<string, string>();
  for (const name of ['frontend', 'tools', 'browser-mcp', 'desktop']) dependencies.set(name, await nodePackage(name, opts, python));
  const electronPackage = path.join(dependencies.get('desktop')!, 'electron');
  const electron = path.join(electronPackage, 'dist', process.platform === 'win32' ? 'electron.exe' : 'electron');
  if (!(await exists(electron))) {
    opts.progress('Downloading the desktop runtime from Electron…');
    await run(opts.runtime.node, [path.join(electronPackage, 'install.js')], electronPackage, opts, python, true);
  }
  opts.progress('Preparing application files…');
  await fs.mkdir(opts.server, { recursive: true });
  const clean = (src: string) => !src.split(path.sep).includes('__pycache__') && !src.endsWith('.pyc');
  await fs.cp(path.join(opts.source, 'app'), path.join(opts.server, 'app'), { recursive: true, filter: clean });
  await fs.rm(path.join(opts.server, 'app/static'), { recursive: true, force: true });
  await fs.cp(path.join(opts.source, 'frontend/build'), path.join(opts.server, 'app/static'), { recursive: true });
  await fs.cp(path.join(opts.source, 'fluid/fluid'), path.join(opts.server, 'fluid'), { recursive: true, filter: clean });
  for (const name of ['tools', 'browser-mcp']) {
    const target = path.join(opts.server, name);
    await fs.mkdir(target, { recursive: true });
    await fs.cp(path.join(opts.source, name, 'dist'), path.join(target, 'dist'), { recursive: true });
    await fs.copyFile(path.join(opts.source, name, 'package.json'), path.join(target, 'package.json'));
    await fs.symlink(dependencies.get(name)!, path.join(target, 'node_modules'), process.platform === 'win32' ? 'junction' : 'dir');
  }
  const cloudCache = path.join(opts.cache, 'cloudflared');
  if (await exists(cloudCache)) await fs.cp(cloudCache, path.join(opts.server, 'downloads'), { recursive: true });
  opts.progress('Preparing the verified remote-access runtime…');
  await run(python, ['-B', '-m', 'app.core.cloudflared', '--bundle', '--reuse-verified'], opts.server, opts, python);
  await fs.cp(path.join(opts.server, 'downloads'), cloudCache, { recursive: true });
  return { revision: opts.revision, pythonKey, electron };
}

/** Detach our dependency junctions before deleting a staged/old version. */
export async function removeSourceDirectory(root: string): Promise<void> {
  for (const name of ['frontend', 'tools', 'browser-mcp', 'desktop']) {
    for (const base of ['source', 'server']) {
      const link = path.join(root, base, name, 'node_modules');
      try { if ((await fs.lstat(link)).isSymbolicLink()) await fs.unlink(link); }
      catch (error) { if ((error as NodeJS.ErrnoException).code !== 'ENOENT') throw error; }
    }
  }
  await fs.rm(root, { recursive: true, force: true });
}
