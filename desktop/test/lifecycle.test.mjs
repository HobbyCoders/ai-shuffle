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

test('the source lease comes from the owned worker only, and a worker without the endpoint means no lease', async t => {
    const lease = 'e'.repeat(4096);
    const port = await server(t, (req, res) => {
        if (req.url === '/api/v1/internal/desktop/source-lease' && req.method === 'GET') res.end(JSON.stringify({lease}));
        else { res.statusCode = 404; res.end('{}'); }
    });
    // A full-size lease fits the response limit that guards the other lifecycle calls.
    assert.deepEqual(await requestLifecycle(port, 'test-secret', 'source-lease'), {lease});
    const worker = supervisor();
    worker._state = {...worker.state, phase: 'ready', port};
    worker.lifecycleToken = 'test-secret';
    assert.equal(await worker.sourceLease(), lease);
    // An older worker answers 404: the feature is absent and the desktop sends no header.
    worker.lifecycleRequest = () => requestLifecycle(port, 'test-secret', 'update-policy');
    assert.equal(await worker.sourceLease(), null);
    for (const reply of [{lease: null}, {lease: ''}, {lease: 7}, {}]) {
        worker.lifecycleRequest = async () => reply;
        assert.equal(await worker.sourceLease(), null, JSON.stringify(reply));
    }
    worker.lifecycleRequest = () => { throw Error('must not ask a worker that is not ours or not ready'); };
    worker._state = {...worker.state, external: true};
    assert.equal(await worker.sourceLease(), null);
    worker._state = {...worker.state, external: false, phase: 'starting'};
    assert.equal(await worker.sourceLease(), null);
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

test('NC-11: a forced stop does not ask a wedged worker to stop sessions; it ends the process', { skip: process.platform === 'win32' }, async (t) => {
    const { spawn } = await import('node:child_process');
    const child = spawn(process.execPath, ['-e', 'setInterval(() => {}, 1000)'], { detached: true, stdio: 'ignore' });
    t.after(() => { try { process.kill(-child.pid, 'SIGKILL'); } catch { /* gone */ } });
    await new Promise(resolve => child.once('spawn', resolve));
    const worker = supervisor();
    worker._state = {...worker.state, phase: 'ready', pid: child.pid};
    worker.child = child;
    worker.lifecycleRequest = () => { throw Error('a forced stop must not wait on stop-sessions'); };
    await worker.stop({force: true});
    assert.notEqual(child.exitCode ?? child.signalCode, null, 'the worker process is gone');
    // The graceful path still asks first.
    const graceful = supervisor();
    graceful._state = {...graceful.state, phase: 'ready'};
    graceful.child = {exitCode: null};
    let asked = 0;
    graceful.lifecycleRequest = async (action) => { asked++; assert.equal(action, 'stop-sessions'); return {stopped: false}; };
    await assert.rejects(graceful.stop(), /did not finish/);
    assert.equal(asked, 1);
});
