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

async function fixture(t) {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), 'shuffle-output-reuse-'));
  t.after(() => fs.rm(root, { recursive: true, force: true, maxRetries: 10, retryDelay: 100 }));
  await fs.mkdir(path.join(root, 'npm/bin'), { recursive: true });
  await fs.writeFile(path.join(root, 'npm/package.json'), '{"version":"10.0.0"}');
  const calls = [], stats = [], env = { ...process.env }, clock = { identity: 'v22 linux x64 abi-127', fail: null, failNpm: null, omitElectron: false, fixedTime: false, leases: [], deadPid: 2_000_000_000, unknownPid: 1_999_999_999 };
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
    }
    const missingRuntime = args[1] === 'run' && !existsSync(path.join(options.cwd, 'node_modules/locked/index.js'));
    if (args[1] === 'run' && !missingRuntime) put('dist/index.js', 'module.exports = require("locked");');
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
  const trackedFs = { ...require('node:fs'), promises: { ...fs, lstat(...args) { stats.push(String(args[0])); return fs.lstat(...args); } } };
  const kill = (pid, signal) => {
    if (pid === clock.deadPid || pid === clock.unknownPid) throw Object.assign(new Error('probe'), { code: pid === clock.deadPid ? 'ESRCH' : 'EPERM' });
    return process.kill(pid, signal);
  };
  vm.runInNewContext(output, { module, exports: module.exports, process: { ...process, env, kill }, console, Buffer,
    require: name => name === 'node:child_process' ? { spawn } : name === 'node:fs' ? trackedFs : require(name) });
  let number = 0;
  const build = async (mutate = async () => {}) => {
    const stage = path.join(root, `stage-${++number}`), source = path.join(stage, 'source');
    for (const name of ['app', 'fluid/fluid', 'ui/mobile', ...packages]) await fs.mkdir(path.join(source, name), { recursive: true });
    for (const name of ['requirements.lock', 'requirements-native.lock', 'requirements-voice.lock', 'fluid/pyproject.toml']) await fs.writeFile(path.join(source, name), '# locked');
    await fs.writeFile(path.join(source, 'app/main.py'), `# app revision ${number}`);
    await fs.writeFile(path.join(source, 'fluid/fluid/__init__.py'), `# Fluid revision ${number}`);
    await fs.writeFile(path.join(source, 'ui/index.html'), 'verified desktop');
    await fs.writeFile(path.join(source, 'ui/mobile/index.html'), 'verified phone');
    for (const name of packages) {
      await fs.mkdir(path.join(source, name, 'src'));
      await fs.writeFile(path.join(source, name, 'src/index.ts'), 'export const value = 1;');
      await fs.writeFile(path.join(source, name, 'package.json'), JSON.stringify({ name, scripts: { build: 'tsc' }, dependencies: { locked: '1.0.0' } }));
      await fs.writeFile(path.join(source, name, 'package-lock.json'), '{}');
      await fs.writeFile(path.join(source, name, 'tsconfig.json'), '{}');
    }
    await mutate(source);
    const opts = { source, server: path.join(stage, 'server'), cache: path.join(root, 'cache'), sharedCache: path.join(root, 'cache'),
      log: path.join(root, 'build.log'), prebuiltUI: path.join(source, 'ui'),
      runtime: { python: 'fake-python', node: 'fake-node', npm: path.join(root, 'npm/bin/npm-cli.js'), pythonDependencies: await module.exports.pythonDependencyKey(source) },
      revision: number.toString(16).padStart(40, '0'), progress() {} };
    const start = calls.length, statStart = stats.length;
    const built = await module.exports.buildSource(opts);
    await fs.writeFile(path.join(stage, 'build.json'), JSON.stringify(built));
    return { opts, calls: calls.slice(start), stats: stats.slice(statStart), built };
  };
  const loadUpdates = async cleanup => {
    const loaded = { exports: {} };
    const text = await fs.readFile(new URL('../src/main/source-updates.ts', import.meta.url), 'utf8');
    vm.runInNewContext(ts.transpileModule(text, { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022, esModuleInterop: true } }).outputText,
      { module: loaded, exports: loaded.exports, process, console, Buffer, Response, AbortSignal, setTimeout, clearTimeout,
        require: name => name === './source-build' ? { ...module.exports, pruneNodeDependencies: cleanup } : name.startsWith('.') ? {} : require(name) });
    return loaded.exports.SourceUpdates;
  };
  return { root, build, env, clock, loadUpdates,
    prune: (revisions, cache = path.join(root, 'cache')) => module.exports.pruneNodeDependencies(cache, revisions),
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

test('repeated repairs prune unreferenced generations and never rehash known damaged predecessors', async t => {
  const f = await fixture(t), first = await f.build();
  const retained = await fs.realpath(path.join(first.opts.source, 'browser-mcp/node_modules'));
  const marker = await fs.readFile(path.join(path.dirname(retained), '.complete'), 'utf8');
  const cache = path.dirname(path.dirname(retained)), rejected = [];
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
    await f.prune([path.dirname(first.opts.source), path.dirname(current.opts.source)]);
    assert.equal((await fs.readdir(cache, { withFileTypes: true })).filter(entry => entry.isDirectory()).length, 2, 'only retained generations remain after repair');
  }
  assert.equal(await fs.readFile(path.join(path.dirname(retained), '.complete'), 'utf8'), marker);
  await assert.rejects(fs.access(path.join(retained, 'locked')), { code: 'ENOENT' });
  assert.equal(f.clock.leases.every(Boolean), true, 'every npm install owns a temporary lease');
  await f.forget(first); await f.prune([path.dirname(current.opts.source)]);
  assert.equal((await fs.readdir(cache, { withFileTypes: true })).filter(entry => entry.isDirectory()).length, 1);
  assert.equal((await fs.readdir(cache)).some(entry => entry.startsWith('.invalid-')), false);
});

