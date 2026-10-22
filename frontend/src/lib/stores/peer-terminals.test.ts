import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const api = vi.hoisted(() => ({ requestPeer: vi.fn(), peerAuthorization: vi.fn(), revokePeerAuthorization: vi.fn(), peerRequests: vi.fn(), approvePeerRequest: vi.fn(), revokePeerRequest: vi.fn() }));
vi.mock('$lib/api/terminals', async (importOriginal) => {
	const actual = await importOriginal<typeof import('$lib/api/terminals')>();
	return { ...actual, terminalsApi: api };
});

import { APPROVED_POLL_MS, peerTerminals, SOURCE_POLL_MS } from './peer-terminals.svelte';

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

describe('source status polling', () => {
	it('keeps at most one status call in flight per worker when the peer hangs', async () => {
		api.requestPeer.mockResolvedValue(pending);
		api.peerAuthorization.mockReturnValue(new Promise(() => {}));
		await peerTerminals.request('b', 'B');
		await vi.advanceTimersByTimeAsync(SOURCE_POLL_MS * 10);
		expect(api.peerAuthorization).toHaveBeenCalledTimes(1);
	});

	it('checks an approved, unopened lease less often', async () => {
		api.requestPeer.mockResolvedValue({ ...pending, status: 'approved', expires_in: 600 });
		api.peerAuthorization.mockResolvedValue({ ...pending, status: 'approved', expires_in: 590 });
		await peerTerminals.request('b', 'B');
		await vi.advanceTimersByTimeAsync(APPROVED_POLL_MS * 3);
		expect(api.peerAuthorization.mock.calls.length).toBeLessThanOrEqual(4);
		expect(api.peerAuthorization.mock.calls.length).toBeGreaterThanOrEqual(2);
		expect(peerTerminals.flow('b')?.phase).toBe('approved');
	});

	it('never moves an approved request back to pending on a late reply', async () => {
		api.requestPeer.mockResolvedValue(pending);
		await peerTerminals.request('b', 'B');
		api.peerAuthorization.mockResolvedValueOnce({ ...pending, status: 'approved', expires_in: 600 });
		await peerTerminals.poll('b');
		api.peerAuthorization.mockResolvedValueOnce({ ...pending, status: 'pending' });
		await peerTerminals.poll('b');
		expect(peerTerminals.flow('b')?.phase).toBe('approved');
	});

	it('revokes a known authorization id even without a flow', async () => {
		api.revokePeerAuthorization.mockResolvedValue({ revoked: true });
		await peerTerminals.cancel('b', LEASE);
		expect(api.revokePeerAuthorization).toHaveBeenCalledTimes(1);
		expect(peerTerminals.isRevoked(LEASE)).toBe(true);
	});
});

describe('recipient list', () => {
	it('shows a slow list instead of overtaking every answer with the next tick', async () => {
		const request = { request_id: 'r5', worker_id: 'a', status: 'pending', used: false, expires_in: 100, confirmation_code: 'ABCDEF', source_name: 'A', age_seconds: 1 };
		// Each answer takes 5 s, longer than the 3 s poll interval.
		api.peerRequests.mockImplementation(() => new Promise((resolve) => setTimeout(() => resolve({ requests: [request] }), 5_000)));
		const stop = peerTerminals.watch();
		await vi.advanceTimersByTimeAsync(4_000);
		expect(api.peerRequests).toHaveBeenCalledTimes(1);
		await vi.advanceTimersByTimeAsync(1_500);
		expect(peerTerminals.requests.map((r) => r.request_id)).toEqual(['r5']);
		await vi.advanceTimersByTimeAsync(20_000);
		expect(api.peerRequests.mock.calls.length).toBeLessThanOrEqual(5);
		expect(peerTerminals.requests.map((r) => r.request_id)).toEqual(['r5']);
		stop();
	});

	it('ignores a list answer fetched before a decision', async () => {
		const request = { request_id: 'r9', worker_id: 'a', status: 'pending', used: false, expires_in: 100, confirmation_code: 'ABCDEF', source_name: 'A', age_seconds: 1 };
		api.peerRequests.mockResolvedValueOnce({ requests: [request] });
		await peerTerminals.refreshRequests();
		let stale!: (value: unknown) => void;
		api.peerRequests.mockReturnValueOnce(new Promise((r) => (stale = r)));
		const late = peerTerminals.refreshRequests();
		api.approvePeerRequest.mockResolvedValue({ ...request, status: 'approved', expires_in: 600 });
		api.peerRequests.mockReturnValueOnce(new Promise(() => {}));
		await peerTerminals.approve('r9');
		expect(peerTerminals.requests[0].status).toBe('approved');
		stale({ requests: [request] });
		await late;
		expect(peerTerminals.requests[0].status).toBe('approved');
	});

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
