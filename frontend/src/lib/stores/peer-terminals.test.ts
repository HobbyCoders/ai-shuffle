/**
 * This device's side of a peer terminal (#674): a linked device signs one open;
 * an unlinked one shows its own code and waits; and the receiving worker's list
 * of waiting and linked devices.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const api = vi.hoisted(() => ({
	peerChallenge: vi.fn(), requestPeerLink: vi.fn(), deviceLinks: vi.fn(),
	approveDeviceLink: vi.fn(), declineDeviceLink: vi.fn(), revokeDeviceLink: vi.fn(),
}));
vi.mock('$lib/api/terminals', async (importOriginal) => {
	const actual = await importOriginal<typeof import('$lib/api/terminals')>();
	return { ...actual, terminalsApi: api };
});
const key = vi.hoisted(() => ({ fail: null as string | null, sign: vi.fn(async (text: string) => `signed:${text.length}`) }));
vi.mock('$lib/terminal/device-key', () => ({
	deviceLabel: () => 'Chrome on Linux',
	deviceKey: async () => {
		if (key.fail) throw new Error(key.fail);
		return { keyId: 'key-1', publicKey: 'public-1', code: 'ABCD2345EFGH', sign: key.sign };
	},
}));

import { LINK_POLL_MS, peerTerminals } from './peer-terminals.svelte';
import { TerminalApiError } from '$lib/api/terminals';
import { ACTION_OPEN_AND_PASTE, PROOF_PURPOSE } from '$lib/terminal/peer';

const target = { session_id: 'chat-b', cwd: null, shell: null };
const linked = { status: 'linked', receiver: 'dev_b', via: 'dev_a', challenge: 'c1', expires_in: 60 };
const unlinked = { status: 'unlinked', receiver: 'dev_b', via: 'dev_a' };
const pending = { status: 'pending', receiver: 'dev_b', via: 'dev_a', expires_in: 500 };

beforeEach(() => {
	vi.useFakeTimers();
	peerTerminals.reset();
	Object.values(api).forEach((fn) => fn.mockReset());
	key.fail = null;
	key.sign.mockClear();
});
afterEach(() => { peerTerminals.reset(); vi.useRealTimers(); });

describe('signing an open', () => {
	it('signs exactly what it sends, bound to the worker, relay, challenge, action and target', async () => {
		api.peerChallenge.mockResolvedValue(linked);
		const proof = await peerTerminals.authorize('dev_b', 'B', ACTION_OPEN_AND_PASTE, target);
		expect(api.peerChallenge).toHaveBeenCalledWith('dev_b', 'key-1');
		expect(proof?.key_id).toBe('key-1');
		expect(JSON.parse(proof!.payload)).toEqual({ purpose: PROOF_PURPOSE, receiver: 'dev_b', via: 'dev_a', challenge: 'c1', action: ACTION_OPEN_AND_PASTE, target });
		expect(key.sign).toHaveBeenCalledWith(proof!.payload);
		expect(api.requestPeerLink).not.toHaveBeenCalled();
	});

	it('shows the code worked out here, waits for the owner, then signs without another click', async () => {
		api.peerChallenge.mockResolvedValueOnce(unlinked).mockResolvedValueOnce(pending).mockResolvedValueOnce(linked);
		api.requestPeerLink.mockResolvedValue({ status: 'pending', key_id: 'key-1', expires_in: 600 });
		const run = peerTerminals.authorize('dev_b', 'B', 'terminal.open', target);
		await vi.advanceTimersByTimeAsync(0);
		expect(api.requestPeerLink).toHaveBeenCalledWith('dev_b', 'public-1', 'Chrome on Linux');
		const flow = peerTerminals.flow('dev_b');
		expect(flow?.phase === 'linking' && flow.code).toBe('ABCD2345EFGH');
		await vi.advanceTimersByTimeAsync(LINK_POLL_MS * 2);
		expect((await run)?.key_id).toBe('key-1');
		expect(peerTerminals.flow('dev_b')).toBeNull();
	});

	it('never sends the code to either worker', async () => {
		api.peerChallenge.mockResolvedValueOnce(unlinked).mockResolvedValue(linked);
		api.requestPeerLink.mockResolvedValue({ status: 'pending', key_id: 'key-1' });
		const run = peerTerminals.authorize('dev_b', 'B', 'terminal.open', target);
		await vi.advanceTimersByTimeAsync(LINK_POLL_MS);
		await run;
		expect(JSON.stringify([...api.peerChallenge.mock.calls, ...api.requestPeerLink.mock.calls])).not.toContain('ABCD2345EFGH');
	});

	it('ends with a reason when the request runs out or is declined', async () => {
		api.peerChallenge.mockResolvedValueOnce(unlinked).mockResolvedValue(unlinked);
		api.requestPeerLink.mockResolvedValue({ status: 'pending', key_id: 'key-1' });
		const run = peerTerminals.authorize('dev_b', 'B', 'terminal.open', target);
		await vi.advanceTimersByTimeAsync(LINK_POLL_MS);
		expect(await run).toBeNull();
		const flow = peerTerminals.flow('dev_b');
		expect(flow?.phase === 'ended' && flow.retry).toBe(true);
	});

	it('produces nothing after Stop waiting, even when the link completes', async () => {
		api.peerChallenge.mockResolvedValueOnce(unlinked).mockResolvedValueOnce(pending).mockResolvedValue(linked);
		api.requestPeerLink.mockResolvedValue({ status: 'pending', key_id: 'key-1' });
		const run = peerTerminals.authorize('dev_b', 'B', 'terminal.open', target);
		await vi.advanceTimersByTimeAsync(0);
		peerTerminals.cancel('dev_b');
		await vi.advanceTimersByTimeAsync(LINK_POLL_MS);
		expect(await run).toBeNull();
		expect(key.sign).not.toHaveBeenCalled();
		expect(peerTerminals.flow('dev_b')).toBeNull();
	});

	it('keeps waiting through a short outage while the link card is up', async () => {
		api.peerChallenge.mockResolvedValueOnce(unlinked).mockRejectedValueOnce(new TerminalApiError('down', 503, 'peer_unavailable')).mockResolvedValue(linked);
		api.requestPeerLink.mockResolvedValue({ status: 'pending', key_id: 'key-1', expires_in: 600 });
		const run = peerTerminals.authorize('dev_b', 'B', 'terminal.open', target);
		await vi.advanceTimersByTimeAsync(0);
		const flow = peerTerminals.flow('dev_b');
		expect(flow?.phase === 'linking' && flow.unreachable).toBe(true);
		await vi.advanceTimersByTimeAsync(LINK_POLL_MS);
		expect((await run)?.key_id).toBe('key-1');
	});

	it('stops with a final reason where this browser cannot hold a key', async () => {
		key.fail = 'Needs a secure connection.';
		expect(await peerTerminals.authorize('dev_b', 'B', 'terminal.open', target)).toBeNull();
		const flow = peerTerminals.flow('dev_b');
		expect(flow?.phase === 'ended' && [flow.message, flow.retry]).toEqual(['Needs a secure connection.', false]);
		expect(api.peerChallenge).not.toHaveBeenCalled();
	});
});

describe('the receiving worker', () => {
	const request = { request_id: 'r1', label: 'Chrome on Linux', worker_id: 'dev_a', worker_name: 'A', age_seconds: 5, expires_in: 100 };
	const device = { key_id: 'key-1', label: 'Chrome on Linux', worker_id: 'dev_a', worker_name: 'A', linked_by: 'owner', created_at: '2026-11-08T00:00:00Z', last_used_at: null };

	it('links by the typed code and refreshes the list', async () => {
		api.approveDeviceLink.mockResolvedValue(device);
		api.deviceLinks.mockResolvedValue({ links: [device], requests: [] });
		expect(await peerTerminals.approve('ABCD 2345 EFGH')).toBeNull();
		expect(api.approveDeviceLink).toHaveBeenCalledWith('ABCD 2345 EFGH');
		expect(peerTerminals.links).toEqual([device]);
	});

	it('says plainly when no device is waiting with that code', async () => {
		api.approveDeviceLink.mockRejectedValue(new TerminalApiError('nope', 404, 'not_found'));
		expect(await peerTerminals.approve('WRONG')).toContain('No device is waiting with that code');
	});

	it('removes a revoked device straight away and keeps a failure on its row', async () => {
		api.deviceLinks.mockResolvedValue({ links: [device], requests: [request] });
		await peerTerminals.refreshDevices();
		api.revokeDeviceLink.mockResolvedValueOnce({ revoked: true });
		api.deviceLinks.mockResolvedValue({ links: [], requests: [request] });
		await peerTerminals.revoke('key-1');
		expect(peerTerminals.links).toEqual([]);
		api.declineDeviceLink.mockRejectedValue(new TerminalApiError('broken', 500, 'server_error'));
		await peerTerminals.decline('r1');
		expect(peerTerminals.notice('r1')).toBe('broken');
	});

	it('drops a request once it has run out, and shows nothing to a non-owner', async () => {
		api.deviceLinks.mockResolvedValue({ links: [], requests: [request] });
		const stop = peerTerminals.watch();
		await vi.advanceTimersByTimeAsync(0);
		expect(peerTerminals.requests).toHaveLength(1);
		api.deviceLinks.mockRejectedValue(new TerminalApiError('no', 403, 'http_403'));
		await peerTerminals.refreshDevices();
		expect(peerTerminals.requests).toEqual([]);
		stop();
	});
});
