import assert from 'node:assert/strict';
import { test } from 'node:test';
import { promises as fs, mkdirSync } from 'node:fs';
import { EventEmitter } from 'node:events';
import os from 'node:os';
import path from 'node:path';
import vm from 'node:vm';
import { createRequire } from 'node:module';
import { pythonDependencies, pythonInputText as scriptRule } from '../scripts/runtime-contract.mjs';
const require = createRequire(import.meta.url);

/** source-build.ts with spawn faked: `npm ci` creates node_modules, `node -p` reports `identity.value`. */
async function load(t) {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), 'shuffle-cache-keys-'));
  t.after(() => fs.rm(root, { recursive: true, force: true, maxRetries: 10, retryDelay: 100 }));
  const installs = [], identity = { value: 'v22.23.2 win32 x64 127' };
  const spawn = (exe, args, options) => {
    const child = new EventEmitter();
    child.stdout = new EventEmitter();
    setImmediate(() => {
      if (args[0] === '-p') child.stdout.emit('data', Buffer.from(identity.value + '\n'));
      if (args[1] === 'ci') { installs.push(options.cwd); mkdirSync(path.join(options.cwd, 'node_modules'), { recursive: true }); }
      child.emit('exit', 0);
    });
    return child;
  };
  const module = { exports: {} };
  const ts = require('typescript');
  const output = ts.transpileModule(await fs.readFile(new URL('../src/main/source-build.ts', import.meta.url), 'utf8'), { compilerOptions: {
    module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022, esModuleInterop: true } }).outputText;
  vm.runInNewContext(output + '\nmodule.exports.nodePackage = nodePackage; module.exports.nodeIdentity = nodeIdentity; module.exports.pythonRuntime = pythonRuntime;', {
    module, exports: module.exports, process, console, Buffer,
    require: name => name === 'node:child_process' ? { spawn } : require(name),
  });
  const npm = path.join(root, 'node/node_modules/npm/bin/npm-cli.js');
  await fs.mkdir(path.dirname(npm), { recursive: true });
  await fs.writeFile(path.join(root, 'node/node_modules/npm/package.json'), JSON.stringify({ version: '10.9.4' }));
  async function source(name, lock = '{"lockfileVersion":3}') {
    const dir = path.join(root, `source-${Math.random().toString(16).slice(2)}`);
    await fs.mkdir(path.join(dir, name), { recursive: true });
    await fs.writeFile(path.join(dir, name, 'package.json'), '{"name":"x"}');
    await fs.writeFile(path.join(dir, name, 'package-lock.json'), lock);
    return dir;
  }
  const options = (src, runtimeKey, shared = true) => ({
    source: src, cache: path.join(root, 'dependencies', runtimeKey), sharedCache: shared ? path.join(root, 'dependencies') : undefined,
    runtime: { root, python: 'python', node: 'node', npm, pythonDependencies: null }, revision: 'r', log: path.join(root, 'build.log'), progress() {},
  });
  return { ...module.exports, root, installs, identity, source, options };
}

test('npm caches survive a native runtime rebuild with the same Node and npm', async t => {
  const f = await load(t);
  const first = f.options(await f.source('tools'), 'runtime-a');
  const id = await f.nodeIdentity(first);
  assert.equal(id, 'v22.23.2 win32 x64 127 npm 10.9.4');
  const cached = await f.nodePackage('tools', first, 'python', id);
  assert.equal(f.installs.length, 1);
  assert.ok(cached.startsWith(path.join(f.root, 'dependencies', 'node', 'tools') + path.sep), cached);
  // A new runtime key with the same Node build reuses the installed packages.
  assert.equal(await f.nodePackage('tools', f.options(await f.source('tools'), 'runtime-b'), 'python', id), cached);
  assert.equal(f.installs.length, 1);
});

test('a different Node build, npm or lockfile never reuses an npm cache', async t => {
  const f = await load(t);
  const opts = f.options(await f.source('tools'), 'runtime-a');
  const base = await f.nodePackage('tools', opts, 'python', await f.nodeIdentity(opts));
  f.identity.value = 'v22.24.0 win32 x64 127';
  const otherNode = await f.nodePackage('tools', f.options(await f.source('tools'), 'runtime-a'), 'python', await f.nodeIdentity(opts));
  await fs.writeFile(path.join(f.root, 'node/node_modules/npm/package.json'), JSON.stringify({ version: '11.0.0' }));
  const otherNpm = await f.nodePackage('tools', f.options(await f.source('tools'), 'runtime-a'), 'python', await f.nodeIdentity(opts));
  const otherLock = await f.nodePackage('tools', f.options(await f.source('tools', '{"lockfileVersion":3,"x":1}'), 'runtime-a'), 'python', await f.nodeIdentity(opts));
  assert.equal(new Set([base, otherNode, otherNpm, otherLock]).size, 4);
  assert.equal(f.installs.length, 4);
});