test('dependency retention keeps rollback/runtime references and only two unused generations per package', async t => {
  const f = await fixture(t), first = await f.build();
  const retained = await fs.realpath(path.join(first.opts.source, 'browser-mcp/node_modules'));
  const cache = path.dirname(path.dirname(retained));
  let current = first;
  for (let version = 1; version <= 7; version++) {
    const next = await f.build(source => fs.writeFile(path.join(source, 'browser-mcp/package-lock.json'), JSON.stringify({ version })));
    if (current !== first) await f.forget(current);
    current = next;
    await f.prune([path.dirname(first.opts.source), path.dirname(current.opts.source)]);
    assert.ok((await fs.readdir(cache, { withFileTypes: true })).filter(entry => entry.isDirectory()).length <= 4, 'two referenced plus at most two unused');
  }
  const runtime = path.join(first.opts.server, 'browser-mcp/dist/index.js');
  assert.equal(execFileSync(process.execPath, ['-p', `require(${JSON.stringify(runtime)})`], { encoding: 'utf8' }).trim(), 'runtime dependency');
  assert.equal(await fs.realpath(path.join(first.opts.source, 'browser-mcp/node_modules')), retained);
  await f.forget(first);
  await f.prune([path.dirname(current.opts.source)]);
  assert.equal((await fs.readdir(cache, { withFileTypes: true })).filter(entry => entry.isDirectory()).length, 3);
});

test('all retained generations survive via server links, missing module roots and build.json alone', async t => {
  const f = await fixture(t), retained = [];
  for (let version = 1; version <= 5; version++) retained.push(await f.build(async source => {
    for (const name of ['browser-mcp', 'desktop']) await fs.writeFile(path.join(source, name, 'package-lock.json'), JSON.stringify({ version }));
  }));
  const first = retained[0], browser = await fs.realpath(path.join(first.opts.source, 'browser-mcp/node_modules'));
  const desktop = await fs.realpath(path.join(first.opts.source, 'desktop/node_modules'));
  for (const name of packages) await fs.unlink(path.join(first.opts.source, name, 'node_modules'));
  await fs.rm(first.opts.source, { recursive: true, force: true, maxRetries: 10, retryDelay: 100 });
  // The browser now has only a dangling server link; desktop only build.json.
  await fs.rm(browser, { recursive: true, force: true, maxRetries: 10, retryDelay: 100 });
  await f.prune(retained.map(result => path.dirname(result.opts.source)));
  for (const modules of [browser, desktop]) {
    await fs.access(path.join(path.dirname(modules), '.complete'));
    const cache = path.dirname(path.dirname(modules));
    assert.equal((await fs.readdir(cache, { withFileTypes: true })).filter(entry => entry.isDirectory()).length, 5);
  }
});

test('the two-unused-generation cap spans shared and native-runtime cache namespaces', async t => {
  const f = await fixture(t), parents = ['node', path.join('1'.repeat(24), 'node'), path.join('2'.repeat(64), 'node')];
  for (const parent of parents) for (const digit of ['a', 'b', 'c']) {
    const entry = path.join(f.root, 'cache', parent, 'browser-mcp', digit.repeat(24));
    await fs.mkdir(entry, { recursive: true }); await fs.writeFile(path.join(entry, '.complete'), 'legacy');
  }
  await f.prune([]);
  let count = 0;
  for (const parent of parents) count += (await fs.readdir(path.join(f.root, 'cache', parent, 'browser-mcp'), { withFileTypes: true })).filter(entry => entry.isDirectory()).length;
  assert.equal(count, 2);
});

