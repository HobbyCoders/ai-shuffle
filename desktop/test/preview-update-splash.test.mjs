import assert from 'node:assert/strict';
import { EventEmitter } from 'node:events';
import { promises as fs, readFileSync } from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { test } from 'node:test';
import { createRequire } from 'node:module';
import { previewOptions, previewUpdateSplash } from '../scripts/preview-update-splash.mjs';
const require = createRequire(import.meta.url);
const { parseSourceState } = require('../dist/main/source-updates.js');
const { splashPhase } = require('../dist/main/update-splash.js');

for (const phase of ['checking', 'starting']) test(`manual ${phase} preview uses synthetic state and removes only its own directory`, async t => {
 const parent = await fs.mkdtemp(path.join(os.tmpdir(), 'shuffle-splash-preview-'));
 t.after(() => fs.rm(parent, { recursive: true, force: true, maxRetries: 10, retryDelay: 100 }));
 const root = path.join(parent, 'new-root');
 const options = previewOptions(['--phase', phase, '--root', root, '--electron', process.execPath, '--release', '1.0.2']);
 const code = await previewUpdateSplash(options, (electron, args, settings) => {
  assert.equal(electron, process.execPath);
  assert.match(args[0], /update-splash-main\.js$/);
  assert.ok(args.includes(`--profile=${path.join(root, 'profile')}`));
  assert.ok(!args.some(arg => arg.startsWith('--launcher=')));
  assert.equal(settings.env.ELECTRON_RUN_AS_NODE, undefined);
  const state = parseSourceState(readFileSync(path.join(root, 'state.json'), 'utf8'));
  assert.equal(splashPhase(state, 'b'.repeat(40)), phase);
  const child = new EventEmitter(); child.kill = () => {};
  queueMicrotask(() => child.emit('close', 0, null));
  return child;
 });
 assert.equal(code, 0);
 await assert.rejects(fs.stat(root), { code: 'ENOENT' });
});

test('manual preview refuses existing roots and invalid phases without touching their files', async t => {
 const root = await fs.mkdtemp(path.join(os.tmpdir(), 'shuffle-splash-preview-existing-'));
 t.after(() => fs.rm(root, { recursive: true, force: true, maxRetries: 10, retryDelay: 100 }));
 await fs.writeFile(path.join(root, 'state.json'), 'keep');
 await assert.rejects(previewUpdateSplash({ root, phase: 'checking', electron: process.execPath }, () => assert.fail('must not spawn')), { code: 'EEXIST' });
 assert.equal(await fs.readFile(path.join(root, 'state.json'), 'utf8'), 'keep');
 assert.throws(() => previewOptions(['--phase', 'attached', '--root', root]), /Usage/);
 assert.throws(() => previewOptions(['--phase', 'checking', '--root', 'relative']), /Usage/);
});
