import assert from 'node:assert/strict';
import { existsSync, promises as fs } from 'node:fs';
import Module from 'node:module';
import os from 'node:os';
import path from 'node:path';
import { test } from 'node:test';
import { installedElectron } from './helpers/electron-binary.mjs';

// Issue #1495: only fetch-electron.mjs may download Electron. Here the prefetch
// "failed" (nothing installed), then the binary became available. The test-side
// resolver must report each state without loading the package, whose index.js
// downloads. This fake index.js leaves a marker and installs if it is ever run.
test('the test-side Electron resolver never loads the package, so it never downloads', async t => {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), 'shuffle-electron-binary-'));
  t.after(() => fs.rm(root, { recursive: true, force: true }));
  const pkg = path.join(root, 'node_modules', 'electron');
  const marker = path.join(root, 'downloaded');
  const binary = process.platform === 'win32' ? 'electron.exe' : 'electron';
  await fs.mkdir(pkg, { recursive: true });
  await fs.writeFile(path.join(pkg, 'package.json'), JSON.stringify({ name: 'electron', version: '0.0.0', main: 'index.js' }));
  await fs.writeFile(path.join(pkg, 'index.js'), `const fs = require('fs'), path = require('path');
    fs.writeFileSync(${JSON.stringify(marker)}, 'downloaded');
    fs.mkdirSync(path.join(__dirname, 'dist'), { recursive: true });
    fs.writeFileSync(path.join(__dirname, 'dist', ${JSON.stringify(binary)}), 'binary');
    fs.writeFileSync(path.join(__dirname, 'path.txt'), ${JSON.stringify(binary)});
    module.exports = path.join(__dirname, 'dist', ${JSON.stringify(binary)});`);

  const load = Module._load;
  const loaded = [];
  Module._load = function (request, ...rest) {
    if (request === 'electron' || request.startsWith(pkg)) loaded.push(request);
    return load.call(this, request, ...rest);
  };
  t.after(() => { Module._load = load; });
  const env = {};

  assert.equal(installedElectron(pkg, env), null, 'no path.txt and no binary');
  await fs.mkdir(path.join(pkg, 'dist'));
  await fs.writeFile(path.join(pkg, 'dist', binary), 'partial');
  assert.equal(installedElectron(pkg, env), null, 'an extraction without path.txt is not complete');
  await fs.rm(path.join(pkg, 'dist', binary));
  await fs.writeFile(path.join(pkg, 'path.txt'), binary);
  assert.equal(installedElectron(pkg, env), null, 'path.txt naming a missing binary');
  await fs.writeFile(path.join(pkg, 'dist', binary), 'binary');
  assert.equal(installedElectron(pkg, env), path.join(pkg, 'dist', binary), 'path.txt and the binary');

  const override = path.join(root, 'override');
  assert.equal(installedElectron(pkg, { ELECTRON_OVERRIDE_DIST_PATH: override }), null, 'override dist without the binary');
  await fs.mkdir(override);
  await fs.writeFile(path.join(override, binary), 'binary');
  assert.equal(installedElectron(pkg, { ELECTRON_OVERRIDE_DIST_PATH: override }), path.join(override, binary));

  assert.equal(existsSync(marker), false, 'the resolver ran the downloading index.js');
  assert.deepEqual(loaded, [], 'the resolver loaded the electron package');
});