test('abandoned install cleanup requires age and no live/unknown lease, and never follows a junction', async t => {
  const f = await fixture(t), first = await f.build();
  const retained = await fs.realpath(path.join(first.opts.source, 'browser-mcp/node_modules'));
  const cache = path.dirname(path.dirname(retained)), prefix = path.basename(path.dirname(retained)).slice(0, 24);
  const old = new Date(Date.now() - 25 * 60 * 60_000), recent = new Date();
  const candidates = [];
  for (const [index, pid, time] of [[1, null, old], [2, f.clock.deadPid, old], [3, process.pid, old], [4, null, recent], [5, f.clock.unknownPid, old]]) {
    const entry = path.join(cache, `${prefix}.tmp-${String(index).padStart(8, '0')}`);
    await fs.mkdir(entry);
    if (pid !== null) await fs.writeFile(path.join(entry, '.install.lock'), String(pid));
    await fs.utimes(entry, time, time); candidates.push(entry);
  }
  const outside = path.join(f.root, 'outside'); await fs.mkdir(outside); await fs.writeFile(path.join(outside, 'keep'), 'safe');
  await fs.symlink(outside, path.join(candidates[0], 'node_modules'), process.platform === 'win32' ? 'junction' : 'dir');
  await fs.utimes(candidates[0], old, old);
  await f.prune([path.dirname(first.opts.source)]);
  for (const entry of candidates.slice(0, 2)) await assert.rejects(fs.access(entry), { code: 'ENOENT' });
  for (const entry of candidates.slice(2)) await fs.access(entry);
  assert.equal(await fs.readFile(path.join(outside, 'keep'), 'utf8'), 'safe');
  await fs.writeFile(path.join(candidates[2], '.install.lock'), String(f.clock.deadPid));
  await fs.utimes(candidates[2], old, old); await fs.utimes(candidates[3], old, old);
  await f.prune([path.dirname(first.opts.source)]);
  for (const entry of candidates.slice(2, 4)) await assert.rejects(fs.access(entry), { code: 'ENOENT' });
  await fs.access(candidates[4]);
});

test('unreadable retained metadata defers dependency cleanup rather than guessing unused', async t => {
  const f = await fixture(t), first = await f.build();
  const dependencies = await fs.realpath(path.join(first.opts.source, 'browser-mcp/node_modules'));
  const cache = path.dirname(path.dirname(dependencies));
  for (let index = 1; index <= 4; index++) {
    const entry = path.join(cache, String(index).repeat(24)); await fs.mkdir(entry);
    await fs.writeFile(path.join(entry, '.complete'), 'legacy');
  }
  await fs.writeFile(path.join(path.dirname(first.opts.source), 'build.json'), '{');
  await assert.rejects(f.prune([path.dirname(first.opts.source)]));
  assert.equal((await fs.readdir(cache, { withFileTypes: true })).filter(entry => entry.isDirectory()).length, 5);
});

test('updater dependency cleanup runs under update.lock on success/error but never after lock refusal', async t => {
  const f = await fixture(t), cleanupCalls = [];
  const Updates = await f.loadUpdates(async (...args) => {
    cleanupCalls.push({ args, held: existsSync(path.join(f.root, 'update.lock')) });
    // Exercise the real cleanup with the exact dependency root/retained paths.
    assert.equal(args[0], path.join(f.root, 'dependencies'));
    await f.prune(args[1], args[0]);
  });
  const revision = 'a'.repeat(40); await fs.mkdir(path.join(f.root, 'versions', revision), { recursive: true });
  const linkedRevision = path.join(f.root, 'versions', 'b'.repeat(40));
  const external = path.join(f.root, 'retained'); await fs.mkdir(external);
  await fs.symlink(external, linkedRevision, process.platform === 'win32' ? 'junction' : 'dir');
  const cache = path.join(f.root, 'dependencies/node/browser-mcp');
  for (const digit of ['1', '2', '3']) {
    const entry = path.join(cache, digit.repeat(24)); await fs.mkdir(entry, { recursive: true });
    await fs.writeFile(path.join(entry, '.complete'), 'legacy');
  }
  const updater = new Updates({ root: f.root, publicKey: 'test', bootstrapNode: process.execPath });
  updater.state = { active: revision }; updater.retryCleanup = async () => {};
  updater.manifest = async () => ({ signed: '', manifest: { revision } });
  assert.equal((await updater.check()).kind, 'current');
  assert.equal(cleanupCalls.length, 1); assert.equal(cleanupCalls[0].held, true);
  assert.equal((await fs.readdir(cache)).length, 2, 'the updater actually reclaims unused generations');
  assert.deepEqual(Array.from(cleanupCalls[0].args[1]), [path.join(f.root, 'versions', revision), linkedRevision, path.join(f.root, 'staging')]);
  await assert.rejects(fs.access(path.join(f.root, 'update.lock')), { code: 'ENOENT' });
  updater.manifest = async () => { throw new Error('feed failed'); };
  await assert.rejects(updater.check(), /feed failed/);
  assert.equal(cleanupCalls.length, 2); assert.equal(cleanupCalls[1].held, true); assert.equal(updater.checking, false);
  await fs.writeFile(path.join(f.root, 'update.lock'), String(process.pid));
  await assert.rejects(updater.check(), /already running/);
  assert.equal(cleanupCalls.length, 2); await fs.access(path.join(f.root, 'update.lock'));
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

test('corrupted and incomplete cache entries rebuild, and retention is bounded', async t => {
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
