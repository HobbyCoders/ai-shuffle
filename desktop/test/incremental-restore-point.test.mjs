import assert from 'node:assert/strict';
import { promises as fs } from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { createRequire } from 'node:module';
import { test } from 'node:test';
import { rollbackFixture, B, C } from './helpers/source-updates-fixture.mjs';
const require = createRequire(import.meta.url);
const nativeFs = require('node:fs');
const { IncrementalClusterCopy } = require('../dist/main/incremental-restore-point.js');
const { ColdClusterCopy } = require('../dist/main/database-restore.js');

async function cluster(t) {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), 'shuffle-incremental-'));
  t.after(() => fs.rm(root, { recursive: true, force: true, maxRetries: 10, retryDelay: 100 }));
  for (const [name, body] of Object.entries({ PG_VERSION: '16', 'global/pg_control': 'control', 'base/1/rows': 'original rows', 'base/1/unchanged': 'untouched' })) {
    await fs.mkdir(path.dirname(path.join(root, 'data', name)), { recursive: true });
    await fs.writeFile(path.join(root, 'data', name), body);
  }
  await fs.mkdir(path.join(root, 'data', 'empty'));
  const calls = [];
  return { root, calls, copy: new IncrementalClusterCopy(root, async snapshot => { calls.push(snapshot); }) };
}
function reads(t, root) {
  const files = [], original = nativeFs.createReadStream;
  t.mock.method(nativeFs, 'createReadStream', (file, ...args) => {
    if (String(file).startsWith(path.join(root, 'data') + path.sep)) files.push(path.relative(path.join(root, 'data'), file));
    return original(file, ...args);
  });
  return files;
}
const legacy = root => new ColdClusterCopy(root, async () => {});

test('first point produces the legacy manifest; next point reads only changed files and retains empty folders', async t => {
  const { root, copy, calls } = await cluster(t), files = reads(t, root);
  await copy.snapshot();
  assert.equal(files.length, 4);
  await legacy(root).validate(); // real installed launcher consumer
  await copy.discard();
  files.length = 0;
  await fs.writeFile(path.join(root, 'data/base/1/rows'), 'updated rows');
  await fs.writeFile(path.join(root, 'data/new'), 'new');
  await fs.rm(path.join(root, 'data/empty'), { recursive: true });
  await fs.mkdir(path.join(root, 'data/new-empty'));
  await copy.snapshot();
  assert.deepEqual(files.sort(), [path.join('base', '1', 'rows'), 'new'].sort());
  await legacy(root).validate();
  await fs.writeFile(path.join(root, 'data/base/1/rows'), 'bad migration');
  await legacy(root).restore();
  assert.equal(await fs.readFile(path.join(root, 'data/base/1/rows'), 'utf8'), 'updated rows');
  assert.ok((await fs.stat(path.join(root, 'data/new-empty'))).isDirectory());
  await assert.rejects(fs.stat(path.join(root, 'data/empty')), { code: 'ENOENT' });
  assert.deepEqual(calls, [true, true]);
});

test('an unchanged multi-file cluster is not copied or hashed in the app', async t => {
  const { root, copy } = await cluster(t);
  await copy.snapshot(); await legacy(root).validate(); await copy.discard();
  const files = reads(t, root);
  await copy.snapshot();
  assert.deepEqual(files, []);
  await legacy(root).validate();
  // No hardlinks to live data: in-place writes never alter the point.
  await fs.writeFile(path.join(root, 'data/base/1/unchanged'), 'overwritten');
  await legacy(root).validate();
  await legacy(root).restore();
  assert.equal(await fs.readFile(path.join(root, 'data/base/1/unchanged'), 'utf8'), 'untouched');
});

