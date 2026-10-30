import assert from 'node:assert/strict';
import { test } from 'node:test';
import { promises as fs, mkdirSync, writeFileSync } from 'node:fs';
import { EventEmitter } from 'node:events';
import os from 'node:os';
import path from 'node:path';
import vm from 'node:vm';
import { createRequire } from 'node:module';
const require = createRequire(import.meta.url);

async function fixture(t) {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), 'shuffle-prebuilt-build-'));
  t.after(() => fs.rm(root, { recursive: true, force: true, maxRetries: 10, retryDelay: 100 }));
  const source = path.join(root, 'source');
  for (const name of ['app', 'fluid/fluid', 'frontend', 'tools', 'browser-mcp', 'desktop']) await fs.mkdir(path.join(source, name), { recursive: true });
  for (const name of ['requirements.lock', 'requirements-native.lock', 'requirements-voice.lock', 'fluid/pyproject.toml']) await fs.writeFile(path.join(source, name), '# locked fixture');
  await fs.writeFile(path.join(source, 'app/main.py'), '# current app');
  await fs.writeFile(path.join(source, 'fluid/fluid/__init__.py'), '# current Fluid');
  for (const name of ['frontend', 'tools', 'browser-mcp', 'desktop']) {
    await fs.writeFile(path.join(source, name, 'package.json'), JSON.stringify({ name }));
    await fs.writeFile(path.join(source, name, 'package-lock.json'), '{}');
  }
  const calls = [];
  const spawn = (exe, args, options) => {
    calls.push({ exe, args, cwd: options.cwd });
    const child = new EventEmitter();
    const put = (relative, content = '') => {
      const target = path.join(options.cwd, relative);
      mkdirSync(path.dirname(target), { recursive: true }); writeFileSync(target, content);
    };
    if (args[1] === 'ci') {
      put('node_modules/.installed');
      put(`node_modules/electron/dist/${process.platform === 'win32' ? 'electron.exe' : 'electron'}`);
    }
    if (args[1] === 'run') {
      if (path.basename(options.cwd) === 'frontend') { put('build/index.html', 'local desktop'); put('build/mobile/index.html', 'local phone'); }
      else put('dist/index.js', 'fresh output');
    }
    if (args.includes('app.core.cloudflared')) put('downloads/verified-runtime', 'fixture');
    setImmediate(() => child.emit('exit', 0));
    return child;
  };
  const module = { exports: {} };
  const input = await fs.readFile(new URL('../src/main/source-build.ts', import.meta.url), 'utf8');
  const ts = require('typescript');
  const output = ts.transpileModule(input, { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022, esModuleInterop: true } }).outputText;
  vm.runInNewContext(output, { module, exports: module.exports, process, console, Buffer,
    require: name => name === 'node:child_process' ? { spawn } : require(name) });
  const opts = { source, server: path.join(root, 'server'), cache: path.join(root, 'cache'), log: path.join(root, 'build.log'),
    runtime: { python: 'fake-python', node: process.execPath, npm: 'npm-cli.js', pythonDependencies: await module.exports.pythonDependencyKey(source) },
    revision: 'b'.repeat(40), progress() {} };
  return { ...module.exports, root, opts, calls };
}

for (const prebuilt of [true, false]) {
  test(`signed UI handoff skips exactly the frontend subprocesses: prebuilt=${prebuilt}`, async t => {
    const f = await fixture(t);
    if (prebuilt) {
      const ui = path.join(f.root, 'verified-ui');
      await fs.mkdir(path.join(ui, 'mobile'), { recursive: true });
      await fs.writeFile(path.join(ui, 'index.html'), 'verified desktop');
      await fs.writeFile(path.join(ui, 'mobile/index.html'), 'verified phone');
      f.opts.prebuiltUI = ui;
    }
    await f.buildSource(f.opts);
    const frontend = f.calls.filter(call => path.basename(call.cwd) === 'frontend' || call.cwd.includes(`${path.sep}node${path.sep}frontend${path.sep}`));
    assert.equal(frontend.length, prebuilt ? 0 : 2, 'a bundle hit must not install or compile the frontend');
    const staticRoot = path.join(f.opts.server, 'app/static');
    assert.equal(await fs.readFile(path.join(staticRoot, 'index.html'), 'utf8'), prebuilt ? 'verified desktop' : 'local desktop');
    assert.equal(await fs.readFile(path.join(staticRoot, 'mobile/index.html'), 'utf8'), prebuilt ? 'verified phone' : 'local phone');
    assert.equal(await fs.readFile(path.join(f.opts.server, 'fluid/__init__.py'), 'utf8'), '# current Fluid');
  });
}
