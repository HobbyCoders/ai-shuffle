import assert from 'node:assert/strict';
import { promises as fs } from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { test } from 'node:test';
import { createRequire } from 'node:module';
import { afterRename } from './helpers/crash-rename.mjs';
const { ColdClusterCopy, durableJson } = createRequire(import.meta.url)('../dist/main/database-restore.js');
async function fixture(t) {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), 'shuffle-restore-'));
  t.after(() => fs.rm(root, { recursive: true, force: true }));
  await fs.mkdir(path.join(root, 'data/global'), { recursive: true });
  await fs.mkdir(path.join(root, 'data/empty'));
  await fs.writeFile(path.join(root, 'data/PG_VERSION'), '16');
  await fs.writeFile(path.join(root, 'data/global/pg_control'), 'clean control');
  await fs.writeFile(path.join(root, 'data/rows'), 'previous schema and data');
  const calls = [];
  return { root, calls, copy: new ColdClusterCopy(root, async snapshot => { calls.push(snapshot); }) };
}
test('cold copy is validated, durable, private and restores files and empty directories', async t => {
  const { root, copy, calls } = await fixture(t);
  await copy.snapshot();
  await fs.writeFile(path.join(root, 'data/rows'), 'migrated data');
  await copy.restore();
  assert.equal(await fs.readFile(path.join(root, 'data/rows'), 'utf8'), 'previous schema and data');
  assert.ok((await fs.stat(path.join(root, 'data/empty'))).isDirectory());
  assert.deepEqual(calls, [true, false]);
  assert.ok(await fs.stat(path.join(root, 'update-snapshot/manifest.json')));
  await copy.discard();
  assert.deepEqual(await fs.readdir(root), ['data']);
});
test('missing/corrupt copy and a live pid never overwrite the database', async t => {
  const { root, copy } = await fixture(t);
  await fs.writeFile(path.join(root, 'data/postmaster.pid'), '123');
  await assert.rejects(copy.snapshot(), /stopped/);
  await copy.discard();
  await fs.rm(path.join(root, 'data/postmaster.pid'));
  await copy.snapshot();
  await fs.writeFile(path.join(root, 'update-snapshot/data/rows'), 'corrupt');
  await assert.rejects(copy.restore(), /validation/);
  assert.equal(await fs.readFile(path.join(root, 'data/rows'), 'utf8'), 'previous schema and data');
});
test('snapshot failure is closed, and only one snapshot can be retained', async t => {
  const { root, copy } = await fixture(t);
  const denied = new ColdClusterCopy(root, async () => { throw Error('worker alive'); });
  await assert.rejects(denied.snapshot(), /worker alive/);
  await assert.rejects(fs.stat(path.join(root, 'update-snapshot')), { code: 'ENOENT' });
  await copy.snapshot();
  await assert.rejects(copy.snapshot(), { code: 'EEXIST' });
});
for (const boundary of ['data', 'update-restore']) test(`restore resumes after power loss just after renaming ${boundary}`, async t => {
  const { root, copy } = await fixture(t);
  await copy.snapshot();
  await fs.writeFile(path.join(root, 'data/rows'), 'migrated');
  const undo = afterRename(async from => {
    if (from === path.join(root, boundary)) throw Error('power loss');
  });
  try { await assert.rejects(copy.restore(), /power loss/); } finally { undo(); }
  const restarted = new ColdClusterCopy(root, async () => {});
  await restarted.restore();
  assert.equal(await fs.readFile(path.join(root, 'data/rows'), 'utf8'), 'previous schema and data');
  await restarted.discard();
});
test('an interrupted restore copy keeps the immutable snapshot and retries', async t => {
  const { root, copy } = await fixture(t);
  await copy.snapshot();
  const copyFile = fs.copyFile;
  fs.copyFile = async (from, to) => { if (to.startsWith(path.join(root, 'update-restore'))) throw Error('disk full'); return copyFile(from, to); };
  try { await assert.rejects(copy.restore(), /disk full/); } finally { fs.copyFile = copyFile; }
  assert.ok(await fs.stat(path.join(root, 'update-snapshot/manifest.json')));
  await copy.restore();
});
test('journal replacement flushes its temporary file before publishing it', async t => {
  const { root } = await fixture(t);
  const file = path.join(root, 'journal.json');
  await durableJson(file, { phase: 'preparing' });
  const open = fs.open;
  fs.open = async (...args) => {
    const handle = await open(...args);
    if (args[0] === file + '.tmp') handle.sync = async () => { throw Error('power loss'); };
    return handle;
  };
  try { await assert.rejects(durableJson(file, { phase: 'ready' }), /power loss/); } finally { fs.open = open; }
  assert.deepEqual(JSON.parse(await fs.readFile(file)), { phase: 'preparing' });
  await durableJson(file, { phase: 'ready' });
  assert.deepEqual(JSON.parse(await fs.readFile(file)), { phase: 'ready' });
});
