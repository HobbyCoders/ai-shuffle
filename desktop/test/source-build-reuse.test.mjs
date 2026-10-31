import assert from 'node:assert/strict';
import { test } from 'node:test';
import { promises as fs, existsSync, mkdirSync, utimesSync, writeFileSync } from 'node:fs';
import { execFileSync } from 'node:child_process';
import { EventEmitter } from 'node:events';
import os from 'node:os';
import path from 'node:path';
import vm from 'node:vm';
import { createRequire } from 'node:module';
const require = createRequire(import.meta.url);
const packages = ['tools', 'browser-mcp', 'desktop'];

/** `electron`: load source-build as Electron does, where node:fs reads `*.asar` files as folders. */
async function fixture(t, { electron = false } = {}) {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), 'shuffle-output-reuse-'));
  t.after(() => fs.rm(root, { recursive: true, force: true, maxRetries: 10, retryDelay: 100 }));
  await fs.mkdir(path.join(root, 'npm/bin'), { recursive: true });
  await fs.writeFile(path.join(root, 'npm/package.json'), '{"version":"10.0.0"}');
  const calls = [], stats = [], env = { ...process.env }, clock = { identity: 'v22 linux x64 abi-127', fail: null, failNpm: null, omitElectron: false, fixedTime: false, leases: [] };
  const spawn = (exe, args, options) => {
    calls.push({ exe, args, cwd: options.cwd });
    const child = new EventEmitter(); child.stdout = new EventEmitter();
    const put = (relative, content = '') => {
      const target = path.join(options.cwd, relative);
      mkdirSync(path.dirname(target), { recursive: true }); writeFileSync(target, content);
      if (clock.fixedTime) utimesSync(target, 1_000_000_000, 1_000_000_000);
    };
    if (args[1] === 'ci') {
      clock.leases.push(existsSync(path.join(options.cwd, '.install.lock')));
      put('node_modules/.installed');
      put('node_modules/locked/package.json', '{"name":"locked","main":"index.js"}');
      put('node_modules/locked/index.js', 'module.exports = "runtime dependency";');
      if (!clock.omitElectron) put(`node_modules/electron/dist/${process.platform === 'win32' ? 'electron.exe' : 'electron'}`);
    }
    if (args[0]?.endsWith(path.join('electron', 'install.js'))) {
      put(`dist/${process.platform === 'win32' ? 'electron.exe' : 'electron'}`);
      put('path.txt', process.platform === 'win32' ? 'electron.exe' : 'electron');
      if (electron) put('dist/resources/default_app.asar', 'Electron default app archive');
    }
    const missingRuntime = args[1] === 'run' && !existsSync(path.join(options.cwd, 'node_modules/locked/index.js'));
    if (args[1] === 'run' && !missingRuntime) put('dist/index.js', 'module.exports = require("locked");');
    if (args[1] === 'run' && path.basename(options.cwd) === 'frontend' && !missingRuntime) {
      put('build/index.html', 'compiled desktop'); put('build/mobile/index.html', 'compiled phone');
    }
    if (args.includes('app.core.cloudflared')) put('downloads/verified-runtime');
    setImmediate(() => {
      if (args[0] === '-p') child.stdout.emit('data', clock.identity);
      const failedInstall = args[1] === 'ci' && path.basename(path.dirname(options.cwd)) === clock.failNpm;
      child.emit('exit', missingRuntime || failedInstall || (args[1] === 'run' && path.basename(options.cwd) === clock.fail) ? 1 : 0);
    });
    return child;
  };
  const module = { exports: {} }, ts = require('typescript');
  const input = await fs.readFile(new URL('../src/main/source-build.ts', import.meta.url), 'utf8');
  const output = ts.transpileModule(input, { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022, esModuleInterop: true } }).outputText;
  const trackedFs = { ...require('node:fs'), promises: { ...fs } };
  for (const method of ['lstat', 'readFile', 'rm']) trackedFs.promises[method] = async (...args) => {
    if (clock.onIo) await clock.onIo(method, String(args[0]));
    if (method === 'lstat') stats.push(String(args[0]));
    if (clock.ioError?.method === method && clock.ioError.file === String(args[0])) {
      const { code } = clock.ioError; clock.ioError = null;
      return Promise.reject(Object.assign(new Error('transient cache I/O failure'), { code }));
    }
    return fs[method](...args);
  };
  // Electron's asar-aware node:fs opens each archive it reads and keeps it open
  // until the process exits; Windows then cannot rename or delete the install.
  const asarReads = [];
  const asarAware = { ...trackedFs, promises: Object.fromEntries(Object.entries(trackedFs.promises).map(([method, call]) => [method, typeof call !== 'function' ? call : (...args) => {
    const archive = args.find(arg => typeof arg === 'string' && /\.asar(?:[\\/]|$)/i.test(arg));
    if (archive === undefined) return call(...args);
    asarReads.push(archive);
    return Promise.reject(new Error(`asar-aware fs opened ${archive}`));
  }])) };
  const versions = electron ? { ...process.versions, electron: '44.0.0' } : process.versions;
  vm.runInNewContext(output, { module, exports: module.exports, process: { ...process, env, versions }, console, Buffer,
    require: name => name === 'node:child_process' ? { spawn }
      : name === (electron ? 'original-fs' : 'node:fs') ? trackedFs
      : name === 'node:fs' ? asarAware
      : name === 'node:timers' ? { setTimeout: (callback, ms, ...rest) => setTimeout(callback, clock.discardWait ?? ms, ...rest), clearTimeout }
      : require(name) });
  let number = 0;
  const build = async (mutate = async () => {}) => {
    const stage = path.join(root, `stage-${++number}`), source = path.join(stage, 'source');
    const nodePackages = clock.localUI ? ['frontend', ...packages] : packages;
    for (const name of ['app', 'fluid/fluid', 'ui/mobile', ...nodePackages]) await fs.mkdir(path.join(source, name), { recursive: true });
    for (const name of ['requirements.lock', 'requirements-native.lock', 'requirements-voice.lock', 'fluid/pyproject.toml']) await fs.writeFile(path.join(source, name), '# locked');
    await fs.writeFile(path.join(source, 'app/main.py'), `# app revision ${number}`);
    await fs.writeFile(path.join(source, 'fluid/fluid/__init__.py'), `# Fluid revision ${number}`);
    await fs.writeFile(path.join(source, 'ui/index.html'), 'verified desktop');
    await fs.writeFile(path.join(source, 'ui/mobile/index.html'), 'verified phone');
    for (const name of nodePackages) {
      await fs.mkdir(path.join(source, name, 'src'));
      await fs.writeFile(path.join(source, name, 'src/index.ts'), 'export const value = 1;');
      await fs.writeFile(path.join(source, name, 'package.json'), JSON.stringify({ name, scripts: { build: 'tsc' }, dependencies: { locked: '1.0.0' } }));
      await fs.writeFile(path.join(source, name, 'package-lock.json'), '{}');
      await fs.writeFile(path.join(source, name, 'tsconfig.json'), '{}');
    }
    await mutate(source);
    const sharedCache = path.join(root, clock.cacheRoot ?? 'cache');
    const opts = { source, server: path.join(stage, 'server'), cache: path.join(sharedCache, clock.namespace ?? ''), sharedCache,
      log: path.join(root, 'build.log'), prebuiltUI: clock.localUI ? undefined : path.join(source, 'ui'),
      runtime: { python: 'fake-python', node: 'fake-node', npm: path.join(root, 'npm/bin/npm-cli.js'), pythonDependencies: await module.exports.pythonDependencyKey(source) },
      revision: number.toString(16).padStart(40, '0'), progress() {} };
    const start = calls.length, statStart = stats.length;
    const built = await module.exports.buildSource(opts);
    await fs.writeFile(path.join(stage, 'build.json'), JSON.stringify(built));
    return { opts, calls: calls.slice(start), stats: stats.slice(statStart), built };
  };
  const loadUpdates = async () => {
    const loaded = { exports: {} };
    const text = await fs.readFile(new URL('../src/main/source-updates.ts', import.meta.url), 'utf8');
    vm.runInNewContext(ts.transpileModule(text, { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022, esModuleInterop: true } }).outputText,
      { module: loaded, exports: loaded.exports, process, console, Buffer, Response, AbortSignal, setTimeout, clearTimeout,
        require: name => name === './source-build' ? module.exports : name.startsWith('.') ? {} : require(name) });
    return loaded.exports.SourceUpdates;
  };
  return { root, build, env, clock, loadUpdates, asarReads,
    forget: result => module.exports.removeSourceDirectory(path.dirname(result.opts.source)) };

}
const built = result => result.calls.filter(call => call.args[1] === 'run').map(call => path.basename(call.cwd));
const installed = result => result.calls.filter(call => call.args[1] === 'ci').map(call => call.cwd.split(path.sep).at(-2));

