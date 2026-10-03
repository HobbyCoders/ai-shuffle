import { beforeEach, describe, expect, it, vi } from 'vitest';

const get = vi.fn();
const put = vi.fn();
vi.mock('$lib/api/client', () => ({ api: { get: (...a: unknown[]) => get(...a), put: (...a: unknown[]) => put(...a) } }));

import { awayMode } from './away-mode.svelte';

describe('away mode store', () => {
	beforeEach(() => {
		get.mockReset();
		put.mockReset();
	});

	it('loads the persisted flag once per session', async () => {
		get.mockResolvedValue({ away_mode: true });
		await awayMode.load('s-load');
		await awayMode.load('s-load');
		expect(get).toHaveBeenCalledTimes(1);
		expect(get).toHaveBeenCalledWith('/sessions/s-load');
		expect(awayMode.isAway('s-load')).toBe(true);
		expect(awayMode.isAway(null)).toBe(false);
	});

	it('flips the flag through the owning worker and reports released questions', async () => {
		put.mockResolvedValue({ away: true, released_questions: 2 });
		const result = await awayMode.set('s-set', true);
		expect(put).toHaveBeenCalledWith('/sessions/s-set/away-mode', { away: true });
		expect(result.released_questions).toBe(2);
		expect(awayMode.isAway('s-set')).toBe(true);
		expect(awayMode.busy['s-set']).toBe(false);
	});

	it('keeps the old value when the change fails', async () => {
		put.mockRejectedValue(new Error('unreachable'));
		await expect(awayMode.set('s-fail', true)).rejects.toThrow('unreachable');
		expect(awayMode.isAway('s-fail')).toBe(false);
		expect(awayMode.busy['s-fail']).toBe(false);
	});
});
