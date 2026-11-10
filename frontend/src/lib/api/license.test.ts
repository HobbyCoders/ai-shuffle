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

	it('knows the plan defaults: 3 free, 10 paid, Pro 10, Ultimate 30, unlimited from 100 seats', () => {
		expect(license.FREE_WORKERS).toBe(3);
		expect(license.PAID_WORKERS).toBe(10);
		expect(license.PRO_WORKERS).toBe(10);
		expect(license.ULTIMATE_WORKERS).toBe(30);
		expect(license.UNLIMITED_SEATS).toBe(100);
	});

	it.each([
		['paid', 'pro', 'Pro'],
		['paid', 'ultimate', 'Ultimate'],
		['paid', 'custom', null],
		['paid', undefined, null],
		['paid', null, null],
		['paid', 'enterprise', null],
		['free', 'free', null],
		['free', 'pro', null],
		['not_linked', undefined, null],
	])('names a %s account with tier %s as %s', (state, tier, name) => {
		expect(license.paidPlanName({ state, tier } as Parameters<typeof license.paidPlanName>[0])).toBe(name);
	});

	it('names no plan before the status loads', () => {
		expect(license.paidPlanName(null)).toBeNull();
	});
});