test('unchanged content across signed revisions runs zero package builds or npm installs', async t => {
  const f = await fixture(t); await f.build();
  const second = await f.build();
  assert.deepEqual(built(second), [], 'unchanged tools/browser-mcp/desktop must reuse their outputs');
  assert.deepEqual(installed(second), []);
  assert.equal(await fs.readFile(path.join(second.opts.server, 'fluid/__init__.py'), 'utf8'), '# Fluid revision 2');
  assert.equal(await fs.readFile(path.join(second.opts.server, 'app/main.py'), 'utf8'), '# app revision 2');
});


test('a missing runtime dependency with intact install and output markers is replaced before output reuse', async t => {
  for (const name of packages) {
    const f = await fixture(t), first = await f.build();
    const retained = await fs.realpath(path.join(first.opts.source, name, 'node_modules'));
    const marker = path.join(path.dirname(retained), '.complete');
    const originalMarker = await fs.readFile(marker, 'utf8');
    const outputRoot = path.join(f.root, 'cache/builds', name);
    const [outputKey] = await fs.readdir(outputRoot);
    const outputMarker = await fs.readFile(path.join(outputRoot, outputKey, '.complete'), 'utf8');
    await fs.rm(path.join(retained, 'locked'), { recursive: true, maxRetries: 10, retryDelay: 100 });
    const second = await f.build();
    const entry = path.join(second.opts.source, name, 'dist/index.js');
    assert.equal(execFileSync(process.execPath, ['-p', `require(${JSON.stringify(entry)})`], { encoding: 'utf8', stdio: ['ignore', 'pipe', 'pipe'] }).trim(), 'runtime dependency');
    assert.deepEqual(installed(second), [name], 'only the damaged dependency closure is reinstalled');
    assert.deepEqual(built(second), [], 'unchanged output is reusable after its runtime dependencies are restored');
    const replacement = await fs.realpath(path.join(second.opts.source, name, 'node_modules'));
    assert.notEqual(replacement, retained, 'retained dependency entries must never be repaired in place');
    assert.equal(await fs.readFile(marker, 'utf8'), originalMarker);
    await assert.rejects(fs.access(path.join(retained, 'locked')), { code: 'ENOENT' });
    assert.equal(await fs.readFile(path.join(outputRoot, outputKey, '.complete'), 'utf8'), outputMarker);
    const third = await f.build();
    assert.deepEqual(installed(third), []);
    assert.deepEqual(built(third), []);
    assert.equal(await fs.realpath(path.join(third.opts.source, name, 'node_modules')), replacement);
  }
});


