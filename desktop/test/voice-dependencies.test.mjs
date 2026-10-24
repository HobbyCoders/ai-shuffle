import assert from 'node:assert/strict';
import { test } from 'node:test';
import { promises as fs, mkdirSync } from 'node:fs';
import { EventEmitter } from 'node:events';
import os from 'node:os';
import path from 'node:path';
import vm from 'node:vm';
import { createRequire } from 'node:module';
const require = createRequire(import.meta.url);

async function fixture(t, failMain = false) {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), 'shuffle-voice-build-'));
  t.after(() => fs.rm(root, { recursive: true, force: true, maxRetries: 10, retryDelay: 100 }));
  const source = path.join(root, 'source');
  await fs.mkdir(source);
  await fs.mkdir(path.join(source, 'fluid'));
  for (const name of ['requirements.lock', 'requirements-native.lock', 'requirements-voice.lock', 'fluid/pyproject.toml']) {
    await fs.writeFile(path.join(source, name), '# synthetic pinned inputs');
  }
  const calls = [];
  let voiceAttempts = 0;
  const spawn = (exe, args) => {
    calls.push(args);
    const child = new EventEmitter();
    if (args[0] === '-m' && args[1] === 'venv') mkdirSync(args[2], { recursive: true });
    const voice = args[1] === 'app/core/voice_dependencies.py';
    const failed = voice ? ++voiceAttempts === 1 : failMain && args.includes('requirements.lock');
    setImmediate(() => child.emit('exit', failed ? 1 : 0));
    return child;
  };
  const module = { exports: {} };
  const input = await fs.readFile(new URL('../src/main/source-build.ts', import.meta.url), 'utf8');
  const output = require('typescript').transpileModule(input, { compilerOptions: {
    module: require('typescript').ModuleKind.CommonJS, target: require('typescript').ScriptTarget.ES2022,
    esModuleInterop: true,
  } }).outputText;
  vm.runInNewContext(output + '\nmodule.exports.pythonRuntime = pythonRuntime;', {
    module, exports: module.exports, process, console, Buffer,
    require: name => name === 'node:child_process' ? { spawn } : require(name),
  });
  const opts = { source, cache: path.join(root, 'cache'), log: path.join(root, 'build.log'),
    runtime: { python: 'fake-python', node: process.execPath, pythonDependencies: 'old' },
    revision: 'synthetic', progress() {} };
  return { ...module.exports, opts, calls };
}

test('a failed optional install keeps the base usable but never stamps complete; unchanged locks retry', async t => {
  const f = await fixture(t);
  const key = await f.pythonRuntime(f.opts);
  const target = path.join(f.opts.cache, 'python', key.slice(0, 24));
  assert.equal(await fs.readFile(path.join(target, '.base-complete'), 'utf8'), key);
  await assert.rejects(fs.access(path.join(target, '.complete')), { code: 'ENOENT' });
  const before = f.calls.length;
  assert.equal(await f.pythonRuntime(f.opts), key);
  assert.equal(f.calls.length, before + 1, 'retry only optional libraries, not the whole environment');
  assert.deepEqual([...f.calls.at(-1)], ['-P', 'app/core/voice_dependencies.py', '--install']);
  assert.equal(await fs.readFile(path.join(target, '.complete'), 'utf8'), key);
  const completed = f.calls.length;
  await f.pythonRuntime(f.opts);
  assert.equal(f.calls.length, completed, 'complete environments retain normal fingerprint reuse');
});

test('a main dependency failure still fails the build and cannot stamp either completion marker', async t => {
  const f = await fixture(t, true);
  await assert.rejects(f.pythonRuntime(f.opts), /failed/);
  assert.deepEqual(await fs.readdir(path.join(f.opts.cache, 'python')), []);
});

for (const failedVoice of [false, true]) {
  test(`bundled runtime records actual optional install success: failure=${failedVoice}`, async () => {
    const text = await fs.readFile(new URL('../scripts/build-worker.mjs', import.meta.url), 'utf8');
    const producer = text.slice(text.indexOf('async function installPython()'), text.indexOf('async function installNode()'));
    const calls = [];
    const context = {
      args: {}, platform: 'linux', arch: 'x64', PYTHON_TRIPLES: { 'linux-x64': 'test' },
      PYTHON_VERSION: 'test', PYTHON_BUILD: 'test', native: { pip: '26.2.1' }, cacheDir: '/fake/cache', outDir: '/fake/out', repoDir: '/fake/repo',
      path, process: { env: {} }, log() {}, pythonExe: root => path.join(root, 'bin/python'),
      fetchText: async () => 'hash  cpython-test+test-test-install_only.tar.gz',
      download: async () => {}, extract: async () => {},
      run(exe, args) {
        calls.push(args);
        if (failedVoice && args.some(arg => arg.endsWith('voice_dependencies.py'))) throw Error('synthetic failure');
      },
    };
    const result = await vm.runInNewContext(producer + '\ninstallPython()', context);
    assert.equal(result.optionalVoiceInstalled, !failedVoice);
    // -P: run as a file, app/core/platform.py would otherwise shadow the stdlib (#429).
    assert.deepEqual([...calls.find(args => args.some(arg => arg.endsWith('voice_dependencies.py')))], ['-P', path.join('/fake/repo', 'app/core/voice_dependencies.py'), '--install']);
    assert.ok(calls.some(args => args.includes('--no-build-isolation')), 'main Fluid install must continue');
    assert.match(text, /pythonDependencies: python\.optionalVoiceInstalled === false \? null :/);
  });
}
