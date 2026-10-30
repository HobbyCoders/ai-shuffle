import assert from 'node:assert/strict';
import { test } from 'node:test';
import { promises as fs, mkdirSync, writeFileSync } from 'node:fs';
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
  const calls = [], env = { ...process.env }, clock = { identity: 'v22 linux x64 abi-127', fail: null };
  const spawn = (exe, args, options) => {
    calls.push({ exe, args, cwd: options.cwd });
    const child = new EventEmitter(); child.stdout = new EventEmitter();
    const put = (relative, content = '') => {
      const target = path.join(options.cwd, relative);
      mkdirSync(path.dirname(target), { recursive: true }); writeFileSync(target, content);
    };
    if (args[1] === 'ci') {
      put('node_modules/.installed');
      put(`node_modules/electron/dist/${process.platform === 'win32' ? 'electron.exe' : 'electron'}`);
    }
    if (args[1] === 'run') put('dist/index.js', `compiled ${path.basename(options.cwd)}`);
    if (args.includes('app.core.cloudflared')) put('downloads/verified-runtime');
    setImmediate(() => {
      if (args[0] === '-p') child.stdout.emit('data', clock.identity);
      child.emit('exit', args[1] === 'run' && path.basename(options.cwd) === clock.fail ? 1 : 0);
    });
    return child;
  };
  const module = { exports: {} }, ts = require('typescript');
  const input = await fs.readFile(new URL('../src/main/source-build.ts', import.meta.url), 'utf8');
  const output = ts.transpileModule(input, { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022, esModuleInterop: true } }).outputText;
  vm.runInNewContext(output, { module, exports: module.exports, process: { ...process, env }, console, Buffer,
    require: name => name === 'node:child_process' ? { spawn } : require(name) });
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
    const start = calls.length;
    await module.exports.buildSource(opts);
    return { opts, calls: calls.slice(start) };
  };
  return { root, build, env, clock };
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
