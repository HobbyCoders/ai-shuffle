import assert from 'node:assert/strict';
import { promises as fs } from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { createRequire } from 'node:module';
import { test } from 'node:test';
const require = createRequire(import.meta.url);
const { pruneDependencies } = require('../dist/main/dependency-cleanup.js');

const hex = (c, n) => c.repeat(n);
const A = hex('a', 40), B = hex('b', 40), R = hex('c', 40), FAILED = hex('d', 40);
const K1 = hex('1', 64), K2 = hex('2', 64), P1 = hex('3', 64), P0 = hex('4', 64), ORPHAN = hex('5', 24);
const gen = (c, suffix = '-mabc12-0123abcd') => hex(c, 24) + suffix;
const LINK = process.platform === 'win32' ? 'junction' : 'dir';
const quiet = { log() {}, warn() {} };

async function dir(file) { await fs.mkdir(file, { recursive: true }); return file; }
async function file(name, body = 'x') { await dir(path.dirname(name)); await fs.writeFile(name, body); }
async function exists(name) { try { await fs.lstat(name); return true; } catch (error) { if (error.code === 'ENOENT') return false; throw error; } }
async function link(target, at) { await dir(path.dirname(at)); await fs.symlink(target, at, LINK); }

/** A real-shaped updates root: two retained versions, the running one, and leftovers from older ones. */
async function fixture(t) {
  const base = await fs.mkdtemp(path.join(os.tmpdir(), 'shuffle-dependency-cleanup-'));
  t.after(() => fs.rm(base, { recursive: true, force: true, maxRetries: 10, retryDelay: 100 }));
  const root = path.join(base, 'updates'), deps = path.join(root, 'dependencies'), outside = await dir(path.join(base, 'outside'));
  await file(path.join(outside, 'sentinel'), 'must survive');
  const d = (...parts) => path.join(deps, ...parts);
  const tree1 = K1.slice(0, 24);
  // A (active, layout 2): shared npm sets; browser-mcp's set is only in its link, not in build.json.
  await file(d(tree1, 'python', P1.slice(0, 24), 'Scripts/python.exe'));
  await file(d(tree1, 'cloudflared', 'cloudflared.exe'));
  for (const [pkg, c] of [['tools', 'e'], ['desktop', 'f'], ['browser-mcp', '9']]) await file(d('node', pkg, gen(c), 'node_modules', '.package-lock.json'));
  await file(d('node/desktop', gen('f'), 'node_modules/electron/dist/electron.exe'));
  await file(path.join(root, 'versions', A, 'build.json'), JSON.stringify({ revision: A, runtimeKey: K1, pythonKey: P1, layout: 2,
    electron: d('node/desktop', gen('f'), 'node_modules/electron/dist/electron.exe'), nodeGenerations: [`node/tools/${gen('e')}`, `node/desktop/${gen('f')}`] }));
  for (const base of ['source', 'server']) await link(d('node/tools', gen('e'), 'node_modules'), path.join(root, 'versions', A, base, 'tools/node_modules'));
  await link(d('node/desktop', gen('f'), 'node_modules'), path.join(root, 'versions', A, 'source/desktop/node_modules'));
  await link(d('node/browser-mcp', gen('9'), 'node_modules'), path.join(root, 'versions', A, 'source/browser-mcp/node_modules'));
  await link(path.join(root, 'versions', A, 'source/frontend/node_modules'), path.join(root, 'versions', A, 'source/mobile/node_modules'));
  await dir(path.join(root, 'versions', A, 'source/frontend/node_modules'));
  // B (previous, legacy layout): full-key tree, no nodeGenerations, an npm set inside its own tree.
  await file(d(K2, 'node/tools', gen('7'), 'node_modules/.package-lock.json'));
  await file(d(K2, 'node/desktop', gen('8'), 'node_modules/electron/dist/electron.exe'));
  await file(path.join(root, 'versions', B, 'build.json'), JSON.stringify({ revision: B, runtimeKey: K2, pythonKey: null, electron: d(K2, 'node/desktop', gen('8'), 'node_modules/electron/dist/electron.exe') }));
  await link(d(K2, 'node/tools', gen('7'), 'node_modules'), path.join(root, 'versions', B, 'source/tools/node_modules'));
  // R: the running build, recorded nowhere in state.json; its folder still protects its set.
  await file(d('node/frontend', gen('6'), 'node_modules/.package-lock.json'));
  await link(d('node/frontend', gen('6'), 'node_modules'), path.join(root, 'versions', R, 'source/frontend/node_modules'));
  await file(path.join(root, 'state.json'), JSON.stringify({ schemaVersion: 1, active: A, previous: B, failed: [FAILED] }));
  // Leftovers no kept folder uses.
  await file(d(tree1, 'python', P0.slice(0, 24), 'Scripts/python.exe'));
  await file(d(tree1, 'python', P1.slice(0, 24) + '.tmp-abcdef12', 'pyvenv.cfg'));
  await file(d('node/tools', gen('0'), 'node_modules/.package-lock.json'));
  await file(d('node/tools', '.invalid-' + gen('0')), '');
  await file(d('node/tools', gen('e', '-mabc99-0123abcd.tmp-abcdef12'), 'package.json'));
  await file(d(K2, 'node/tools', gen('0'), 'node_modules/.package-lock.json'));
  await file(d(ORPHAN, 'python', hex('6', 24), 'Scripts/python.exe'));
  await link(outside, d(ORPHAN, 'python', hex('6', 24), 'Lib/linked-outside')); // must be removed as a link, never entered
  // Never touched: unknown names, build outputs, a top-level link, and runtimes/.
  await file(d('builds/tools', hex('7', 64), 'dist/index.js'));
  await file(d('notes.txt'));
  await link(outside, d(hex('8', 24)));
  await file(path.join(root, 'runtimes', hex('9', 24), 'postgres/bin/postgres.exe'));
  return { root, deps, d, outside, tree1 };
}