test('same-size dependency source edits with restored mtime are replaced, not repaired', async t => {
  const f = await fixture(t); f.clock.fixedTime = true;
  const first = await f.build();
  const retained = await fs.realpath(path.join(first.opts.source, 'browser-mcp/node_modules'));
  const file = path.join(retained, 'locked/index.js'), before = await fs.stat(file, { bigint: true });
  await fs.writeFile(file, 'module.exports = "runtime dependencY";');
  await fs.utimes(file, 1_000_000_000, 1_000_000_000);
  const after = await fs.stat(file, { bigint: true });
  assert.equal(after.size, before.size); assert.equal(after.mtimeNs, before.mtimeNs); assert.equal(after.ino, before.ino);
  const second = await f.build();
  assert.deepEqual(installed(second), ['browser-mcp']); assert.deepEqual(built(second), []);
  assert.equal(await fs.readFile(file, 'utf8'), 'module.exports = "runtime dependencY";');
  const runtime = path.join(second.opts.server, 'browser-mcp/dist/index.js');
  assert.equal(execFileSync(process.execPath, ['-p', `require(${JSON.stringify(runtime)})`], { encoding: 'utf8' }).trim(), 'runtime dependency');
});

test('only root npm build-scratch directories are excluded from dependency integrity', async t => {
  const f = await fixture(t), first = await f.build();
  const retained = await fs.realpath(path.join(first.opts.source, 'browser-mcp/node_modules'));
  for (const name of ['.cache', '.vite', '.vite-temp']) {
    await fs.mkdir(path.join(retained, name));
    await fs.writeFile(path.join(retained, name, 'generated.json'), '{}');
  }
  const second = await f.build();
  assert.deepEqual(installed(second), []); assert.deepEqual(built(second), []);
  await fs.mkdir(path.join(retained, 'locked/.cache'));
  await fs.writeFile(path.join(retained, 'locked/.cache', 'runtime.json'), '{}');
  const third = await f.build();
  assert.deepEqual(installed(third), ['browser-mcp']); assert.deepEqual(built(third), []);
});

