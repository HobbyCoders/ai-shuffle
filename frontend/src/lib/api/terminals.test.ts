/**
 * The terminal REST wrapper keeps the server's own reason for a refusal.
 * Peer-terminal routes answer with an object `detail` (503 `{code,message}`,
 * 409 availability `{available,reason_code,reason}`), which the panel must
 * show as written instead of a bare status line.
 */
import { afterEach, describe, expect, it, vi } from 'vitest';
import { terminalsApi, TerminalApiError } from './terminals';

function answer(status: number, body: unknown) {
	return vi.fn(async (_url: string, _init?: RequestInit) => new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } }));
}

async function refusal(run: () => Promise<unknown>): Promise<TerminalApiError> {
	try {
		await run();
	} catch (error) {
		if (error instanceof TerminalApiError) return error;
		throw error;
	}
	throw new Error('expected a refusal');
}

afterEach(() => vi.unstubAllGlobals());

describe('terminal API refusals', () => {
	it('keeps the code and message of an object detail (503 peer control route)', async () => {
		vi.stubGlobal('fetch', answer(503, { detail: { code: 'https_required', message: 'The peer needs an authenticated HTTPS route.' } }));
		const error = await refusal(() => terminalsApi.targets());
		expect(error.status).toBe(503);
		expect(error.code).toBe('https_required');
		expect(error.message).toBe('The peer needs an authenticated HTTPS route.');
	});

	it('keeps the reason of a 409 availability detail', async () => {
		vi.stubGlobal('fetch', answer(409, { detail: { available: false, reason_code: 'peer_offline', reason: 'The peer worker is offline.' } }));
		const error = await refusal(() => terminalsApi.targets());
		expect(error.code).toBe('peer_offline');
		expect(error.message).toBe('The peer worker is offline.');
	});

	it('still reads the existing string detail and top-level code', async () => {
		vi.stubGlobal('fetch', answer(400, { detail: "The folder /nope doesn't exist on this worker.", code: 'bad_folder' }));
		const error = await refusal(() => terminalsApi.create({ cwd: '/nope' }));
		expect(error.code).toBe('bad_folder');
		expect(error.message).toBe("The folder /nope doesn't exist on this worker.");
	});
});

describe('peer terminal routes', () => {
	it('uses the contract paths and bodies', async () => {
		const fetch = answer(200, { requests: [] });
		vi.stubGlobal('fetch', fetch);
		await terminalsApi.requestPeer('dev_b');
		await terminalsApi.peerAuthorization('auth id');
		await terminalsApi.revokePeerAuthorization('auth id');
		await terminalsApi.peerRequests();
		await terminalsApi.approvePeerRequest('req/1');
		await terminalsApi.revokePeerRequest('req/1');
		const calls = fetch.mock.calls.map(([url, init]) => [String(url).replace(/^.*\/terminals/, ''), (init as RequestInit | undefined)?.method ?? 'GET', (init as RequestInit | undefined)?.body ?? null]);
		expect(calls).toEqual([
			['/peer-authorizations', 'POST', JSON.stringify({ worker_id: 'dev_b' })],
			['/peer-authorizations/auth%20id', 'GET', null],
			['/peer-authorizations/auth%20id', 'DELETE', null],
			['/peer-requests', 'GET', null],
			['/peer-requests/req%2F1/approve', 'POST', null],
			['/peer-requests/req%2F1', 'DELETE', null],
		]);
	});
});
