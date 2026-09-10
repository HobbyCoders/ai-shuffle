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
