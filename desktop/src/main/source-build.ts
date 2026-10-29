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
  pythonDependencies: string | null;
}
export interface BuiltSource { revision: string; pythonKey: string | null; electron: string }
export interface BuildOptions {
  source: string;
  server: string;
  cache: string;
  runtime: SourceRuntime;
  /**
   * Runtime-independent cache root (updates/dependencies). npm dependency caches
   * live here, keyed by the Node/npm build and the package files, so a new
   * native runtime with the same Node keeps them. Without it they stay in `cache`.
   */
  sharedCache?: string;
  revision: string;
  version?: string;
  /** A bundle already verified against this signed revision and build version. */
  prebuiltUI?: string;
  log: string;
  progress: (message: string) => void;
}

async function exists(file: string): Promise<boolean> { try { await fs.access(file); return true; } catch { return false; } }
async function fingerprint(root: string, names: string[], text = (_name: string, content: string) => content): Promise<string> {
  const hash = createHash('sha256');
  for (const name of names) hash.update(name + '\0' + text(name, (await fs.readFile(path.join(root, name), 'utf8')).replace(/\r\n/g, '\n')) + '\0');
  return hash.digest('hex');
}
export function buildEnvironment(node: string, python: string): NodeJS.ProcessEnv {
  const env: NodeJS.ProcessEnv = {};
  // Also the source-managed app's environment, and so its worker's: keep the
  // user's own Claude Code config dir so `claude` finds the user's login (D5).
  for (const [key, value] of Object.entries(process.env)) {
    if (/^(path|systemroot|windir|comspec|pathext|userprofile|home|appdata|localappdata|temp|tmp|programfiles|programfiles\(x86\)|programdata|processor_architecture|number_of_processors|claude_config_dir|display|wayland_display|xdg_runtime_dir|xdg_config_home|xdg_data_home|xdg_cache_home|xdg_session_type|dbus_session_bus_address|xauthority|lang|lc_[a-z_]+|user|logname)$/i.test(key)) env[key] = value;
  }
  const pathKey = Object.keys(env).find(k => k.toLowerCase() === 'path') ?? 'PATH';
  env[pathKey] = [path.dirname(node), path.dirname(python), env[pathKey] ?? ''].join(path.delimiter);
  return { ...env, CI: 'true', ELECTRON_SKIP_BINARY_DOWNLOAD: '1', PIP_DISABLE_PIP_VERSION_CHECK: '1', PYTHONNOUSERSITE: '1' };
}
async function run(executable: string, args: string[], cwd: string, opts: BuildOptions, python = opts.runtime.python, electronDownload = false): Promise<void> {
  await fs.mkdir(path.dirname(opts.log), { recursive: true });
  const log = await fs.open(opts.log, 'a');
  const started = Date.now();
  // Timestamps and durations make each step's cost visible in build.log (#430).
  const finish = (result: string) => log.write(`[${new Date().toISOString()}] ${result} after ${((Date.now() - started) / 1000).toFixed(1)}s\n`);
  try {
    await log.write(`\n[${new Date(started).toISOString()}] $ ${path.basename(executable)} ${args.join(' ')}\n`);
    await new Promise<void>((resolve, reject) => {
      const env: NodeJS.ProcessEnv = { ...buildEnvironment(opts.runtime.node, python), GITHUB_SHA: opts.revision };
      if (opts.version) env.APP_VERSION = opts.version;
      if (electronDownload) delete env.ELECTRON_SKIP_BINARY_DOWNLOAD;
      const child = spawn(executable, args, { cwd, env, windowsHide: true, stdio: ['ignore', log.fd, log.fd], timeout: 15 * 60_000 });
      child.once('error', reject);
      child.once('exit', (code, signal) => code === 0 ? resolve() : reject(new Error(`${path.basename(executable)} failed (${signal ?? code}). Build log: ${opts.log}`)));
    }).then(() => finish('done'), async (error) => { await finish('failed'); throw error; });
  } finally { await log.close(); }
}

/**
 * The optional voice installer. -P keeps the script's folder (app/core) off
 * sys.path, where app/core/platform.py would shadow the standard library (#429).
 */
export const VOICE_INSTALL = ['-P', 'app/core/voice_dependencies.py', '--install'];

/** The files that decide the Python environment; desktop/scripts/runtime-contract.mjs hashes the same list. */
export const PYTHON_INPUTS = ['requirements.lock', 'requirements-native.lock', 'requirements-voice.lock', 'fluid/pyproject.toml'];

