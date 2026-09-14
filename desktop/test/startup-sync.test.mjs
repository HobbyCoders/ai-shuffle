import assert from 'node:assert/strict';
import { test } from 'node:test';
import http from 'node:http';
import { createRequire } from 'node:module';
const require = createRequire(import.meta.url);
const { WorkerSupervisor, syncBeforeOpen, ATTACH_ANY_ENV } = require('../dist/main/worker.js');

async function serve(t, handler) {
  const server = http.createServer(handler);
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  t.after(() => new Promise(resolve => server.close(resolve)));
  return server.address().port;
}

for (const external of [true, false]) {
  test(`${external ? 'attached' : 'owned'} worker stays on splash until sync responds`, {timeout: 10000}, async t => {
    let release, requested;
    const began = new Promise(resolve => { requested = resolve; });
    const port = await serve(t, (req, res) => {
      if (req.url === '/health') return res.end(JSON.stringify({service: 'ai-shuffle'}));
      assert.equal(req.url, '/api/v1/network/startup-sync');
      assert.equal(req.method, 'POST');
      release = () => res.end(JSON.stringify({status: 'synced'}));
      requested();
    });
    // This synthetic worker is explicitly attachable; a mere health response
    // must not bypass the supervisor's ownership guard.
    const worker = new WorkerSupervisor({runtime: {}, port, logDir: '.', dataDir: '.', envFile: '', baseEnv: {[ATTACH_ANY_ENV]: '1'}, postgres: () => null});
    if (!external) worker._state = {...worker.state, phase: 'starting'};
    const opening = external ? worker.start() : worker.finishStartup(port);
    await began;
    assert.equal(worker.state.phase, 'starting');
    assert.equal(worker.state.startupStep, 'syncing');
    release();
    await opening;
    assert.equal(worker.state.phase, 'ready');
    assert.equal(worker.state.syncStatus, 'synced');
  });
}

for (const outcome of ['offline', 'partial', 'not_enrolled']) {
  test(`accepts explicit ${outcome} fallback without claiming synced`, async t => {
    const port = await serve(t, (_req, res) => res.end(JSON.stringify({status: outcome})));
    assert.equal((await syncBeforeOpen(port)).status, outcome);
  });
}
for (const [status, body] of [[404, '{}'], [403, '{}'], [200, 'invalid'], [200, '{"status":"healthy"}']]) {
  test(`rejects invalid startup reply ${status} ${body}`, async t => {
    const port = await serve(t, (_req, res) => {res.statusCode = status; res.end(body);});
    await assert.rejects(syncBeforeOpen(port));
  });
}

test('stopping during sync never makes the worker ready', async t => {
  let release, requested;
  const began = new Promise(resolve => {requested = resolve;});
  const port = await serve(t, (_req, res) => {release = () => res.end('{"status":"synced"}'); requested();});
  const worker = new WorkerSupervisor({runtime: {}, port, logDir: '.', dataDir: '.', envFile: '', postgres: () => null});
  worker._state = {...worker.state, phase: 'starting', external: true};
  const opening = worker.finishStartup(port);
  await began;
  await worker.stop();
  release();
  await opening;
  assert.notEqual(worker.state.phase, 'ready');
});