test('same-size source edits with a restored mtime are not reused', async t => {
  const { root, copy } = await cluster(t);
  await copy.snapshot(); await copy.discard();
  const file = path.join(root, 'data/base/1/rows'), before = await fs.stat(file);
  await fs.writeFile(file, 'modified rows');
  await fs.utimes(file, before.atime, before.mtime);
  const files = reads(t, root);
  await copy.snapshot();
  assert.ok(files.includes(path.join('base', '1', 'rows')));
  await legacy(root).validate();
  assert.equal(await fs.readFile(path.join(root, 'update-snapshot/data/base/1/rows'), 'utf8'), 'modified rows');
});

test('damaged cached contents are recopied, and a damaged receipt falls back to a complete copy', async t => {
  const { root, copy } = await cluster(t);
  await copy.snapshot(); await copy.discard();
  await fs.writeFile(path.join(root, 'update-copy-cache/data/base/1/rows'), 'corrupt!');
  const files = reads(t, root);
  await copy.snapshot();
  assert.deepEqual(files, [path.join('base', '1', 'rows')]);
  await legacy(root).validate(); await copy.discard();
  await fs.writeFile(path.join(root, 'update-copy-cache/incremental.json'), '{');
  files.length = 0;
  await copy.snapshot(); assert.equal(files.length, 4); await legacy(root).validate();
});

test('removals and file/directory type changes are reflected exactly', async t => {
  const { root, copy } = await cluster(t);
  await copy.snapshot(); await copy.discard();
  await fs.rm(path.join(root, 'data/base/1/rows'));
  await fs.mkdir(path.join(root, 'data/base/1/rows'));
  await fs.writeFile(path.join(root, 'data/base/1/rows/child'), 'child');
  await fs.rm(path.join(root, 'data/empty'), { recursive: true });
  await fs.writeFile(path.join(root, 'data/empty'), 'now a file');
  await copy.snapshot(); await legacy(root).validate();
  assert.equal(await fs.readFile(path.join(root, 'update-snapshot/data/empty'), 'utf8'), 'now a file');
});

test('live pid, links and an existing point fail closed', async t => {
  const { root, copy } = await cluster(t);
  await fs.writeFile(path.join(root, 'data/postmaster.pid'), '123');
  await assert.rejects(copy.snapshot(), /stopped/);
  await fs.rm(path.join(root, 'data/postmaster.pid'));
  await fs.symlink(path.join(root, 'data/base'), path.join(root, 'data/external'), process.platform === 'win32' ? 'junction' : 'dir');
  await assert.rejects(copy.snapshot(), /symbolic links/);
  await fs.unlink(path.join(root, 'data/external'));
  await copy.snapshot();
  await assert.rejects(copy.snapshot(), { code: 'EEXIST' });
});

test('an interrupted copy cannot be retained or selected, and retry makes a complete point', async t => {
  const { root, copy } = await cluster(t);
  await copy.snapshot(); await copy.discard();
  await fs.writeFile(path.join(root, 'data/base/1/rows'), 'changed');
  const open = fs.open;
  t.mock.method(fs, 'open', async (file, ...args) => {
    if (String(file).endsWith(path.join('data/base/1/rows')) && args[0] === 'r+') throw Error('disk full');
    return open(file, ...args);
  });
  await assert.rejects(copy.snapshot(), /disk full/);
  await assert.rejects(legacy(root).validate());
  await copy.discard();
  t.mock.restoreAll();
  await copy.snapshot(); await legacy(root).validate();
});


for (const boundary of ['update-copy-cache', 'update-snapshot']) test(`cache rename interrupted after ${boundary} is disposable and retryable`, async t => {
  const { root, copy } = await cluster(t);
  await copy.snapshot(); await copy.discard();
  if (boundary === 'update-snapshot') await copy.snapshot();
  const storage = require('../dist/main/database-restore.js'), rename = storage.durableRename;
  t.mock.method(storage, 'durableRename', async (from, to) => {
    await rename(from, to); // perform the real OS write-through rename, then interrupt the caller
    if (from === path.join(root, boundary)) throw Error('synthetic power loss');
  });
  try {
    await assert.rejects(boundary === 'update-copy-cache' ? copy.snapshot() : copy.discard(), /synthetic power loss/);
  } finally { t.mock.restoreAll(); }
  await copy.discard(); // preparing/committed cleanup is restartable
  await copy.snapshot(); await legacy(root).validate();
});

