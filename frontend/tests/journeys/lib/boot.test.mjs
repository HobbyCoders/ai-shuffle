import assert from 'node:assert/strict';
import { it } from 'node:test';
import { runInNewContext } from 'node:vm';
import { waitForBootReconciled, waitForCreatedChat } from './boot.mjs';

function fixture({ reconciled = false, initEnded = false, view = { type: 'session', sessionId: null }, session = 'created' } = {}) {
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
			assert.equal(options.timeout, 2000, 'do not widen the original post-load deadline');
			if (!evaluate(fn)) throw new Error('Timeout 2000ms exceeded');
		}
	};
}

it('does not call preferences/fetch completion boot reconciliation', async () => {
	await assert.rejects(waitForBootReconciled(fixture({ initEnded: true })), /boot reconciliation: .*shuffle:boot.init.end.*"bootstrap":\[\]/);
	await waitForBootReconciled(fixture({ reconciled: true }));
});
it('waits for the same created chat, not any persisted string', async () => {
	await waitForCreatedChat(fixture({ view: { type: 'session', sessionId: 'created' } }));
	await assert.rejects(waitForCreatedChat(fixture({ view: { type: 'session', sessionId: 'old' } })), /"sessionId":"old"/);
});
it('prints the unfollowed view and bootstrap state on timeout', async () => {
	await assert.rejects(waitForCreatedChat(fixture()), /layout follows the created chat: .*"sessionId":null.*Timeout 2000ms/);
});
