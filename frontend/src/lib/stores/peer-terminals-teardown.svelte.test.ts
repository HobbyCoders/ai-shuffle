/**
 * The link countdown when the view watching the device list goes away in the
 * same update that shows the link card. That is the path a real press of Open
 * and paste takes, and Svelte reads a just-added key as undefined there.
 */
import { flushSync } from 'svelte';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';

const api = vi.hoisted(() => ({ peerChallenge: vi.fn(), requestPeerLink: vi.fn(), deviceLinks: vi.fn() }));
vi.mock('$lib/api/terminals', async (importOriginal) => {
	const actual = await importOriginal<typeof import('$lib/api/terminals')>();
	return { ...actual, terminalsApi: api };
});
vi.mock('$lib/terminal/device-key', () => ({
	deviceLabel: () => 'Chrome on Linux',
	deviceKey: async () => ({ keyId: 'key-1', publicKey: 'public-1', code: 'ABCD2345EFGH', sign: async () => 'signed' }),
}));

import { LINK_POLL_MS, peerTerminals } from './peer-terminals.svelte';

beforeEach(() => {
	vi.useFakeTimers();
	peerTerminals.reset();
	api.peerChallenge.mockResolvedValue({ status: 'pending', receiver: 'dev_b', via: 'dev_a', expires_in: 500 });
	api.deviceLinks.mockResolvedValue({ requests: [], links: [] });
});
afterEach(() => { peerTerminals.reset(); vi.useRealTimers(); });

it('keeps the link countdown running when the last watcher ends as the card appears', async () => {
	const stop = $effect.root(() => {
		$effect(() => {
			if (peerTerminals.flow('dev_b')) return;
			return peerTerminals.watch();
		});
	});
	flushSync();

	const run = peerTerminals.authorize('dev_b', 'B', 'terminal.open', { session_id: 'chat-b', cwd: null, shell: null });
	await vi.advanceTimersByTimeAsync(0);
	flushSync();
	expect(peerTerminals.flow('dev_b')?.phase).toBe('linking');

	const before = peerTerminals.now;
	await vi.advanceTimersByTimeAsync(1_000);
	expect(peerTerminals.now).toBeGreaterThan(before);

	peerTerminals.cancel('dev_b');
	await vi.advanceTimersByTimeAsync(LINK_POLL_MS);
	await run;
	stop();
});