/**
 * pip arguments for the Python environment, the same way the container
 * updater installs it (docker/application_updates.py): every package, including
 * fluid's build backend, must match a hash in the signed source's lock files,
 * so a hostile or compromised index release cannot slip in.
 */
export function pythonInstallSteps(): string[][] {
  // pip's 15-second read timeout can drop a large wheel on a slow link.
  const pip = ['-m', 'pip', 'install', '--prefer-binary', '--timeout', '120', '--retries', '10'];
  return [
    [...pip, '--require-hashes', '-r', 'requirements.lock'],
    // The signed source carries a metadata-corrected, hash-pinned pynput wheel.
    [...pip, '--find-links', 'desktop/vendor', '--only-binary=pynput,evdev-binary', '--require-hashes', '-r', 'requirements-native.lock'],
    // This helper fails closed for voice, not for the app installation.
    VOICE_INSTALL,
    [...pip, '--no-deps', '--no-build-isolation', './fluid'],
  ];
}

/**
 * The part of a Python input that decides the environment. Fluid's test-runner
 * settings ([tool.pytest*] tables in fluid/pyproject.toml) do not change what
 * pip installs, so they are left out. Any other table, including one this rule
 * does not know, still counts. A file with a multi-line string counts whole, so
 * text inside a string can never hide a table. desktop/scripts/runtime-contract.mjs
 * applies the same rule.
 */