test('unused dependency trees and npm sets are removed; everything a kept or running build uses survives', async t => {
  const { root, d, outside, tree1 } = await fixture(t);
  const result = await pruneDependencies(root, R, quiet);
  assert.equal(result.skipped, undefined);
  assert.deepEqual(result.failed, []);
  assert.deepEqual(result.removed.sort(), [
    ORPHAN,
    `${K2}/node/tools/${gen('0')}`,
    `${tree1}/python/${P0.slice(0, 24)}`,
    `${tree1}/python/${P1.slice(0, 24)}.tmp-abcdef12`,
    `node/tools/${gen('0')}`,
    `node/tools/${gen('e', '-mabc99-0123abcd.tmp-abcdef12')}`,
  ].sort());
  for (const gone of [d(ORPHAN), d('node/tools', gen('0')), d('node/tools', '.invalid-' + gen('0')), d(K2, 'node/tools', gen('0')), d(tree1, 'python', P0.slice(0, 24))]) assert.equal(await exists(gone), false, gone);
  for (const kept of [d(tree1, 'python', P1.slice(0, 24), 'Scripts/python.exe'), d(tree1, 'cloudflared/cloudflared.exe'),
    d('node/tools', gen('e'), 'node_modules'), d('node/desktop', gen('f'), 'node_modules/electron/dist/electron.exe'),
    d('node/browser-mcp', gen('9'), 'node_modules'), d('node/frontend', gen('6'), 'node_modules'),
    d(K2, 'node/tools', gen('7'), 'node_modules'), d(K2, 'node/desktop', gen('8'), 'node_modules'),
    d('builds/tools', hex('7', 64), 'dist/index.js'), d('notes.txt'), d(hex('8', 24)),
    path.join(root, 'runtimes', hex('9', 24), 'postgres/bin/postgres.exe')]) assert.ok(await exists(kept), kept);
  assert.equal(await fs.readFile(path.join(outside, 'sentinel'), 'utf8'), 'must survive');
  assert.equal(await exists(path.join(root, 'update.lock')), false);
  // Nothing is left to do on the next start.
  assert.deepEqual((await pruneDependencies(root, R, quiet)).removed, []);
});

for (const [name, damage] of [
  ['a kept build record cannot be read', async ({ root }) => fs.writeFile(path.join(root, 'versions', B, 'build.json'), '{')],
  ['a link points outside the update folders', async ({ root, outside }) => link(outside, path.join(root, 'versions', B, 'server/tools/node_modules'))],
  ['the restore journal cannot be read', async ({ root }) => fs.writeFile(path.join(root, 'restore-point.json'), 'not json')],
  ['the update state cannot be read', async ({ root }) => fs.writeFile(path.join(root, 'state.json'), '[]')],
]) test(`nothing is removed when ${name}`, async t => {
  const f = await fixture(t);
  await damage(f);
  const result = await pruneDependencies(f.root, R, quiet);
  assert.ok(result.skipped, 'cleanup must report why it kept everything');
  assert.deepEqual(result.removed, []);
  assert.ok(await exists(f.d(ORPHAN)));
  assert.ok(await exists(f.d('node/tools', gen('0'))));
  assert.equal(await exists(path.join(f.root, 'update.lock')), false);
});

test('a first start that is not healthy yet, or an update holding the lock, leaves everything', async t => {
  const f = await fixture(t);
  await fs.writeFile(path.join(f.root, 'state.json'), JSON.stringify({ schemaVersion: 1, active: A, previous: B, booting: R }));
  assert.match((await pruneDependencies(f.root, R, quiet)).skipped, /first start/);
  await fs.writeFile(path.join(f.root, 'state.json'), JSON.stringify({ schemaVersion: 1, active: A, previous: B }));
  await fs.writeFile(path.join(f.root, 'update.lock'), String(process.pid));
  assert.match((await pruneDependencies(f.root, R, quiet)).skipped, /update is running/);
  assert.equal(await fs.readFile(path.join(f.root, 'update.lock'), 'utf8'), String(process.pid));
  assert.ok(await exists(f.d(ORPHAN)));
});

test('a staged or pending build keeps what it links, and a missing updates folder is not an error', async t => {
  const f = await fixture(t);
  const staged = hex('e', 40);
  await link(f.d(ORPHAN, 'python'), path.join(f.root, 'versions', staged, 'server/tools/node_modules'));
  await link(f.d('node/tools', gen('0'), 'node_modules'), path.join(f.root, 'staging/source/tools/node_modules'));
  await fs.writeFile(path.join(f.root, 'state.json'), JSON.stringify({ schemaVersion: 1, active: A, previous: B, staged }));
  const result = await pruneDependencies(f.root, R, quiet);
  assert.equal(result.skipped, undefined);
  assert.ok(await exists(f.d(ORPHAN, 'python')));
  assert.ok(await exists(f.d('node/tools', gen('0'))));
  assert.equal(await exists(f.d(f.tree1, 'python', P0.slice(0, 24))), false);
  const empty = await pruneDependencies(path.join(f.root, 'missing'), null, quiet);
  assert.deepEqual(empty.removed, []);
});
