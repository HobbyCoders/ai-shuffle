import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const api = vi.hoisted(() => ({ requestPeer: vi.fn(), peerAuthorization: vi.fn(), revokePeerAuthorization: vi.fn(), peerRequests: vi.fn() }));
vi.mock('$lib/api/terminals', async (importOriginal) => {
	const actual = await importOriginal<typeof import('$lib/api/terminals')>();
	return { ...actual, terminalsApi: api };
});

import { peerTerminals, SOURCE_POLL_MS } from './peer-terminals.svelte';

const LEASE = 'lease_'.padEnd(28, 'x');
const pending = { authorization_id: LEASE, worker_id: 'b', request_id: 'r', status: 'pending', used: false, expires_in: 120, confirmation_code: 'ABCDEF', source_name: 'A', age_seconds: 0 };

beforeEach(() => { vi.useFakeTimers(); peerTerminals.reset(); Object.values(api).forEach((fn) => fn.mockReset()); });
afterEach(() => { peerTerminals.reset(); vi.useRealTimers(); });

describe('source request flow', () => {
	it('polls while pending and ends the request when the recipient stops answering past expiry', async () => {
		api.requestPeer.mockResolvedValue(pending);
		api.peerAuthorization.mockRejectedValue(new Error('down'));
		await peerTerminals.request('b', 'B');
		expect(peerTerminals.flow('b')?.phase).toBe('pending');
		await vi.advanceTimersByTimeAsync(SOURCE_POLL_MS);
		expect(api.peerAuthorization).toHaveBeenCalledWith(LEASE);
		await vi.advanceTimersByTimeAsync(130_000);
		const flow = peerTerminals.flow('b');
		expect(flow?.phase).toBe('ended');
		expect(flow?.phase === 'ended' && flow.message).toContain("didn't approve in time");
		const calls = api.peerAuthorization.mock.calls.length;
		await vi.advanceTimersByTimeAsync(10_000);
		expect(api.peerAuthorization.mock.calls.length).toBe(calls);
	});

	it('withdraws a request that was cancelled while it was in flight', async () => {
		let resolve!: (value: typeof pending) => void;
		api.requestPeer.mockReturnValue(new Promise((r) => (resolve = r)));
		api.revokePeerAuthorization.mockResolvedValue({ revoked: true });
		const run = peerTerminals.request('b', 'B');
		await peerTerminals.cancel('b');
		resolve(pending);
		await run;
		expect(peerTerminals.flow('b')).toBeNull();
		expect(api.revokePeerAuthorization).toHaveBeenCalledWith(LEASE);
	});

	it('does not send a second request while one is pending', async () => {
		api.requestPeer.mockResolvedValue(pending);
		await peerTerminals.request('b', 'B');
		await peerTerminals.request('b', 'B');
		expect(api.requestPeer).toHaveBeenCalledTimes(1);
	});

	it('forgets local authority even when the recipient cannot be told', async () => {
		api.requestPeer.mockResolvedValue(pending);
		api.revokePeerAuthorization.mockRejectedValue(new Error('down'));
		await peerTerminals.request('b', 'B');
		await peerTerminals.cancel('b');
		expect(peerTerminals.flow('b')).toBeNull();
	});
});

describe('recipient list', () => {
	it('drops requests locally once they expire', async () => {
		api.peerRequests.mockResolvedValue({ requests: [{ ...pending, worker_id: 'a', expires_in: 5 }] });
		const stop = peerTerminals.watch();
		await vi.advanceTimersByTimeAsync(0);
		expect(peerTerminals.requests).toHaveLength(1);
		api.peerRequests.mockRejectedValue(new Error('down'));
		await vi.advanceTimersByTimeAsync(6_000);
		expect(peerTerminals.requests).toHaveLength(0);
		stop();
	});
});
