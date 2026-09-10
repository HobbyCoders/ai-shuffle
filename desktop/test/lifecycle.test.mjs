import assert from 'node:assert/strict';
import { test } from 'node:test';
import http from 'node:http';
import { createRequire } from 'node:module';
const require = createRequire(import.meta.url);
const { requestLifecycle, WorkerSupervisor } = require('../dist/main/worker.js');

async function server(t, handler) {
    const s = http.createServer(handler);
    await new Promise(resolve => s.listen(0, '127.0.0.1', resolve));
    t.after(() => new Promise(resolve => s.close(resolve)));
    return s.address().port;
}

test('lifecycle requests use the child secret and GET/POST on loopback', async t => {
    const seen = [];
    const port = await server(t, (req, res) => {
        seen.push([req.method, req.url, req.headers.authorization]);
        res.end(JSON.stringify(req.method === 'GET' ? {active_sessions: 2} : {stopped: true}));
    });
    assert.deepEqual(await requestLifecycle(port, 'test-secret', 'active-sessions'), {active_sessions: 2});
    assert.deepEqual(await requestLifecycle(port, 'test-secret', 'stop-sessions'), {stopped: true});
    assert.deepEqual(seen, [
        ['GET', '/api/v1/internal/desktop/active-sessions', 'Bearer test-secret'],
        ['POST', '/api/v1/internal/desktop/stop-sessions', 'Bearer test-secret']
    ]);
});

for (const [name, status, body] of [['denied', 403, '{}'], ['malformed', 200, 'not JSON']]) {
    test(`lifecycle rejects ${name} responses`, async t => {
        const port = await server(t, (_req, res) => { res.statusCode = status; res.end(body); });
        await assert.rejects(requestLifecycle(port, 'test-secret', 'stop-sessions'));
    });
}

function supervisor() {
    return new WorkerSupervisor({runtime: {}, logDir: '.', dataDir: '.', envFile: '', postgres: () => null});
}

test('attached workers are neither counted nor sent a shutdown request', async () => {
    const worker = supervisor();
    worker._state = {...worker.state, phase: 'ready', external: true};
    worker.lifecycleRequest = () => { throw Error('must not call an attached worker'); };
    assert.equal(await worker.activeSessionCount(), 0);
    await worker.stop();
});

test('missing stop acknowledgment preserves the owned child for retry', async () => {
    const worker = supervisor();
    worker._state = {...worker.state, phase: 'ready'};
    worker.child = {exitCode: null};
    worker.lifecycleRequest = async () => ({stopped: false});
    await assert.rejects(worker.stop(), /did not finish/);
    assert.equal(worker.state.phase, 'ready');
    assert.equal(worker.stopping, false);
});
