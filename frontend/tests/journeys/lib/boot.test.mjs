import assert from 'node:assert/strict';
import { it } from 'node:test';
import { runInNewContext } from 'node:vm';
import { waitForBootReconciled, waitForCreatedChat } from './boot.mjs';

function fixture({ reconciled = false, initEnded = false, view = { type: 'session', sessionId: null }, session = 'created', timeout = 2000 } = {}) {
	const globals = {
		localStorage: { getItem: () => JSON.stringify({ activeView: view }) },
		sessionStorage: { getItem: () => JSON.stringify({ sessionId: session }) },
		performance: {
			getEntriesByName: name => (name === 'shuffle:boot.reconciled' ? reconciled : initEnded) ? [{ name, startTime: 100 }] : [],
			getEntriesByType: () => []
		}
	};
	const evaluate = fn => runInNewContext(`(${fn})()`, globals);
	return {
		evaluate,
		async waitForFunction(fn, arg, options) {
			assert.equal(options.timeout, timeout, 'use the caller budget; keep the created-chat view deadline at 2 s');
			if (!evaluate(fn)) throw new Error(`Timeout ${timeout}ms exceeded`);
		}
	};
}

it('waits for reconciliation, not fetch/init completion, within the explicit boot-hold budget', async () => {
	const budget = { timeout: 20_000 };
	await assert.rejects(waitForBootReconciled(fixture({ initEnded: true, ...budget }), budget), /boot reconciliation: .*shuffle:boot.init.end.*"bootstrap":\[\]/);
	await waitForBootReconciled(fixture({ reconciled: true, ...budget }), budget);
});
it('waits for the same created chat, not any persisted string', async () => {
	await waitForCreatedChat(fixture({ view: { type: 'session', sessionId: 'created' } }));
	await assert.rejects(waitForCreatedChat(fixture({ view: { type: 'session', sessionId: 'old' } })), /"sessionId":"old"/);
});
it('prints the unfollowed view and bootstrap state on timeout', async () => {
	await assert.rejects(waitForCreatedChat(fixture()), /layout follows the created chat: .*"sessionId":null.*Timeout 2000ms/);
});