test('legacy and incomplete dependency records are misses without changing retained records', async t => {
  for (const value of ['legacy', 'partial']) {
    const f = await fixture(t), first = await f.build();
    const retained = await fs.realpath(path.join(first.opts.source, 'tools/node_modules'));
    const marker = path.join(path.dirname(retained), '.complete');
    const key = JSON.parse(await fs.readFile(marker, 'utf8')).key;
    if (value === 'legacy') await fs.writeFile(marker, key); else await fs.unlink(marker);
    const second = await f.build();
    assert.deepEqual(installed(second), ['tools']); assert.deepEqual(built(second), []);
    assert.notEqual(await fs.realpath(path.join(second.opts.source, 'tools/node_modules')), retained);
    if (value === 'legacy') assert.equal(await fs.readFile(marker, 'utf8'), key);
    else await assert.rejects(fs.access(marker), { code: 'ENOENT' });
  }
});

test('a failed replacement install publishes no entry and leaves the retained dependency tree untouched', async t => {
  const f = await fixture(t), first = await f.build();
  const retained = await fs.realpath(path.join(first.opts.source, 'browser-mcp/node_modules'));
  await fs.rm(path.join(retained, 'locked'), { recursive: true, maxRetries: 10, retryDelay: 100 });
  f.clock.failNpm = 'browser-mcp';
  await assert.rejects(f.build(), /failed/);
  const generation = path.basename(path.dirname(retained));
  assert.deepEqual((await fs.readdir(path.dirname(path.dirname(retained)))).sort(), ['.invalid-' + generation, generation].sort());
  await assert.rejects(fs.access(path.join(retained, 'locked')), { code: 'ENOENT' });
  f.clock.failNpm = null;
  assert.deepEqual(installed(await f.build()), ['browser-mcp']);
});