test('a linked cache root is discarded without following it', async t => {
  const { root, copy } = await cluster(t), outside = path.join(root, 'synthetic-outside');
  await fs.mkdir(outside); await fs.writeFile(path.join(outside, 'sentinel'), 'do not touch');
  await fs.symlink(outside, path.join(root, 'update-copy-cache'), process.platform === 'win32' ? 'junction' : 'dir');
  await copy.snapshot(); await legacy(root).validate();
  assert.equal(await fs.readFile(path.join(outside, 'sentinel'), 'utf8'), 'do not touch');
});

test('app producer, installed launcher preflight and failed-start recovery preserve the journal and floor', async t => {
  const { root, copy } = await cluster(t), { f, u } = await rollbackFixture(t);
  await copy.snapshot(); await copy.discard(); // seed the cache through the real producer
  await u.prepareRollback(async () => copy);
  const launcher = f.create(); await launcher.load();
  await launcher.prepareRollback(async () => legacy(root));
  assert.equal((await launcher.select()).revision, B);
  await launcher.requireRestorePoint(legacy(root));
  await launcher.databaseStarting();
  await fs.writeFile(path.join(root, 'data/base/1/rows'), 'failed migration');
  await launcher.failed();
  const restarted = f.create(); await restarted.load();
  assert.equal(await restarted.recoverRollback(async () => legacy(root)), true);
  assert.equal(await fs.readFile(path.join(root, 'data/base/1/rows'), 'utf8'), 'original rows');
  assert.equal(restarted.hasMigrationRisk, false);
});


test('healthy completion retains the producer cache for the next real update journal', async t => {
  const { root, copy } = await cluster(t), { f, u } = await rollbackFixture(t);
  await u.prepareRollback(async () => copy);
  await u.select(); await u.databaseStarting(); await u.healthy();
  await u.finishRollback(async () => copy);
  await assert.rejects(fs.stat(path.join(f.root, 'restore-point.json')), { code: 'ENOENT' });
  assert.ok(await fs.stat(path.join(root, 'update-copy-cache/incremental.json')));
  await f.release(C); await u.download();
  const files = reads(t, root);
  await u.prepareRollback(async () => copy);
  assert.deepEqual(files, []);
  const launcher = f.create(); await launcher.load();
  await launcher.prepareRollback(async () => legacy(root));
  assert.equal((await launcher.select()).revision, C);
});

test('a source that changes during the copy never completes a point', async t => {
  const { root, copy } = await cluster(t), original = nativeFs.createReadStream;
  t.mock.method(nativeFs, 'createReadStream', (file, ...args) => {
    const stream = original(file, ...args);
    if (String(file) === path.join(root, 'data/base/1/rows')) stream.on('end', () => nativeFs.writeFileSync(file, 'racing write'));
    return stream;
  });
  await assert.rejects(copy.snapshot(), /Database changed/);
  await assert.rejects(fs.access(path.join(root, 'update-snapshot/incremental.json')), { code: 'ENOENT' });
  await copy.discard();
  await assert.rejects(fs.stat(path.join(root, 'update-copy-cache')), { code: 'ENOENT' });
});

test('launcher full validation rejects same-size corruption even when a cache receipt claimed a hit', async t => {
  const { root, copy } = await cluster(t), { f, u } = await rollbackFixture(t);
  await copy.snapshot(); await copy.discard();
  await u.prepareRollback(async () => copy);
  await fs.writeFile(path.join(root, 'update-snapshot/data/base/1/rows'), 'corruptedrows');
  const launcher = f.create(); await launcher.load();
  await assert.rejects(launcher.prepareRollback(async () => legacy(root)), /validation/);
  assert.equal(JSON.parse(await fs.readFile(path.join(f.root, 'state.json'), 'utf8')).booting, undefined);
});
