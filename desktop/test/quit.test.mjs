import assert from 'node:assert/strict';
import { test } from 'node:test';
import { createRequire } from 'node:module';
const require = createRequire(import.meta.url);
const { QuitGuard } = require('../dist/main/quit.js');

function setup(active = 2, answer = true) {
	const calls = [];
	const actions = {
		activeSessionCount: async () => active,
		confirm: async count => { calls.push(['confirm', count]); return answer; },
		stop: async () => { calls.push('stop'); },
		quit: () => { calls.push('quit'); },
		failed: async () => { calls.push('failed'); }
	};
	return { calls, actions, guard: new QuitGuard(actions) };
}

test('active close confirms then waits for stop before allowing quit', async () => {
	const { calls, guard } = setup();
	await guard.request();
	assert.deepEqual(calls, [['confirm', 2], 'stop', 'quit']);
	assert.equal(guard.canQuit, true);
});
test('cancel keeps the app and worker alive and allows another attempt', async () => {
	const { calls, guard } = setup(2, false);
	await guard.request(); await guard.request();
	assert.deepEqual(calls, [['confirm', 2], ['confirm', 2]]);
	assert.equal(guard.canQuit, false);
});
test('idle or attached worker closes without warning', async () => {
	const { calls, guard } = setup(0);
	await guard.request(); assert.deepEqual(calls, ['stop', 'quit']);
});
test('unknown activity does not silently close', async () => {
	const { calls, actions, guard } = setup();
	actions.activeSessionCount = async () => { throw Error('offline'); };
	await guard.request(); assert.deepEqual(calls, [['confirm', null], 'stop', 'quit']);
});
test('repeated close while stopping cannot bypass the guard', async () => {
	const { actions, calls, guard } = setup();
	let release;
	actions.stop = () => new Promise(resolve => { release = resolve; });
	const first = guard.request();
	await new Promise(resolve => setImmediate(resolve));
	await guard.request();
	assert.equal(guard.canQuit, false);
	assert.deepEqual(calls, [['confirm', 2]]);
	release(); await first;
	assert.deepEqual(calls, [['confirm', 2], 'quit']);
});
test('cleanup failure keeps the app open and can be retried', async () => {
	const { actions, calls, guard } = setup();
	actions.stop = async () => { throw Error('db unavailable'); };
	await guard.request();
	assert.equal(guard.canQuit, false);
	assert.deepEqual(calls, [['confirm', 2], 'failed']);
	actions.stop = async () => {};
	await guard.request(); assert.equal(guard.canQuit, true);
});

function forcing(active = 0) {
	const s = setup(active);
	s.actions.forceStop = async () => { s.calls.push('forceStop'); };
	return s;
}

test('NC-11: the tray Quit runs the graceful stop first and quits without killing when it works', async () => {
	const { calls, guard } = forcing();
	await guard.request({ force: true });
	assert.deepEqual(calls, ['stop', 'quit']);
	assert.equal(guard.canQuit, true);
});

test('NC-11: the tray Quit kills a worker whose graceful stop fails, without the "still stopping" dead end', async () => {
	const { actions, calls, guard } = forcing(2);
	actions.stop = async () => { calls.push('stop'); throw Error('Worker did not finish stopping sessions'); };
	await guard.request({ force: true });
	assert.deepEqual(calls, [['confirm', 2], 'stop', 'forceStop', 'quit'], 'active sessions are still confirmed first');
	assert.equal(guard.canQuit, true);
});

test('NC-11: the tray Quit kills a worker whose graceful stop hangs past the grace period', async () => {
	const s = setup(0);
	s.actions.forceStop = async () => { s.calls.push('forceStop'); };
	s.actions.stop = () => { s.calls.push('stop'); return new Promise(() => {}); };
	const guard = new QuitGuard(s.actions, 20);
	await guard.request({ force: true });
	assert.deepEqual(s.calls, ['stop', 'forceStop', 'quit']);
});

test('NC-11: a tray Quit during a pending close upgrades it instead of being ignored', async () => {
	const s = setup(0);
	s.actions.forceStop = async () => { s.calls.push('forceStop'); };
	s.actions.stop = () => { s.calls.push('stop'); return new Promise(() => {}); };
	const guard = new QuitGuard(s.actions, 20);
	const first = guard.request();
	await new Promise(resolve => setImmediate(resolve));
	await guard.request({ force: true });
	await first;
	assert.deepEqual(s.calls, ['stop', 'forceStop', 'quit']);
	assert.equal(guard.canQuit, true);
});

test('NC-11: closing the window never kills on the first failure; a repeated failure offers Quit Anyway', async () => {
	const { actions, calls, guard } = forcing(0);
	const offers = [];
	let answer = false;
	actions.stop = async () => { throw Error('hung'); };
	actions.failed = async (_error, offerForce) => { offers.push(offerForce); return offerForce && answer; };
	await guard.request();
	assert.deepEqual(offers, [false]); assert.equal(guard.canQuit, false);
	await guard.request();
	assert.deepEqual(offers, [false, true]); assert.equal(guard.canQuit, false, 'Keep Open still keeps it open');
	answer = true;
	await guard.request();
	assert.deepEqual(calls, ['forceStop', 'quit']);
	assert.equal(guard.canQuit, true);
});

test('NC-11: a successful stop resets the repeated-failure count', async () => {
	const { actions, guard } = forcing(0);
	const offers = [];
	let fail = true;
	actions.stop = async () => { if (fail) throw Error('hung'); };
	actions.quit = () => {};
	actions.failed = async (_error, offerForce) => { offers.push(offerForce); return false; };
	await guard.request();
	assert.deepEqual(offers, [false]);
	fail = false; await guard.request(); assert.equal(guard.canQuit, true);
});
