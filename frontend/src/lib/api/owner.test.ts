/** fetchOwnerStatus tells an older worker (no owner passwords), one that did not answer and one that answered with an error apart. */
import { beforeEach, describe, expect, it, vi } from 'vitest';

const get = vi.hoisted(() => vi.fn());
vi.mock('./client', () => ({ api: { get } }));
import { fetchOwnerStatus, getOwnerStatus } from './owner';

const body = {
	password_set: true, setup_allowed: false, required: false, trusted_lan: false,
	login_available: true, desktop_handoff: false, min_length: 12
};

beforeEach(() => { get.mockReset(); });

describe('fetchOwnerStatus', () => {
	it('returns the status', async () => {
		get.mockResolvedValue(body);
		expect(await fetchOwnerStatus()).toEqual({ ok: true, status: body });
		expect(await getOwnerStatus()).toEqual(body);
	});

	it.each([404, 405])('treats HTTP %i as a worker without owner passwords', async (status) => {
		get.mockRejectedValue({ detail: 'Not Found', status });
		expect(await fetchOwnerStatus()).toEqual({ ok: false, reason: 'unsupported' });
	});

	it('treats a reply without the owner fields as a worker without owner passwords', async () => {
		get.mockResolvedValue({ status: 'ok' });
		expect(await fetchOwnerStatus()).toEqual({ ok: false, reason: 'unsupported' });
		expect(await getOwnerStatus()).toBeNull();
	});

	it('treats a network failure (no reply) as unreachable', async () => {
		get.mockRejectedValue(new TypeError('Failed to fetch'));
		expect(await fetchOwnerStatus()).toEqual({ ok: false, reason: 'unreachable' });
		expect(await getOwnerStatus()).toBeNull();
	});

	it.each([
		['a server error', 502],
		['a refusal', 403],
		['a rate limit', 429]
	])('reports %s as an error answer with its status', async (_label, status) => {
		get.mockRejectedValue({ detail: 'nope', status });
		expect(await fetchOwnerStatus()).toEqual({ ok: false, reason: 'error', code: status });
		expect(await getOwnerStatus()).toBeNull();
	});
});
