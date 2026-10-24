import assert from 'node:assert/strict';
import { promises as fs } from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { createRequire } from 'node:module';
import { test } from 'node:test';
const require = createRequire(import.meta.url);
const { SinglePassClusterCopy, checkRestorePointStructure, launcherValidatedRestorePoint, RESTORE_POINT_VALIDATING_LAUNCHERS } = require('../dist/main/update-restore-point.js');
const { ColdClusterCopy } = require('../dist/main/database-restore.js');

/** A stopped cluster's layout, with nested and empty folders and numeric names like PostgreSQL's. */
async function cluster(t) {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), 'shuffle-restore-point-'));
  t.after(() => fs.rm(root, { recursive: true, force: true, maxRetries: 10, retryDelay: 100 }));
  const files = { 'PG_VERSION': '17\n', 'global/pg_control': 'control', 'base/1/1259': 'x'.repeat(200_000), 'base/16384/2619': 'rows', 'pg_wal/000000010000000000000001': 'wal', 'postgresql.conf': 'port = 5432\n' };
  for (const [name, body] of Object.entries(files)) {
    await fs.mkdir(path.dirname(path.join(root, 'data', name)), { recursive: true });
    await fs.writeFile(path.join(root, 'data', name), body);
  }
  await fs.mkdir(path.join(root, 'data', 'pg_tblspc'), { recursive: true });
  return root;
}
const read = file => fs.readFile(file, 'utf8');

test('only a known installed Windows launcher handoff skips the second hash', () => {
  assert.deepEqual([...RESTORE_POINT_VALIDATING_LAUNCHERS], [4, 5, 6, 7, 8]);
  for (const version of [4, 5, 6, 7, 8]) assert.equal(launcherValidatedRestorePoint('win32', version, true, false), true);
  for (const version of [0, 1, 2, 3, 9, 999, NaN, Infinity, 4.5]) assert.equal(launcherValidatedRestorePoint('win32', version, true, false), false);
  for (const platform of ['linux', 'darwin']) assert.equal(launcherValidatedRestorePoint(platform, 4, true, false), false);
  assert.equal(launcherValidatedRestorePoint('win32', 4, false, false), false);
  assert.equal(launcherValidatedRestorePoint('win32', 4, true, true), false);
});

test('a launcher version bump requires explicitly reviewing the hash allowlist', async () => {
  const { version } = JSON.parse(await read(new URL('../launcher.json', import.meta.url)));
  assert.equal(version, 8, 'Re-audit RESTORE_POINT_VALIDATING_LAUNCHERS and the installed factory; do not trust a new launcher without proof it hashes ready points');
});

test('the installed launcher awaits prepareRollback before every select', async () => {
  const entry = await read(new URL('../src/main/installed-entry.ts', import.meta.url));
  const prepared = entry.indexOf('await updates.prepareRollback(database)');
  const selections = [...entry.matchAll(/await updates\.select\(/g)];
  assert.ok(prepared >= 0 && selections.length > 0);
  for (const selected of selections) assert.ok(prepared < selected.index);
});

test('structural validation accepts the real producer manifest without reading file contents', async t => {
  const root = await cluster(t);
  await new SinglePassClusterCopy(root, async () => {}).snapshot();
  await new ColdClusterCopy(root, async () => {}).validate(); // installed launcher's check
  const originalRead = fs.readFile;
  fs.readFile = async (file, ...args) => {
    assert.equal(path.basename(String(file)), 'manifest.json', 'only the manifest is read');
    return originalRead(file, ...args);
  };
  try { await checkRestorePointStructure(root); } finally { fs.readFile = originalRead; }
});

for (const change of ['missing', 'extra', 'type', 'link', 'manifest']) test(`structural validation refuses ${change} entries`, async t => {
  const root = await cluster(t);
  await new SinglePassClusterCopy(root, async () => {}).snapshot();
  const data = path.join(root, 'update-snapshot', 'data');
  const control = path.join(data, 'global', 'pg_control');
  if (change === 'missing') await fs.rm(control);
  if (change === 'extra') await fs.writeFile(path.join(data, 'extra'), 'unexpected');
  if (change === 'type') { await fs.rm(control); await fs.mkdir(control); }
  if (change === 'link') { await fs.rm(path.join(data, 'pg_tblspc'), { recursive: true, maxRetries: 10, retryDelay: 100 }); await fs.symlink(path.join(root, 'data'), path.join(data, 'pg_tblspc'), 'junction'); }
  if (change === 'manifest') await fs.writeFile(path.join(root, 'update-snapshot', 'manifest.json'), '[]');
  await assert.rejects(checkRestorePointStructure(root));
});

test('full validation still rejects changed contents on older or unknown launchers', async t => {
  const root = await cluster(t);
  const copy = new SinglePassClusterCopy(root, async () => {});
  await copy.snapshot();
  await fs.writeFile(path.join(root, 'update-snapshot', 'data', 'global', 'pg_control'), 'corrupt');
  for (const launcher of [0, 3]) {
    const validator = launcherValidatedRestorePoint('win32', launcher, true, false) ? { validate: () => checkRestorePointStructure(root) } : copy;
    await assert.rejects(validator.validate(), /validation/);
  }
});

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
