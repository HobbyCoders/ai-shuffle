import assert from 'node:assert/strict';
import { lstat, mkdir, mkdtemp, readFile, rm, symlink, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { createRequire } from 'node:module';
import { test } from 'node:test';
import { execFileSync, spawnSync } from 'node:child_process';
import { previewOptions, previewUrl, reservePreviewRoot, stopPreview } from '../scripts/preview-source.mjs';
const require = createRequire(import.meta.url);
const { buildWorkerEnv } = require('../dist/main/worker.js');
const repo = path.join(tmpdir(), 'checkout');
test('preview opens the frontend root, not the retired chat route', () => {
  for (const base of ['http://127.0.0.1:18100', 'http://127.0.0.1:18100/']) {
    assert.equal(previewUrl(base), 'http://127.0.0.1:18100/');
  }
});

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

test('preview Git overrides repository fsmonitor without changing it or leaving a daemon', async () => {
  const root = await mkdtemp(path.join(tmpdir(), 'shuffle-preview-git-'));
  const checkout = path.join(root, 'workspace');
  try {
    await mkdir(checkout);
    const options = previewOptions({ ...process.env, ...base, SHUFFLE_PREVIEW_ROOT: root }, repo);
    const env = buildWorkerEnv(options);
    await mkdir(env.HOME, { recursive: true });
    const git = (...args) => execFileSync('git', args, { cwd: checkout, env, windowsHide: true, encoding: 'utf8' }).trim();
    git('init', '-q');
    git('config', '--local', 'core.fsmonitor', 'true');
    assert.equal(git('config', '--get', 'core.fsmonitor'), 'false');
    assert.equal(git('config', '--local', '--get', 'core.fsmonitor'), 'true');
    git('status', '--porcelain');
    if (process.platform === 'win32') {
      const status = spawnSync('git', ['fsmonitor--daemon', 'status'], { cwd: checkout, env, windowsHide: true });
      assert.equal(status.status, 1, 'preview status must not start a detached fsmonitor');
    }
  } finally {
    await rm(root, { recursive: true, force: true });
  }
  await assert.rejects(lstat(root), { code: 'ENOENT' });
});

test('failed protection explains recovery, preserves its cause and removes only the empty root', async () => {
  const parent = await mkdtemp(path.join(tmpdir(), 'shuffle-source-preview-'));
  const root = path.join(parent, 'fresh');
  const cause = new Error('SetAccessControl: Attempted to perform an unauthorized operation.');
  try {
    await assert.rejects(reservePreviewRoot(root, async directory => {
      assert.equal(directory, root);
      assert.equal((await lstat(directory)).isDirectory(), true);
      throw cause;
    }), error => {
      assert.equal(error.cause, cause);
      assert.ok(error.message.includes(root));
      assert.match(error.message, /SHUFFLE_PREVIEW_ROOT.*new, nonexistent child/);
      assert.match(error.message, /aborted before writing credentials/);
      assert.match(error.message, /Do not relax credential permissions/);
      assert.match(error.message, /empty preview directory was removed/);
      assert.ok(error.message.includes(cause.message));
      if (process.platform === 'win32') assert.match(error.message, /LocalAppData\/Temp/);
      return true;
    });
    await assert.rejects(lstat(root), { code: 'ENOENT' });
    // A failed attempt must not block retrying the same unused name.
    await reservePreviewRoot(root);
  } finally {
    await rm(parent, { recursive: true, force: true });
  }
});

test('failed protection never recursively deletes files added to the new root', async () => {
  const parent = await mkdtemp(path.join(tmpdir(), 'shuffle-source-preview-'));
  const root = path.join(parent, 'fresh');
  try {
    await assert.rejects(reservePreviewRoot(root, async directory => {
      await writeFile(path.join(directory, 'sentinel'), 'preserve');
      throw new Error('protection denied');
    }), /preview directory remains; inspect it/);
    assert.equal(await readFile(path.join(root, 'sentinel'), 'utf8'), 'preserve');
  } finally {
    await rm(parent, { recursive: true, force: true });
  }
});

test('reservation refuses existing files and directory links without protecting their targets', async () => {
  const parent = await mkdtemp(path.join(tmpdir(), 'shuffle-source-preview-'));
  const target = path.join(parent, 'target');
  const link = path.join(parent, 'link');
  const file = path.join(parent, 'sentinel');
  try {
    await mkdir(target);
    await writeFile(file, 'preserve');
    await symlink(target, link, process.platform === 'win32' ? 'junction' : 'dir');
    for (const root of [file, link, path.join(parent, 'missing', 'child')]) {
      await assert.rejects(reservePreviewRoot(root, () => assert.fail('must not protect existing paths')), {
        code: root.endsWith('child') ? 'ENOENT' : 'EEXIST',
      });
    }
    assert.equal(await readFile(file, 'utf8'), 'preserve');
    assert.equal((await lstat(link)).isSymbolicLink(), true);
    assert.equal((await lstat(target)).isDirectory(), true);
  } finally {
    // Remove the junction itself, never recurse through its target on Windows.
    await rm(link, { force: true });
    await rm(parent, { recursive: true, force: true });
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
