import assert from 'node:assert/strict';
import { test } from 'node:test';
import http from 'node:http';
import { createRequire } from 'node:module';
const { requestLifecycle, followUpdateRequests, WorkerSupervisor } = createRequire(import.meta.url)('../dist/main/worker.js');
const settle = () => new Promise(setImmediate);

test('the hint transport uses both existing credentials and cancellation closes its HTTP request', async () => {
  let seen, received, closed;
  const arrived = new Promise(resolve => { received = resolve; });
  const disconnected = new Promise(resolve => { closed = resolve; });
  const server = http.createServer((req, res) => {
    seen = { method: req.method, url: req.url, headers: req.headers };
    req.on('data', () => {}); req.on('end', received); res.on('close', closed);
  });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  const abort = new AbortController();
  const pending = requestLifecycle(server.address().port, 'synthetic-control', 'update-wake', { generation: 0 }, 'a'.repeat(64), abort.signal);
  try {
    await arrived;
    assert.equal(seen.method, 'POST'); assert.equal(seen.url, '/api/v1/internal/desktop/update-wake');
    assert.equal(seen.headers.authorization, 'Bearer synthetic-control');
    assert.equal(seen.headers['x-shuffle-desktop-listener'], 'a'.repeat(64));
    abort.abort(); await assert.rejects(pending, { name: 'AbortError' });
    await disconnected;
  } finally { abort.abort(); await new Promise(resolve => server.close(resolve)); }
});

test('old workers stop the subscription after 404 instead of spinning', async () => {
  let calls = 0, wakes = 0;
  const stop = followUpdateRequests(async () => { calls++; throw Object.assign(Error('old worker'), { status: 404 }); }, () => { wakes++; });
  await settle(); await settle(); stop();
  assert.equal(calls, 1); assert.equal(wakes, 0);
});

test('stop clears a transport-error backoff without retrying or leaking its timer', async t => {
  let calls = 0, pending = null;
  t.mock.method(globalThis, 'setTimeout', callback => { pending = callback; return 42; });
  t.mock.method(globalThis, 'clearTimeout', timer => { assert.equal(timer, 42); pending = null; });
  const stop = followUpdateRequests(async () => { calls++; throw Error('offline'); }, () => assert.fail('no wake on failure'));
  await settle(); assert.equal(typeof pending, 'function');
  stop(); await settle(); assert.equal(pending, null); assert.equal(calls, 1);
});

test('stop cancels one held request and no late response can wake the updater', async () => {
  let calls = 0, wakes = 0, resolve, signal;
  const stop = followUpdateRequests(async (_generation, value) => { calls++; signal = value; return new Promise(done => { resolve = done; }); }, () => { wakes++; });
  assert.equal(calls, 1); stop(); assert.equal(signal.aborted, true);
  resolve({ generation: 1 }); await settle();
  assert.equal(calls, 1); assert.equal(wakes, 0);
});

test('owned ready-worker restarts and shutdown detach the single waiter; attached workers never subscribe', async () => {
  const worker = new WorkerSupervisor({ runtime: {}, logDir: '.', dataDir: '.', envFile: '', postgres: () => null });
  let calls = 0, aborted = 0;
  worker.lifecycleRequest = async (_action, _body, signal) => {
    calls++; return new Promise((_resolve, reject) => signal.addEventListener('abort', () => { aborted++; reject(Error('stopped')); }, { once: true }));
  };
  worker._state = { ...worker.state, phase: 'ready', external: false, pid: 123, startedAt: 'one' };
  const stop = worker.watchUpdateRequests(() => {});
  worker.emit('state', worker.state); assert.equal(calls, 1);
  worker._state = { ...worker.state, phase: 'stopping' }; worker.emit('state', worker.state);
  assert.equal(aborted, 1);
  worker._state = { ...worker.state, phase: 'ready', pid: 124, startedAt: 'two' }; worker.emit('state', worker.state);
  assert.equal(calls, 2); stop(); await settle();
  assert.equal(aborted, 2); assert.equal(worker.listenerCount('state'), 0);
  worker._state = { ...worker.state, phase: 'ready', external: true };
  worker.watchUpdateRequests(() => {})(); assert.equal(calls, 2);
});