test('repeated repairs retain every permanent generation and never rehash known damaged predecessors', async t => {
  const f = await fixture(t), first = await f.build();
  const retained = await fs.realpath(path.join(first.opts.source, 'browser-mcp/node_modules'));
  const marker = await fs.readFile(path.join(path.dirname(retained), '.complete'), 'utf8');
  const cache = path.dirname(path.dirname(retained)), rejected = [], generations = new Set([path.dirname(retained)]);
  let current = first;
  for (let repair = 0; repair < 6; repair++) {
    const damaged = await fs.realpath(path.join(current.opts.source, 'browser-mcp/node_modules'));
    await fs.rm(path.join(damaged, 'locked'), { recursive: true, maxRetries: 10, retryDelay: 100 });
    const replacement = await f.build();
    assert.deepEqual(installed(replacement), ['browser-mcp']); assert.deepEqual(built(replacement), []);
    assert.equal(replacement.stats.some(file => rejected.some(old => file === old || file.startsWith(old + path.sep))), false, 'a known damaged predecessor must not be hashed again');
    rejected.push(damaged);
    if (current !== first) await f.forget(current);
    current = replacement;
    generations.add(path.dirname(await fs.realpath(path.join(current.opts.source, 'browser-mcp/node_modules'))));
    assert.equal(generations.size, repair + 2);
    assert.equal((await fs.readdir(cache, { withFileTypes: true })).filter(entry => entry.isDirectory()).length, generations.size);
    for (const generation of generations) await fs.access(path.join(generation, '.complete'));
  }
  assert.equal(await fs.readFile(path.join(path.dirname(retained), '.complete'), 'utf8'), marker);
  await assert.rejects(fs.access(path.join(retained, 'locked')), { code: 'ENOENT' });
  assert.equal(f.clock.leases.every(Boolean), true, 'every npm install owns a temporary lease');
  await f.forget(first);
  assert.equal((await fs.readdir(cache, { withFileTypes: true })).filter(entry => entry.isDirectory()).length, generations.size);
  assert.equal((await fs.readdir(cache)).filter(entry => entry.startsWith('.invalid-')).length, rejected.length);
});

test('build records contain the exact selected generations for new, reused and runtime-local installs', async t => {
  const f = await fixture(t);
  for (const runtime of [null, '1'.repeat(24), '2'.repeat(64), 'ui']) {
    f.clock.localUI = runtime === 'ui'; f.clock.namespace = f.clock.localUI ? null : runtime;
    f.clock.identity = f.clock.namespace ? '' : 'v22 linux x64 abi-127';
    const first = await f.build(), second = await f.build();
    assert.deepEqual(installed(second), []);
    for (const result of [first, second]) {
      const expected = [];
      for (const name of f.clock.localUI ? ['frontend', ...packages] : packages) expected.push(path.relative(result.opts.sharedCache, path.dirname(await fs.realpath(path.join(result.opts.source, name, 'node_modules')))).split(path.sep).join('/'));
      const record = JSON.parse(await fs.readFile(path.join(path.dirname(result.opts.source), 'build.json'), 'utf8'));
      assert.deepEqual(Array.from(result.built.nodeGenerations), expected);
      assert.deepEqual(record.nodeGenerations, expected);
      assert.equal(record.nodeGenerations.every(id => !id.includes('..') && !id.includes('\\')), true);
      if (f.clock.namespace) assert.equal(record.nodeGenerations.every(id => id.startsWith(runtime + '/node/')), true);
    }
  }
});


test('transient validation errors skip reuse once without tombstoning healthy dependencies', async t => {
  for (const code of ['EACCES', 'EIO', 'EBUSY', 'EPERM']) for (const location of ['marker', 'source', 'directory']) {
    const f = await fixture(t), first = await f.build();
    const retained = await fs.realpath(path.join(first.opts.source, 'browser-mcp/node_modules'));
    const generation = path.dirname(retained), invalid = path.join(path.dirname(generation), '.invalid-' + path.basename(generation));
    const file = location === 'marker' ? path.join(generation, '.complete') : location === 'source' ? path.join(retained, 'locked/index.js') : retained;
    f.clock.ioError = { method: location === 'directory' ? 'lstat' : 'readFile', file, code };
    // Leave no successful replacement to hide a permanently rejected old tree.
    f.clock.failNpm = 'browser-mcp'; await assert.rejects(f.build(), /failed/);
    assert.equal(f.clock.ioError, null, 'the transient failure must have been exercised');
    await assert.rejects(fs.access(invalid), { code: 'ENOENT' });
    f.clock.failNpm = null;
    const recovered = await f.build();
    assert.deepEqual(installed(recovered), []); assert.deepEqual(built(recovered), []);
    assert.equal(await fs.realpath(path.join(recovered.opts.source, 'browser-mcp/node_modules')), retained);
  }
});