test('without a shared root or a known Node build, npm caches stay inside their runtime', async t => {
  const f = await load(t);
  const unshared = f.options(await f.source('tools'), 'runtime-a', false);
  assert.ok((await f.nodePackage('tools', unshared, 'python', null)).startsWith(path.join(unshared.cache, 'node') + path.sep));
  const unknown = f.options(await f.source('tools'), 'runtime-b');
  await fs.rm(path.join(f.root, 'node/node_modules/npm/package.json'));
  assert.equal(await f.nodeIdentity(unknown), null);
  assert.ok((await f.nodePackage('tools', unknown, 'python', null)).startsWith(path.join(unknown.cache, 'node') + path.sep));
});

const PYPROJECT = '[project]\nname = "fluid"\ndependencies = ["httpx>=0.27"]\n\n[tool.pytest.ini_options]\nasyncio_mode = "auto"\npythonpath = [".."]\n\n[tool.uv]\npackage = true\n';

test('only Fluid\'s pytest settings are left out of the Python key, the same way in both implementations', async t => {
  const f = await load(t);
  const root = await fs.mkdtemp(path.join(os.tmpdir(), 'shuffle-python-key-'));
  t.after(() => fs.rm(root, { recursive: true, force: true, maxRetries: 10, retryDelay: 100 }));
  await fs.mkdir(path.join(root, 'fluid'));
  for (const name of ['requirements.lock', 'requirements-native.lock', 'requirements-voice.lock']) await fs.writeFile(path.join(root, name), `# ${name}`);
  const key = async (text) => {
    await fs.writeFile(path.join(root, 'fluid/pyproject.toml'), text);
    const [build, script] = [await f.pythonDependencyKey(root), await pythonDependencies(root)];
    assert.equal(build, script, 'source-build.ts and runtime-contract.mjs must agree');
    return build;
  };
  const base = await key(PYPROJECT);
  assert.equal(await key(PYPROJECT.replace('pythonpath = [".."]', 'pythonpath = ["../x"]\ntestpaths = ["tests"]')), base, 'pytest-only edit');
  assert.equal(await key(PYPROJECT.replace(/\n/g, '\r\n')), base, 'line endings');
  assert.notEqual(await key(PYPROJECT.replace('httpx>=0.27', 'httpx>=0.28')), base, '[project] edit');
  assert.notEqual(await key(PYPROJECT.replace('package = true', 'package = false')), base, 'other tool tables still count');
  assert.notEqual(await key(PYPROJECT.replace('[tool.pytest.ini_options]', '[tool.pytest.ini_options]\n[build-system]\nrequires = ["x"]')), base, 'a table after the pytest header counts');
  // A multi-line string could hide a header line, so such a file counts whole.
  const quoted = PYPROJECT.replace('name = "fluid"', 'name = "fluid"\ndescription = """\n[tool.pytest.x]\n"""');
  assert.notEqual(await key(quoted.replace('asyncio_mode = "auto"', 'asyncio_mode = "strict"')), await key(quoted));
  assert.equal(f.pythonInputText('requirements.lock', '[tool.pytest]\nx'), '[tool.pytest]\nx');
  assert.equal(scriptRule('fluid/pyproject.toml', PYPROJECT), f.pythonInputText('fluid/pyproject.toml', PYPROJECT));
});

test('a runtime recorded with the whole-file Python key still matches unchanged inputs', async t => {
  const f = await load(t);
  const source = path.join(f.root, 'py');
  await fs.mkdir(path.join(source, 'fluid'), { recursive: true });
  for (const name of ['requirements.lock', 'requirements-native.lock', 'requirements-voice.lock']) await fs.writeFile(path.join(source, name), `# ${name}`);
  await fs.writeFile(path.join(source, 'fluid/pyproject.toml'), PYPROJECT);
  const { createHash } = await import('node:crypto');
  const legacy = createHash('sha256');
  for (const name of ['requirements.lock', 'requirements-native.lock', 'requirements-voice.lock', 'fluid/pyproject.toml']) legacy.update(name + '\0' + await fs.readFile(path.join(source, name), 'utf8') + '\0');
  const opts = { ...f.options(source, 'runtime-a'), source };
  assert.equal(await f.pythonRuntime({ ...opts, runtime: { ...opts.runtime, pythonDependencies: legacy.digest('hex') } }), null);
  assert.equal(await f.pythonRuntime({ ...opts, runtime: { ...opts.runtime, pythonDependencies: await f.pythonDependencyKey(source) } }), null);
});
