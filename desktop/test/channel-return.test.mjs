import assert from 'node:assert/strict';
import { generateKeyPairSync, sign } from 'node:crypto';
import { promises as fs } from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { createRequire } from 'node:module';
import { test } from 'node:test';
const require = createRequire(import.meta.url);
const { SourceUpdates, verifySourceManifest } = require('../dist/main/source-updates.js');
const { returnToStable, stableReturnRefusal, installedRelease } = require('../dist/main/channel-return.js');

const A = 'a'.repeat(40), B = 'b'.repeat(40), DIGEST = 'd'.repeat(64);
const keys = generateKeyPairSync('ed25519');
const publicKey = keys.publicKey.export({ type: 'spki', format: 'pem' });
function signed(fields, privateKey = keys.privateKey) {
  const payload = Buffer.from(JSON.stringify({ format: 2, sequence: Date.now(), sha256: DIGEST, size: 10, tree: DIGEST, channel: 'stable', ...fields }));
  return JSON.stringify({ payload: payload.toString('base64'), signature: sign(null, payload, privateKey).toString('base64') });
}
async function root(t, channel = 'nightly') {
  const dir = await fs.mkdtemp(path.join(os.tmpdir(), 'shuffle-channel-return-'));
  t.after(() => fs.rm(dir, { recursive: true, force: true, maxRetries: 10, retryDelay: 100 }));
  if (channel) await fs.writeFile(path.join(dir, 'update-channel.json'), JSON.stringify({ channel }));
  return dir;
}
const saved = async dir => JSON.parse(await fs.readFile(path.join(dir, 'update-channel.json'), 'utf8')).channel;
/** A source updater running revision A from the signed release `installed`. */
function updates(dir, installed, overrides = {}) {
  return { options: { root: dir, publicKey }, version: A, pendingVersion: null, hasMigrationRisk: false,
    channel: async () => JSON.parse(await fs.readFile(path.join(dir, 'update-channel.json'), 'utf8')).channel,
    release: async revision => { assert.equal(revision, A); return verifySourceManifest(installed, publicKey); }, ...overrides };
}

test('a nightly install that is not ahead of stable goes back to stable', async t => {
  const dir = await root(t);
  await returnToStable(updates(dir, signed({ revision: A, channel: 'nightly', version: '1.0.2-dev.3+gaaaaaaa', container: { sequence: 900 } })),
    signed({ revision: B, version: '1.0.2', container: { sequence: 901 } }), null);
  assert.equal(await saved(dir), 'stable');
});

test('the stable revision itself on the nightly channel goes back to stable', async t => {
  const dir = await root(t);
  await returnToStable(updates(dir, signed({ revision: A, channel: 'nightly', version: '1.0.2', container: { sequence: 950 } })),
    signed({ revision: A, version: '1.0.2', container: { sequence: 950 } }), null);
  assert.equal(await saved(dir), 'stable');
});

test('code ahead of stable stays on nightly', async t => {
  const dir = await root(t);
  for (const sequence of [950, 951]) {
    await assert.rejects(returnToStable(updates(dir, signed({ revision: A, channel: 'nightly', container: { sequence } })),
      signed({ revision: B, version: '1.0.2', container: { sequence: 950 } }), null), /newer than the current stable release \(1\.0\.2\)/);
  }
  assert.equal(await saved(dir), 'nightly');
});

test('the stable manifest must be signed by this install and be a stable release', async t => {
  const dir = await root(t);
  const installed = signed({ revision: A, channel: 'nightly', container: { sequence: 1 } });
  const other = generateKeyPairSync('ed25519').privateKey;
  await assert.rejects(returnToStable(updates(dir, installed), signed({ revision: B, container: { sequence: 2 } }, other), null), /signature/);
  await assert.rejects(returnToStable(updates(dir, installed), signed({ revision: B, channel: 'nightly', container: { sequence: 2 } }), null), /signed stable release/);
  assert.equal(await saved(dir), 'nightly');
});

test('an update in progress or a possibly migrated database keeps nightly', async t => {
  const dir = await root(t);
  const installed = signed({ revision: A, channel: 'nightly', container: { sequence: 1 } });
  const stable = signed({ revision: B, container: { sequence: 2 } });
  await assert.rejects(returnToStable(updates(dir, installed, { pendingVersion: B }), stable, null), /Finish the current update/);
  await assert.rejects(returnToStable(updates(dir, installed, { hasMigrationRisk: true }), stable, null), /Finish the current update/);
  await assert.rejects(returnToStable(updates(dir, installed, { options: { root: dir, publicKey, channel: 'nightly' } }), stable, null), /cannot be changed/);
  assert.equal(await saved(dir), 'nightly');
});

test("an installer's bundled build compares its final version with stable's", async t => {
  for (const [version, allowed] of [['1.0.1', true], ['1.0.2', true], ['1.0.3', false], ['1.0.2-dev.4', false], [null, false]]) {
    const dir = await root(t);
    const u = new SourceUpdates({ root: dir, publicKey, bootstrapNode: process.execPath, baseline: { revision: A, sequence: 50 } });
    assert.deepEqual(await installedRelease(u, version), { revision: A, container_sequence: null, version });
    const attempt = returnToStable(u, signed({ revision: B, version: '1.0.2', container: { sequence: 99 } }), version);
    if (allowed) await attempt; else await assert.rejects(attempt, /newer than|Cannot tell/);
    assert.equal(await saved(dir), allowed ? 'stable' : 'nightly', String(version));
  }
});

test('the refusal rule prefers main order and falls back to final versions only', () => {
  const stable = { revision: B, version: '1.0.2', container: { sequence: 10 } };
  assert.equal(stableReturnRefusal({ revision: A, container_sequence: 9, version: '9.9.9' }, stable), null);
  assert.match(stableReturnRefusal({ revision: A, container_sequence: 10, version: '1.0.0' }, stable), /newer than/);
  assert.equal(stableReturnRefusal({ revision: A, container_sequence: null, version: '1.0.2' }, stable), null);
  assert.match(stableReturnRefusal({ revision: A, container_sequence: 9, version: null }, { revision: B }), /Cannot tell/);
  assert.equal(stableReturnRefusal({ revision: B, container_sequence: null, version: null }, { revision: B }), null);
});
