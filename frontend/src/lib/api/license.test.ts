import { beforeEach, describe, expect, it, vi } from 'vitest';

const transport = vi.hoisted(() => ({ get: vi.fn(), put: vi.fn(), post: vi.fn(), delete: vi.fn(), patch: vi.fn() }));
vi.mock('./client', () => ({ api: transport }));

import * as license from './license';

beforeEach(() => vi.resetAllMocks());

describe('Cloud status client', () => {
	it('reads the account plan from GET /license', async () => {
		const status = { state: 'paid', source: 'account', plan: 'paid', seats: 100, seats_used: 2, remote_worker_limit: 100 };
		transport.get.mockResolvedValue(status);
		await expect(license.getLicense()).resolves.toEqual(status);
		expect(transport.get).toHaveBeenCalledExactlyOnceWith('/license');
	});

	it('has no key routes: the plan follows the signed-in account', () => {
		for (const name of ['activateLicense', 'releaseSeat', 'deactivateLicense', 'refreshLicense']) {
			expect(license).not.toHaveProperty(name);
		}
		expect(transport.post).not.toHaveBeenCalled();
	});

	it('knows the plan defaults: 2 free, 4 paid, unlimited from 100 seats', () => {
		expect(license.FREE_WORKERS).toBe(2);
		expect(license.PAID_WORKERS).toBe(4);
		expect(license.UNLIMITED_SEATS).toBe(100);
	});
});
