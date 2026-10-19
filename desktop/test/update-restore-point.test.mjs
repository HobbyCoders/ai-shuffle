import assert from 'node:assert/strict';
import { promises as fs } from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { createRequire } from 'node:module';
import { test } from 'node:test';
const require = createRequire(import.meta.url);
const { SinglePassClusterCopy } = require('../dist/main/update-restore-point.js');
const { ColdClusterCopy } = require('../dist/main/database-restore.js');

/** A stopped cluster's layout, with nested and empty folders and numeric names like PostgreSQL's. */
async function cluster(t) {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), 'shuffle-restore-point-'));
  t.after(() => fs.rm(root, { recursive: true, force: true }));
  const files = { 'PG_VERSION': '17\n', 'global/pg_control': 'control', 'base/1/1259': 'x'.repeat(200_000), 'base/16384/2619': 'rows', 'pg_wal/000000010000000000000001': 'wal', 'postgresql.conf': 'port = 5432\n' };
  for (const [name, body] of Object.entries(files)) {
    await fs.mkdir(path.dirname(path.join(root, 'data', name)), { recursive: true });
    await fs.writeFile(path.join(root, 'data', name), body);
  }
  await fs.mkdir(path.join(root, 'data', 'pg_tblspc'), { recursive: true });
  return root;
}
const read = file => fs.readFile(file, 'utf8');

test('the single-pass copy is the restore point the launcher makes and validates', async t => {
  const fast = await cluster(t), slow = await cluster(t);
  const stops = [];
  await new SinglePassClusterCopy(fast, async snapshot => stops.push(snapshot)).snapshot();
  await new ColdClusterCopy(slow, async () => {}).snapshot();
  assert.deepEqual(stops, [true], 'the cluster is stopped cleanly first');
  // Byte for byte the same manifest, so the launcher's validate() accepts it.
  assert.equal(await read(path.join(fast, 'update-snapshot', 'manifest.json')), await read(path.join(slow, 'update-snapshot', 'manifest.json')));
  await new ColdClusterCopy(fast, async () => {}).validate();
  assert.equal(await read(path.join(fast, 'update-snapshot', 'data', 'base', '1', '1259')), 'x'.repeat(200_000));
  // And the launcher's restore() and discard() work on it unchanged.
  await fs.writeFile(path.join(fast, 'data', 'base', '16384', '2619'), 'migrated');
  await new ColdClusterCopy(fast, async () => {}).restore();
  assert.equal(await read(path.join(fast, 'data', 'base', '16384', '2619')), 'rows');
  await new ColdClusterCopy(fast, async () => {}).discard();
  await assert.rejects(fs.stat(path.join(fast, 'update-snapshot')));
});

test('the single-pass copy refuses what the launcher refuses', async t => {
  const running = await cluster(t);
  await fs.writeFile(path.join(running, 'data', 'postmaster.pid'), '123');
  await assert.rejects(new SinglePassClusterCopy(running, async () => {}).snapshot(), /Not a stopped managed PostgreSQL cluster/);
  await assert.rejects(fs.stat(path.join(running, 'update-snapshot')), 'nothing was copied');
  const existing = await cluster(t);
  await fs.mkdir(path.join(existing, 'update-snapshot'));
  await assert.rejects(new SinglePassClusterCopy(existing, async () => {}).snapshot(), /EEXIST/);
  if (process.platform !== 'win32') {
    const linked = await cluster(t);
    await fs.symlink(os.tmpdir(), path.join(linked, 'data', 'pg_tblspc', '16999'));
    await assert.rejects(new SinglePassClusterCopy(linked, async () => {}).snapshot(), /symbolic links/);
  }
  const stopFails = await cluster(t);
  await assert.rejects(new SinglePassClusterCopy(stopFails, async () => { throw Error('PostgreSQL is not cleanly shut down'); }).snapshot(), /cleanly/);
});

test('a database that changes during the copy is refused', async t => {
  const root = await cluster(t);
  const copy = new SinglePassClusterCopy(root, async () => {});
  // Something writes to the cluster while it is being copied.
  const validate = copy.validate.bind(copy);
  copy.validate = async () => { await validate(); await fs.writeFile(path.join(root, 'data', 'base', '16384', '2619'), 'rows and more'); };
  await assert.rejects(copy.snapshot(), /Database changed while taking its restore point/);
});