export function pythonInputText(name: string, text: string): string {
  if (name !== 'fluid/pyproject.toml' || text.includes('"""') || text.includes("'''")) return text;
  let skip = false;
  return text.split('\n').filter(line => {
    if (/^\s*\[/.test(line)) skip = /^\s*\[\s*tool\.pytest(\.[A-Za-z0-9_-]+)*\s*\]\s*(#.*)?$/.test(line);
    return !skip;
  }).join('\n');
}

/** Matches the bundled runtime's manifest.pythonDependencies when the same locks built it. */
export function pythonDependencyKey(source: string): Promise<string> { return fingerprint(source, PYTHON_INPUTS, pythonInputText); }
/** The key runtimes built before pythonInputText recorded: every input file whole. */
function legacyPythonDependencyKey(source: string): Promise<string> { return fingerprint(source, PYTHON_INPUTS); }

async function installOptionalVoice(python: string, opts: BuildOptions): Promise<boolean> {
  try {
    await run(python, VOICE_INSTALL, opts.source, opts, python);
    return true;
  } catch {
    opts.progress('Optional ChatGPT voice libraries could not be installed; a later call or build can retry.');
    return false;
  }
}

async function pythonRuntime(opts: BuildOptions): Promise<string | null> {
  for (const lock of ['requirements.lock', 'requirements-native.lock']) {
    if (!(await exists(path.join(opts.source, lock)))) throw new Error(`This update has no hash-pinned ${lock}; it cannot be built safely. The running version is unchanged.`);
  }
  const key = await pythonDependencyKey(opts.source);
  // Either key is a hash of these very inputs, so a match means the runtime was installed from them.
  if (key === opts.runtime.pythonDependencies || await legacyPythonDependencyKey(opts.source) === opts.runtime.pythonDependencies) return null;
  const target = path.join(opts.cache, 'python', key.slice(0, 24));
  if (await exists(path.join(target, '.complete'))) return key;
  if (await exists(path.join(target, '.base-complete'))) {
    const python = path.join(target, process.platform === 'win32' ? 'Scripts/python.exe' : 'bin/python');
    if (await installOptionalVoice(python, opts)) await fs.writeFile(path.join(target, '.complete'), key);
    return key; // Main dependencies remain usable even when the optional install fails.
  }
  opts.progress('Installing updated Python dependencies. The running application is unchanged.');
  const temporary = `${target}.tmp-${randomUUID().slice(0, 8)}`;
  await fs.mkdir(path.dirname(target), { recursive: true });
  try {
    await run(opts.runtime.python, ['-m', 'venv', temporary], opts.source, opts);
    const python = path.join(temporary, process.platform === 'win32' ? 'Scripts/python.exe' : 'bin/python');
    let voiceInstalled = false;
    for (const args of pythonInstallSteps()) {
      if (args === VOICE_INSTALL) voiceInstalled = await installOptionalVoice(python, opts);
      else await run(python, args, opts.source, opts, python);
    }
    await fs.writeFile(path.join(temporary, '.base-complete'), key);
    if (voiceInstalled) await fs.writeFile(path.join(temporary, '.complete'), key);
    await fs.rename(temporary, target);
    return key;
  } finally { await fs.rm(temporary, { recursive: true, force: true }); }
}

/**
 * The build of Node and npm that `npm ci` runs with: version, platform, arch,
 * native module ABI and the bundled npm's version. null if it cannot be read,
 * which keeps that build's npm caches inside its runtime's cache.
 */
async function nodeIdentity(opts: BuildOptions): Promise<string | null> {
  try {
    const npm = JSON.parse(await fs.readFile(path.resolve(path.dirname(opts.runtime.npm), '..', 'package.json'), 'utf8')).version;
    const node = await new Promise<string>((resolve, reject) => {
      const child = spawn(opts.runtime.node, ['-p', '[process.version, process.platform, process.arch, process.versions.modules].join(" ")'],
        { env: buildEnvironment(opts.runtime.node, opts.runtime.python), windowsHide: true, stdio: ['ignore', 'pipe', 'ignore'], timeout: 60_000 });
      let output = '';
      child.stdout!.on('data', (chunk: Buffer) => { output += chunk.toString(); });
      child.once('error', reject);
      child.once('exit', code => code === 0 && output.trim() ? resolve(output.trim()) : reject(new Error('node identity unavailable')));
    });
    if (typeof npm !== 'string' || !npm) return null;
    return `${node} npm ${npm}`;
  } catch { return null; }
}

/** npm ci uses --ignore-scripts. Keep all installation-affecting/unknown fields. */
export function nodeDependencyInputText(name: string, text: string): string {
  if (name !== 'package.json') return text;
  const manifest = JSON.parse(text);
  for (const field of ['scripts', 'description', 'keywords', 'homepage', 'bugs', 'repository', 'license', 'author', 'contributors', 'funding']) delete manifest[field];
  return JSON.stringify(Object.fromEntries(Object.keys(manifest).sort().map(key => [key, manifest[key]])));
}
function reuseNodeBuilds(): boolean {
  return !['0', 'false', 'off'].includes((process.env.AI_SHUFFLE_PERF_DESKTOP_BUILD_REUSE ?? '').trim().toLowerCase());
}

/** These three builds consume their own package tree and root JSON configs,
 * not the app revision/version. Hash bytes, scripts, compiler config, lock and
 * Node/npm/ABI together. Do not apply this contract to the version-stamped UI. */
async function nodeTree(directory: string, inputs = false): Promise<string> {
  const hash = createHash('sha256'); let count = 0;
  async function visit(relative: string): Promise<void> {
    for (const entry of (await fs.readdir(path.join(directory, relative), { withFileTypes: true })).sort((a, b) => a.name.localeCompare(b.name))) {
      if (inputs && ['node_modules', 'dist', '.git'].includes(entry.name)) continue;
      const name = relative ? relative + '/' + entry.name : entry.name;
      if (entry.isSymbolicLink() || (!entry.isFile() && !entry.isDirectory())) throw new Error('Non-regular build input/output');
      if (entry.isDirectory()) await visit(name);
      else { hash.update(name + '\0' + createHash('sha256').update(await fs.readFile(path.join(directory, name))).digest('hex') + '\n'); count++; }
    }
  }
  await visit('');
  if (!count) throw new Error('Empty build input/output');
  return hash.digest('hex');
}
async function nodeOutputKey(name: string, opts: BuildOptions, identity: string, dependencies: string): Promise<string> {
  const hash = createHash('sha256').update(`node-build-1\0${name}\0${identity}\0${dependencies}\0`);
  hash.update(await nodeTree(path.join(opts.source, name), true));
  for (const entry of (await fs.readdir(opts.source, { withFileTypes: true })).sort((a, b) => a.name.localeCompare(b.name))) {
    if (entry.isFile() && (entry.name.endsWith('.json') || entry.name === '.npmrc')) hash.update(entry.name + '\0').update(await fs.readFile(path.join(opts.source, entry.name))).update('\0');
  }
  return hash.digest('hex');
}
async function reuseNodeOutput(cache: string, key: string, output: string): Promise<boolean> {
  try {
    const marker = JSON.parse(await fs.readFile(path.join(cache, '.complete'), 'utf8'));
    if (marker.key !== key || marker.tree !== await nodeTree(path.join(cache, 'dist'))) return false;
    await fs.rm(output, { recursive: true, force: true });
    await fs.cp(path.join(cache, 'dist'), output, { recursive: true });
    if (await nodeTree(output) !== marker.tree) throw new Error('Build output changed during reuse');
    return true;
  } catch { return false; }
}
async function publishNodeOutput(root: string, key: string, output: string): Promise<void> {
  const temporary = path.join(root, `${key}.tmp-${randomUUID()}`), target = path.join(root, key);
  await fs.mkdir(temporary, { recursive: true });
  try {
    await fs.cp(output, path.join(temporary, 'dist'), { recursive: true });
    const tree = await nodeTree(path.join(temporary, 'dist'));
    await fs.writeFile(path.join(temporary, '.complete'), JSON.stringify({ key, tree }));
    // A corrupt/incomplete entry cannot be reused, even if it has this key.
    await fs.rm(target, { recursive: true, force: true });
    await fs.rename(temporary, target);
    const entries = [];
    for (const entry of await fs.readdir(root, { withFileTypes: true })) {
      if (!entry.isDirectory() || entry.isSymbolicLink() || !/^[a-f0-9]{64}$/.test(entry.name)) continue;
      const file = path.join(root, entry.name);
      entries.push({ file, time: (await fs.stat(file)).mtimeMs });
    }
    entries.sort((a, b) => b.time - a.time);
    // Staged/running outputs are copies, never links into this bounded cache.
    for (const entry of entries.slice(2)) await fs.rm(entry.file, { recursive: true, force: true });
  } finally { await fs.rm(temporary, { recursive: true, force: true }); }
}

async function nodePackage(name: string, opts: BuildOptions, python: string, identity: string | null): Promise<string> {
  const source = path.join(opts.source, name);
  const files = await fingerprint(source, ['package.json', 'package-lock.json'], reuseNodeBuilds() ? nodeDependencyInputText : undefined);
  // A shared cache is used only for a known Node/npm build; the key covers it and the package files.
  const shared = opts.sharedCache && identity;
  const key = shared ? createHash('sha256').update(`node-dependencies-1\0${identity}\0${files}`).digest('hex') : files;
  const cache = path.join(shared ? opts.sharedCache! : opts.cache, 'node', name, key.slice(0, 24));
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
  const output = path.join(source, 'dist');
  const outputRoot = path.join(opts.sharedCache ?? opts.cache, 'builds', name);
  const outputKey = reuseNodeBuilds() && identity && ['tools', 'browser-mcp', 'desktop'].includes(name)
    ? await nodeOutputKey(name, opts, identity, files) : null;
  if (outputKey && await reuseNodeOutput(path.join(outputRoot, outputKey), outputKey, output)) {
    opts.progress(`Reusing verified ${name} build output.`);
  } else {
    opts.progress(`Building ${name} from source…`);
    await run(opts.runtime.node, [opts.runtime.npm, 'run', 'build'], source, opts, python);
    if (outputKey && outputKey === await nodeOutputKey(name, opts, identity!, files)) {
      try { await publishNodeOutput(outputRoot, outputKey, output); }
      catch { opts.progress(`Built ${name}; its output could not be cached.`); }
    }
  }
  return dependencies;
}

export async function buildSource(opts: BuildOptions): Promise<BuiltSource> {
  const pythonKey = await pythonRuntime(opts);
  const python = pythonKey ? path.join(opts.cache, 'python', pythonKey.slice(0, 24), process.platform === 'win32' ? 'Scripts/python.exe' : 'bin/python') : opts.runtime.python;
  const dependencies = new Map<string, string>();
  const identity = opts.sharedCache ? await nodeIdentity(opts) : null;
  for (const name of [...(opts.prebuiltUI ? [] : ['frontend']), 'tools', 'browser-mcp', 'desktop']) dependencies.set(name, await nodePackage(name, opts, python, identity));
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
  // Explicit call starts can retry this signed, hash-pinned optional closure.
  await fs.copyFile(path.join(opts.source, 'requirements-voice.lock'), path.join(opts.server, 'requirements-voice.lock'));
  await fs.rm(path.join(opts.server, 'app/static'), { recursive: true, force: true });
  await fs.cp(opts.prebuiltUI ?? path.join(opts.source, 'frontend/build'), path.join(opts.server, 'app/static'), { recursive: true });
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
  // mobile/node_modules is the phone UI build's link to frontend/node_modules.
  for (const name of ['frontend', 'mobile', 'tools', 'browser-mcp', 'desktop']) {
    for (const base of ['source', 'server']) {
      const link = path.join(root, base, name, 'node_modules');
      try { if ((await fs.lstat(link)).isSymbolicLink()) await fs.unlink(link); }
      catch (error) { if ((error as NodeJS.ErrnoException).code !== 'ENOENT') throw error; }
    }
  }
  await fs.rm(root, { recursive: true, force: true });
}
