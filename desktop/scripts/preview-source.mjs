// Source-only preview using the existing private PostgreSQL and worker supervisors.
// Build desktop/ first. Configuration comes from environment; never copy a profile.
import { execFile } from 'node:child_process';
import { promisify } from 'node:util';
import { promises as fs, watch } from 'node:fs';
import path from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { createRequire } from 'node:module';
const require = createRequire(import.meta.url);
const { PostgresSupervisor, resolvePostgresBinaries } = require('../dist/main/postgres.js');
const { WorkerSupervisor, buildWorkerEnv, isPortFree } = require('../dist/main/worker.js');
const { privateDirectory } = require('../dist/main/private-file.js');
const exec = promisify(execFile);
const sourceRoot = fileURLToPath(new URL('../..', import.meta.url));

export function previewOptions(env = process.env, repo = sourceRoot) {
  const required = name => {
    const value = env[name];
    if (!value || !path.isAbsolute(value)) throw new Error(`${name} must be an explicit absolute path`);
    return path.resolve(value);
  };
  const root = required('SHUFFLE_PREVIEW_ROOT');
  const python = required('SHUFFLE_PREVIEW_PYTHON');
  const port = Number(env.SHUFFLE_PREVIEW_PORT ?? 18000);
  const protectedPort = Number(env.SHUFFLE_PREVIEW_PROTECTED_PORT ?? 18001);
  if (![port, protectedPort].every(p => Number.isInteger(p) && p > 0 && p <= 65535) || port === protectedPort) {
    throw new Error('Preview listener ports must be valid and different');
  }
  const relative = path.relative(repo, root);
  if (!relative || (relative !== '..' && !relative.startsWith('..' + path.sep) && !path.isAbsolute(relative))) {
    throw new Error('Preview profile must be outside the checkout');
  }
  return {
    root,
    runtime: { mode: 'repo', root: repo, serverDir: repo, python, nodeBinDir: null,
      toolsDir: path.join(repo, 'tools'), browserMcpEntry: path.join(repo, 'browser-mcp', 'dist', 'index.js'), manifest: null },
    logDir: path.join(root, 'logs'), envFile: path.join(root, 'worker', '.env'),
    dataDir: path.join(root, 'data'), workspaceDir: path.join(root, 'workspace'), profileDir: path.join(root, 'profile'),
    port, protectedPort, allowAttach: false,
    // Fresh previews have no provider/account configuration. Allow only OS
    // process essentials, not arbitrary inherited tokens, proxies or config paths.
    baseEnv: { ...Object.fromEntries(Object.entries(env).filter(([key]) =>
      /^(PATH|SYSTEMROOT|WINDIR|COMSPEC|PATHEXT|TEMP|TMP|LANG|LC_[A-Z_]+|PROCESSOR_ARCHITECTURE|NUMBER_OF_PROCESSORS)$/i.test(key))),
      AI_SHUFFLE_PREVIEW: '1', SHUFFLE_LOCAL_ACCESS: 'true',
      // A checkout can enable Git's detached fsmonitor daemon. Preview Git
      // children must not leave a daemon holding the disposable profile open.
      // Process-local config overrides repository config without changing it.
      GIT_CONFIG_COUNT: '1', GIT_CONFIG_KEY_0: 'core.fsmonitor', GIT_CONFIG_VALUE_0: 'false' },
  };
}

export async function reservePreviewRoot(root, protectDirectory = privateDirectory) {
  // Non-recursive, exclusive mkdir refuses an existing directory or symlink.
  // Keep creation outside the catch: never clean up a root we did not create.
  await fs.mkdir(root, { mode: 0o700 });
  try {
    await protectDirectory(root);
  } catch (cause) {
    let cleanup = 'The empty preview directory was removed.';
    try {
      // No credentials have been written. Never recursively remove a root,
      // even if another process added files while protection was attempted.
      await fs.rmdir(root);
    } catch {
      cleanup = 'The preview directory remains; inspect it before removing it or choose a different new path.';
    }
    const guidance = process.platform === 'win32'
      ? 'Windows may allow directory creation but deny setting its owner or private ACL. Set SHUFFLE_PREVIEW_ROOT to a new, nonexistent child of your user-owned LocalAppData/Temp directory. For a custom parent, check its ownership and ACL permissions.'
      : 'Set SHUFFLE_PREVIEW_ROOT to a new, nonexistent child of a user-owned temporary directory and check the parent permissions.';
    throw new Error(`Could not secure preview root (${root}); preview startup was aborted before writing credentials. ${guidance} Do not relax credential permissions. ${cleanup} Original error: ${cause.message}`, { cause });
  }
}

