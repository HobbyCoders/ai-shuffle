import assert from 'node:assert/strict';
import { EventEmitter } from 'node:events';
import test from 'node:test';
import { waitForInstaller } from '../scripts/install-preview.mjs';

test('installer success waits for exit, not inherited pipe closure', async () => {
  const child = new EventEmitter();
  const done = waitForInstaller(child, { timeoutMs: 1000 });
  child.emit('exit', 0, null);
  await done;
  assert.equal(child.listenerCount('exit'), 0);
});

test('installer nonzero exits and signals never pass', async () => {
  for (const [code, signal] of [[2, null], [null, 'SIGTERM'], [0, 'SIGTERM']]) {
    const child = new EventEmitter();
    const done = waitForInstaller(child, { timeoutMs: 1000 });
    child.emit('exit', code, signal);
    await assert.rejects(done, /NSIS installer exited/);
  }
});

test('installer spawn errors are propagated', async () => {
  const child = new EventEmitter();
  const done = waitForInstaller(child, { timeoutMs: 1000 });
  child.emit('error', new Error('spawn ENOENT'));
  await assert.rejects(done, /ENOENT/);
});

test('timeout captures diagnostics before cleanup, never converts cleanup exit to success', async () => {
  const child = new EventEmitter();
  child.pid = 123;
  const calls = [];
  const done = waitForInstaller(child, {
    timeoutMs: 15, intervalMs: 1000,
    progress: async pid => { assert.equal(pid, 123); calls.push('diagnostics'); },
    terminate: async () => { calls.push('terminate'); child.emit('exit', 0, null); },
  });
  await assert.rejects(done, /timed out/);
  assert.deepEqual(calls, ['diagnostics', 'terminate']);
});

test('diagnostic or cleanup errors cannot hide installer failure', async () => {
  const child = new EventEmitter();
  await assert.rejects(waitForInstaller(child, {
    timeoutMs: 15, intervalMs: 1000,
    progress: async () => { throw new Error('diagnostics unavailable'); },
    terminate: async () => { throw new Error('taskkill failed'); },
  }), /timed out.*cleanup failed/);
});
