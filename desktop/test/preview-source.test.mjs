import assert from 'node:assert/strict';
import { mkdtemp, readFile, rm, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { createRequire } from 'node:module';
import { test } from 'node:test';
import { previewOptions, reservePreviewRoot, stopPreview } from '../scripts/preview-source.mjs';
const require = createRequire(import.meta.url);
const { buildWorkerEnv } = require('../dist/main/worker.js');
const repo = path.join(tmpdir(), 'checkout');

for (const workerFails of [false, true]) {
  test(`preview shutdown handles void stop and cleans up (workerFails=${workerFails})`, async () => {
    const events = [];
    const worker = {
      async stop() { events.push('worker'); if (workerFails) throw new Error('worker stop failed'); },
      async clearConnectionFile() { events.push('clear'); },
    };
    const postgres = { state: { phase: 'ready' }, async stop() { events.push('postgres'); this.state.phase = 'stopped'; } };
    if (workerFails) await assert.rejects(stopPreview(worker, postgres), /worker stop failed/);
    else await stopPreview(worker, postgres);
    assert.deepEqual(events, ['worker', 'postgres', 'clear']);
  });
}

test('preview clears its descriptor even when PostgreSQL fails to stop', async () => {
  let cleared = false;
  const worker = { async stop() {}, async clearConnectionFile() { cleared = true; } };
  const postgres = { state: { phase: 'failed' }, async stop() {} };
  await assert.rejects(stopPreview(worker, postgres), /PostgreSQL did not stop/);
  assert.equal(cleared, true);
});

const base = { SHUFFLE_PREVIEW_ROOT: path.join(tmpdir(), 'isolated-preview'), SHUFFLE_PREVIEW_PYTHON: path.join(tmpdir(), 'python.exe') };

test('source preview uses its checkout, fresh profile and nonattaching loopback supervisor', () => {
  const options = previewOptions({ ...base, SHUFFLE_PREVIEW_PORT: '18100', SHUFFLE_PREVIEW_PROTECTED_PORT: '18101',
    DATA_DIR: '/live', INSTANCE_ID: 'live', AI_SHUFFLE_ENV_FILE: '/live/.env', ANTHROPIC_API_KEY: 'fixture-secret', FAL_KEY: 'fixture-secret', NODE_TLS_REJECT_UNAUTHORIZED: '0', PGHOST: 'live', HOME: '/live' }, repo);
  assert.equal(options.runtime.serverDir, repo);
  assert.equal(options.allowAttach, false);
  const env = buildWorkerEnv(options);
  assert.equal(env.HOST, '127.0.0.1');
  assert.equal(env.PORT, '18100');
  assert.equal(env.SHUFFLE_DESKTOP_PROTECTED_PORT, '18101');
  assert.equal(env.DATA_DIR, path.join(base.SHUFFLE_PREVIEW_ROOT, 'data'));
  assert.equal(env.AI_SHUFFLE_ENV_FILE, path.join(base.SHUFFLE_PREVIEW_ROOT, 'worker', '.env'));
  assert.equal(env.HOME, path.join(base.SHUFFLE_PREVIEW_ROOT, 'profile'));
  assert.equal(env.INSTANCE_ID, undefined);
  assert.equal(env.ANTHROPIC_API_KEY, undefined);
  assert.equal(env.FAL_KEY, undefined);
  assert.equal(env.NODE_TLS_REJECT_UNAUTHORIZED, undefined);
  assert.equal(env.PGHOST, undefined);
  assert.equal(env.PYTHONPATH, path.join(repo, 'fluid'));
  assert.equal(env.AI_SHUFFLE_PREVIEW, '1');
  assert.equal(env.SHUFFLE_LOCAL_ACCESS, 'true');
  const defaults = previewOptions(base, repo);
  assert.equal(defaults.port, 18000); assert.equal(defaults.protectedPort, 18001);
});

test('preview rejects missing, relative, shared or invalid settings', () => {
  for (const override of [{ SHUFFLE_PREVIEW_ROOT: '' }, { SHUFFLE_PREVIEW_ROOT: 'relative' }, { SHUFFLE_PREVIEW_ROOT: repo },
    { SHUFFLE_PREVIEW_ROOT: path.join(repo, 'data') }, { SHUFFLE_PREVIEW_PYTHON: '' }, { SHUFFLE_PREVIEW_PORT: '0' },
    { SHUFFLE_PREVIEW_PROTECTED_PORT: '65536' }, { SHUFFLE_PREVIEW_PORT: '18001' }]) {
    assert.throws(() => previewOptions({ ...base, ...override }, repo));
  }
});

test('reserving a profile refuses an existing directory without changing it', async () => {
  const parent = await mkdtemp(path.join(tmpdir(), 'shuffle-source-preview-'));
  const root = path.join(parent, 'fresh');
  try {
    await reservePreviewRoot(root);
    await writeFile(path.join(root, 'sentinel'), 'preserve');
    await assert.rejects(reservePreviewRoot(root), { code: 'EEXIST' });
    assert.equal(await readFile(path.join(root, 'sentinel'), 'utf8'), 'preserve');
  } finally {
    await rm(parent, { recursive: true, force: true });
  }
});