test('update checks never delete any permanent dependency generation, including unreferenced ones', async t => {
  const f = await fixture(t); f.clock.cacheRoot = 'dependencies';
  const generations = new Map(); let current;
  for (let version = 0; version < 5; version++) {
    const next = await f.build(source => fs.writeFile(path.join(source, 'browser-mcp/package-lock.json'), JSON.stringify({ version })));
    for (const name of packages) {
      const generation = path.dirname(await fs.realpath(path.join(next.opts.source, name, 'node_modules')));
      generations.set(generation, await fs.readFile(path.join(generation, '.complete'), 'utf8'));
    }
    if (current) await f.forget(current);
    current = next;
  }
  const revision = 'a'.repeat(40), versions = path.join(f.root, 'versions'); await fs.mkdir(versions);
  // Active files may be outside the SHA-named inventory during restore. Neither
  // that nor unreferenced older generations permits deleting dependency data.
  const restoring = path.join(versions, revision + '.restoring');
  await fs.rename(path.dirname(current.opts.source), restoring);
  const runtime = path.join(restoring, 'server/browser-mcp/dist/index.js');
  assert.equal(execFileSync(process.execPath, ['-p', `require(${JSON.stringify(runtime)})`], { encoding: 'utf8' }).trim(), 'runtime dependency');
  const removals = [];
  f.clock.onIo = async (method, file) => { if (method === 'rm' && generations.has(file)) removals.push(file); };
  const Updates = await f.loadUpdates();
  const updater = new Updates({ root: f.root, publicKey: 'test', bootstrapNode: process.execPath });
  updater.state = { active: revision }; updater.running = revision; updater.retryCleanup = async () => {};
  updater.manifest = async () => ({ signed: '', manifest: { revision } });
  assert.equal((await updater.check()).kind, 'current');
  updater.manifest = async () => { throw new Error('feed failed'); };
  await assert.rejects(updater.check(), /feed failed/);
  assert.equal(updater.checking, false);
  await assert.rejects(fs.access(path.join(f.root, 'update.lock')), { code: 'ENOENT' });
  assert.deepEqual(removals, [], 'no permanent generation may be removed by update cleanup');
  for (const [generation, marker] of generations) assert.equal(await fs.readFile(path.join(generation, '.complete'), 'utf8'), marker);
  assert.equal(execFileSync(process.execPath, ['-p', `require(${JSON.stringify(runtime)})`], { encoding: 'utf8' }).trim(), 'runtime dependency');
  await fs.writeFile(path.join(f.root, 'update.lock'), String(process.pid));
  await assert.rejects(updater.check(), /already running/);
  assert.deepEqual(removals, []);
});
test('Electron postinstall is completed before dependency integrity is recorded', async t => {
  const f = await fixture(t); f.clock.omitElectron = true;
  const first = await f.build();
  const downloads = result => result.calls.filter(call => call.args[0]?.endsWith(path.join('electron', 'install.js')));
  assert.equal(downloads(first).length, 1);
  const retained = await fs.realpath(path.join(first.opts.source, 'desktop/node_modules'));
  const second = await f.build();
  assert.deepEqual(installed(second), []); assert.deepEqual(built(second), []); assert.equal(downloads(second).length, 0);
  const binary = path.join(retained, 'electron/dist', process.platform === 'win32' ? 'electron.exe' : 'electron');
  await fs.unlink(binary);
  const third = await f.build();
  assert.deepEqual(installed(third), ['desktop']); assert.deepEqual(built(third), []); assert.equal(downloads(third).length, 1);
  await assert.rejects(fs.access(binary), { code: 'ENOENT' });
});

test('inside Electron, the Electron download is hashed and published with the unpatched fs', async t => {
  const f = await fixture(t, { electron: true }); f.clock.omitElectron = true;
  const first = await f.build();
  assert.deepEqual(f.asarReads, [], 'the asar-aware fs must never read a dependency tree');
  assert.deepEqual(installed(first), packages);
  const retained = await fs.realpath(path.join(first.opts.source, 'desktop/node_modules'));
  assert.equal(await fs.readFile(path.join(retained, 'electron/dist/resources/default_app.asar'), 'utf8'), 'Electron default app archive');
  assert.deepEqual((await fs.readdir(path.dirname(retained))).filter(name => name.includes('.tmp-')), []);
  assert.deepEqual(installed(await f.build()), [], 'the recorded integrity matches on the next build');
  // A marker hashed by the asar-aware fs records a different tree: replace it once.
  const marker = path.join(path.dirname(retained), '.complete');
  await fs.writeFile(marker, JSON.stringify({ ...JSON.parse(await fs.readFile(marker, 'utf8')), integrity: 'f'.repeat(64) }));
  assert.deepEqual(installed(await f.build()), ['desktop']);
  assert.deepEqual(installed(await f.build()), []);
  assert.deepEqual(f.asarReads, []);
});