export function previewUrl(base) {
  return new URL('/', base).href;
}

export async function startPreview(env = process.env) {
  const options = previewOptions(env);
  const binaries = resolvePostgresBinaries({ packaged: false, resourcesPath: '', env });
  if ('error' in binaries) throw new Error(binaries.error);
  for (const port of [options.port, options.protectedPort]) {
    if (!await isPortFree(port)) throw new Error(`Preview port ${port} is in use; no worker was attached`);
  }
  const childEnv = buildWorkerEnv(options);
  // An embedded interpreter can ignore cwd in favor of its installed _pth.
  // Reject it rather than accidentally starting the packaged server.
  const probe = await exec(options.runtime.python, ['-c', 'import app.preview; import fluid.swarm; print(app.preview.__file__)'], {
    cwd: sourceRoot, env: childEnv, timeout: 15000, windowsHide: true,
  });
  if (path.resolve(probe.stdout.trim()) !== path.join(sourceRoot, 'app', 'preview.py')) {
    throw new Error('Python does not import this checkout; choose its full development interpreter');
  }
  await reservePreviewRoot(options.root);
  let credential = null;
  const postgres = new PostgresSupervisor({
    binaries, root: path.join(options.root, 'postgres'), logDir: options.logDir,
    runtime: { env: childEnv },
    credentials: { load: () => credential, save: async value => { credential = value; } },
  });
  const worker = new WorkerSupervisor({ ...options, postgres: () => postgres.connection() });
  let requestStop;
  const stopped = new Promise(resolve => { requestStop = resolve; });
  const stop = () => requestStop();
  const watcher = watch(options.root, (_event, filename) => {
    if (String(filename) === 'stop') stop();
  });
  process.once('SIGINT', stop);
  process.once('SIGTERM', stop);
  worker.on('state', state => { if (state.phase === 'failed') stop(); });
  try {
    const db = await postgres.start();
    if (db.phase !== 'ready') throw new Error('Preview PostgreSQL failed; inspect its private log');
    const state = await worker.start();
    if (state.phase !== 'ready' || state.external) throw new Error('Preview worker failed; inspect its private log');
    const response = await fetch(`${state.url}/api/preview`, { signal: AbortSignal.timeout(5000) });
    if (!response.ok) throw new Error(`Preview identity returned HTTP ${response.status}`);
    const identity = await response.json();
    console.log(JSON.stringify({ url: previewUrl(state.url), identity, stop_file: path.join(options.root, 'stop') }));
    await stopped;
    if (worker.state.phase === 'failed') throw new Error('Preview worker stopped unexpectedly; inspect its private log');
  } finally {
    watcher.close();
    process.removeListener('SIGINT', stop);
    process.removeListener('SIGTERM', stop);
    await stopPreview(worker, postgres);
  }
}

export async function stopPreview(worker, postgres) {
  // Worker Stop persistence needs PostgreSQL alive. Cleanup still runs on errors.
  try {
    await worker.stop();
  } finally {
    try {
      await postgres.stop(); // The supervisor resolves void; inspect its state.
      if (postgres.state.phase !== 'stopped') throw new Error('Preview PostgreSQL did not stop; inspect its private log');
    } finally {
      await worker.clearConnectionFile();
    }
  }
}

if (process.argv[1] && import.meta.url === pathToFileURL(path.resolve(process.argv[1])).href) {
  startPreview().catch(error => { console.error(error.message); process.exitCode = 1; });
}
