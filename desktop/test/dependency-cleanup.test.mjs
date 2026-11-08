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
  // R: the running build, recorded nowhere in state.json; its folder still protects its set,
  // which only its link names (its record lists no npm sets).
  await file(d('node/frontend', gen('6'), 'node_modules/.package-lock.json'));
  await file(path.join(root, 'versions', R, 'build.json'), JSON.stringify({ revision: R, runtimeKey: K1, pythonKey: P1, layout: 2,
    electron: d('node/desktop', gen('f'), 'node_modules/electron/dist/electron.exe') }));
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
  // Review of #1276: a link above a folder it lists must stop everything, not only a link at the folder itself.
  ['updates/dependencies itself is a link', async ({ root, deps }) => { await fs.rename(deps, path.join(root, '..', 'dependencies-real')); await link(path.join(root, '..', 'dependencies-real'), deps); }],
  ['a package folder is a link to somewhere outside', async ({ d, outside }) => { await fs.rename(d('node/tools'), path.join(outside, 'tools')); await link(path.join(outside, 'tools'), d('node/tools')); }],
  ['the npm folder is a link into a runtime tree', async ({ d, tree1 }) => { await fs.rename(d('node'), d(tree1, 'node')); await link(d(tree1, 'node'), d('node')); }],
  ['the running build is missing its record', async ({ root }) => fs.rm(path.join(root, 'versions', R, 'build.json'))],
  ['the active build sits under a temporary name with no record', async ({ root }) => { await fs.rm(path.join(root, 'versions', A, 'build.json')); await fs.rename(path.join(root, 'versions', A), path.join(root, 'versions', A + '.restoring')); }],
  ['a kept older build has files but no record', async ({ root }) => fs.rm(path.join(root, 'versions', B, 'build.json'))],
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

for (const [name, before, swap] of [
  // Review of #1276: a folder is swapped for a junction after the inventory, just before a delete.
  // The root swap lands right before a whole-tree delete, which has no folder between it and the root.
  ['updates/dependencies', ORPHAN, async ({ deps }, decoy) => { await fs.rename(deps, deps + '-moved'); await link(decoy, deps); }],
  ['node/tools', `node/tools/${gen('0')}`, async ({ d }, decoy) => { await fs.rename(d('node/tools'), d('node/tools-moved')); await link(path.join(decoy, 'node/tools'), d('node/tools')); }],
]) test(`nothing more is deleted when ${name} becomes a junction after the inventory`, async t => {
  const f = await fixture(t);
  // The junction target holds folders with exactly the names the inventory chose to delete.
  const decoy = path.join(f.outside, 'decoy');
  for (const inside of [path.join('node/tools', gen('0')), ORPHAN]) await file(path.join(decoy, inside, 'keep.txt'), 'must survive');
  let swapped = false, after = [];
  const result = await pruneDependencies(f.root, R, quiet, { beforeRemove: async candidate => {
    if (swapped) after.push(candidate);
    if (candidate === before && !swapped) { swapped = true; after.push(candidate); await swap(f, decoy); }
  } });
  assert.ok(swapped, `the inventory chose ${before}`);
  assert.match(result.skipped, /changed after the inventory|is a link/);
  assert.deepEqual(after, [before], 'the cleanup stops at the first changed folder');
  assert.ok(!result.removed.includes(before));
  for (const inside of [path.join('node/tools', gen('0')), ORPHAN]) assert.equal(await fs.readFile(path.join(decoy, inside, 'keep.txt'), 'utf8'), 'must survive');
  assert.equal(await exists(path.join(f.root, 'update.lock')), false);
});

test('a rejected set\'s marker file is not deleted through node/tools swapped for a junction after the set is removed', async t => {
  // Review of #1276: the set's removal can take long, and node/tools may be swapped before its marker file goes.
  const f = await fixture(t);
  const set = `node/tools/${gen('0')}`, decoy = path.join(f.outside, 'decoy');
  const marker = path.join(decoy, 'node/tools', '.invalid-' + gen('0'));
  await file(marker, 'must survive');
  let swapped = false;
  const result = await pruneDependencies(f.root, R, quiet, { beforeTombstone: async candidate => {
    if (candidate === set && !swapped) { swapped = true; await fs.rename(f.d('node/tools'), f.d('node/tools-moved')); await link(path.join(decoy, 'node/tools'), f.d('node/tools')); }
  } });
  assert.ok(swapped, `the inventory chose ${set}`);
  assert.match(result.skipped, /changed after the inventory|is a link/);
  assert.equal(await fs.readFile(marker, 'utf8'), 'must survive');
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

test('an active build under a temporary name is still read, so everything it runs from survives', async t => {
  // Review of #1276: an active build being put back sits in versions/<revision>.restoring.
  const f = await fixture(t);
  await fs.rm(path.join(f.root, 'versions', R), { recursive: true, maxRetries: 10, retryDelay: 100 });
  await fs.rename(path.join(f.root, 'versions', A), path.join(f.root, 'versions', A + '.restoring'));
  const result = await pruneDependencies(f.root, A, quiet);
  assert.equal(result.skipped, undefined);
  for (const kept of [f.d(f.tree1, 'python', P1.slice(0, 24), 'Scripts/python.exe'), f.d(f.tree1, 'cloudflared/cloudflared.exe'),
    f.d('node/desktop', gen('f'), 'node_modules/electron/dist/electron.exe'), f.d('node/browser-mcp', gen('9'), 'node_modules')]) assert.ok(await exists(kept), kept);
  assert.ok(result.removed.includes(`node/frontend/${gen('6')}`), 'the set only the removed build used is gone');
});

test('a lock that cannot be written is closed and removed again, and another process\'s lock is never removed', async t => {
  // Review of #1276: an empty leftover lock blocks update checks for ten minutes.
  const f = await fixture(t);
  const promises = require('node:fs').promises, open = promises.open;
  t.after(() => { promises.open = open; });
  let closed = 0;
  promises.open = async (...args) => {
    const handle = await open(...args);
    if (!String(args[0]).endsWith('update.lock')) return handle;
    return { writeFile: async () => { throw Object.assign(new Error('no space left on device'), { code: 'ENOSPC' }); }, close: async () => { closed++; await handle.close(); } };
  };
  const result = await pruneDependencies(f.root, R, quiet);
  promises.open = open;
  assert.match(result.skipped, /no space/);
  assert.equal(closed, 1);
  assert.equal(await exists(path.join(f.root, 'update.lock')), false);
  assert.ok(await exists(f.d(ORPHAN)));
  await fs.writeFile(path.join(f.root, 'update.lock'), '4242');
  assert.match((await pruneDependencies(f.root, R, quiet)).skipped, /update is running/);
  assert.equal(await fs.readFile(path.join(f.root, 'update.lock'), 'utf8'), '4242');
});

test('a staged or pending build keeps what it links, and a missing updates folder is not an error', async t => {
  const f = await fixture(t);
  const staged = hex('e', 40);
  await file(path.join(f.root, 'versions', staged, 'build.json'), JSON.stringify({ revision: staged, runtimeKey: K1, pythonKey: null, layout: 2,
    electron: f.d('node/desktop', gen('f'), 'node_modules/electron/dist/electron.exe') }));
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