test('a failed install reports its own error when its temporary folder cannot be removed', async t => {
  const f = await fixture(t);
  f.clock.failNpm = 'desktop'; f.clock.discardWait = 20;
  // The removal never finishes, as when another process keeps a file open.
  f.clock.onIo = (method, file) => method === 'rm' && /[\\/]desktop[\\/][^\\/]+\.tmp-[a-f0-9]{8}$/.test(file) ? new Promise(() => {}) : undefined;
  await assert.rejects(f.build(), /fake-node failed \(1\)/);
  assert.match(await fs.readFile(path.join(f.root, 'build.log'), 'utf8'), /Could not remove .+\.tmp-[a-f0-9]{8} \(still busy after 60s\); it was left behind\./);
  f.clock.onIo = null; f.clock.failNpm = null;
  assert.deepEqual(installed(await f.build()), ['desktop'], 'a left-behind install is never reused');
});

test('one source or lock change misses exactly its package', async t => {
  for (const input of ['src/index.ts', 'package-lock.json', 'tsconfig.json']) {
    const f = await fixture(t); await f.build();
    const second = await f.build(source => fs.appendFile(path.join(source, 'tools', input), ' '));
    assert.deepEqual(built(second), ['tools']);
    assert.deepEqual(installed(second), input === 'package-lock.json' ? ['tools'] : []);
  }
});

test('a build-script-only package edit rebuilds it without npm ci', async t => {
  const f = await fixture(t); await f.build();
  const second = await f.build(async source => {
    const file = path.join(source, 'browser-mcp/package.json'), value = JSON.parse(await fs.readFile(file, 'utf8'));
    value.scripts.build = 'tsc && node verify.mjs'; value.description = 'not an installed dependency';
    await fs.writeFile(file, JSON.stringify(value));
  });
  assert.deepEqual(installed(second), []);
  assert.deepEqual(built(second), ['browser-mcp']);
});

test('shared config and Node ABI changes cannot reuse incompatible output', async t => {
  const f = await fixture(t); await f.build();
  const second = await f.build(source => fs.writeFile(path.join(source, 'tsconfig.shared.json'), '{}'));
  assert.deepEqual(built(second), packages);
  f.clock.identity = 'v22 linux x64 abi-128';
  const third = await f.build();
  assert.deepEqual(built(third), packages); assert.deepEqual(installed(third), packages);
});

test('corrupted and incomplete build-output entries rebuild, and output retention is bounded', async t => {
  const f = await fixture(t); await f.build();
  const directory = path.join(f.root, 'cache/builds/tools');
  const [key] = await fs.readdir(directory);
  await fs.writeFile(path.join(directory, key, 'dist/index.js'), 'corrupted, even same name');
  assert.deepEqual(built(await f.build()), ['tools']);
  await fs.unlink(path.join(directory, key, '.complete'));
  assert.deepEqual(built(await f.build()), ['tools']);
  for (const value of [2, 3, 4]) await f.build(source => fs.writeFile(path.join(source, 'tools/src/index.ts'), String(value)));
  assert.equal((await fs.readdir(directory)).length, 2);
});

test('a failed build never publishes output; the kill switch rebuilds normally', async t => {
  const f = await fixture(t); f.clock.fail = 'tools';
  await assert.rejects(f.build(), /failed/);
  await assert.rejects(fs.access(path.join(f.root, 'cache/builds/tools')), { code: 'ENOENT' });
  f.clock.fail = null; await f.build();
  f.env.AI_SHUFFLE_PERF_DESKTOP_BUILD_REUSE = '0';
  assert.deepEqual(built(await f.build()), packages);
});
