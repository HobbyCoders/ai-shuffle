import assert from 'node:assert/strict';
import { cp, mkdir, mkdtemp, readFile, readdir, rm, writeFile } from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import { test } from 'node:test';
import { refreshStarter } from '../scripts/refresh-starter.mjs';

async function fixture(t, version = 3) {
  const temporary = await mkdtemp(path.join(os.tmpdir(), 'starter-refresh-'));
  t.after(() => rm(temporary, { recursive: true, force: true }));
  const desktop = path.join(temporary, 'desktop'), root = path.join(temporary, 'install');
  await mkdir(desktop); await mkdir(root);
  for (const name of ['src', 'scripts', 'build']) await mkdir(path.join(desktop, name));
  for (const name of ['package.json', 'package-lock.json', 'tsconfig.json', 'runtime-contract.json']) await writeFile(path.join(desktop, name), '{}');
  await writeFile(path.join(desktop, 'launcher.json'), JSON.stringify({ version, minimum: version }));
  // Use the shipped bootstrap and trust bundle, rather than a synthetic marker alone.
  for (const name of ['scripts/bootstrap.mjs', 'build/application-update-key.pem']) {
    await cp(new URL(`../${name}`, import.meta.url), path.join(desktop, name));
  }
  return { desktop, root, starter: path.join(root, 'starter') };
}

test('fresh starter includes the version marker; same or older versions never overwrite it', async t => {
  const { desktop, root, starter } = await fixture(t);
  assert.equal(await refreshStarter(desktop, root), true);
  assert.equal(JSON.parse(await readFile(path.join(starter, 'launcher.json'))).version, 3);
  await writeFile(path.join(starter, 'local-marker'), 'retained');
  assert.equal(await refreshStarter(desktop, root), false);
  assert.equal(await refreshStarter(starter, root), false);
  await writeFile(path.join(desktop, 'launcher.json'), '{"version":2,"minimum":0}');
  assert.equal(await refreshStarter(desktop, root), false);
  assert.equal(await readFile(path.join(starter, 'local-marker'), 'utf8'), 'retained');
});

for (const legacyVersion of [null, 2]) test(`refreshes older starter (${legacyVersion}) without touching install data`, async t => {
  const { desktop, root, starter } = await fixture(t);
  await mkdir(path.join(starter, 'scripts'), { recursive: true });
  await mkdir(path.join(starter, 'dist'));
  await writeFile(path.join(starter, 'scripts/bootstrap.mjs'), 'old verifier');
  if (legacyVersion) await writeFile(path.join(starter, 'launcher.json'), JSON.stringify({ version: legacyVersion, minimum: 0 }));
  for (const name of ['state.json', 'profile.json', 'node-path.txt']) await writeFile(path.join(root, name), name);
  for (const name of ['versions', 'data']) { await mkdir(path.join(root, name)); await writeFile(path.join(root, name, 'keep'), name); }
  assert.equal(await refreshStarter(desktop, root), true);
  for (const name of ['scripts/bootstrap.mjs', 'build/application-update-key.pem', 'launcher.json']) {
    assert.equal(await readFile(path.join(starter, name), 'utf8'), await readFile(path.join(desktop, name), 'utf8'));
  }
  assert.equal((await readdir(starter)).includes('dist'), false);
  const backup = (await readdir(root)).find(name => name.startsWith('starter-backup-'));
  assert.equal(await readFile(path.join(root, backup, 'scripts/bootstrap.mjs'), 'utf8'), 'old verifier');
  for (const name of ['state.json', 'profile.json', 'node-path.txt']) assert.equal(await readFile(path.join(root, name), 'utf8'), name);
  for (const name of ['versions', 'data']) assert.equal(await readFile(path.join(root, name, 'keep'), 'utf8'), name);
});

test('incomplete shipped starter leaves installed code intact', async t => {
  const { desktop, root, starter } = await fixture(t);
  await mkdir(path.join(starter, 'scripts'), { recursive: true });
  await writeFile(path.join(starter, 'scripts/bootstrap.mjs'), 'old verifier');
  await rm(path.join(desktop, 'runtime-contract.json'));
  await assert.rejects(refreshStarter(desktop, root), /ENOENT/);
  assert.equal(await readFile(path.join(starter, 'scripts/bootstrap.mjs'), 'utf8'), 'old verifier');
  assert.deepEqual((await readdir(root)).sort(), ['starter']);
});
