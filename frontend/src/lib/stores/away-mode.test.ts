import { beforeEach, describe, expect, it, vi } from 'vitest';

const get = vi.fn();
const put = vi.fn();
vi.mock('$lib/api/client', () => ({ api: { get: (...a: unknown[]) => get(...a), put: (...a: unknown[]) => put(...a) } }));

import { AWAY_CONFIRM_GRACE_MS, awayMode } from './away-mode.svelte';

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

	it('follows the session row when another device or worker changes it', () => {
		awayMode.observe('s-row', true);
		expect(awayMode.isAway('s-row')).toBe(true);
		awayMode.observe('s-row', false);
		expect(awayMode.isAway('s-row')).toBe(false);
		// A flag never set is off; a row without the field (older server) changes nothing.
		awayMode.observe('s-row', true);
		awayMode.observe('s-row', undefined);
		expect(awayMode.isAway('s-row')).toBe(true);
		awayMode.observe('s-row', null);
		expect(awayMode.isAway('s-row')).toBe(false);
	});

	it('does not refetch a session whose row already reported the flag', async () => {
		awayMode.observe('s-row-first', true);
		await awayMode.load('s-row-first');
		expect(get).not.toHaveBeenCalled();
		expect(awayMode.isAway('s-row-first')).toBe(true);
	});

	it('keeps a confirmed toggle while a remote mirror still reports the old value', async () => {
		vi.useFakeTimers();
		try {
			awayMode.observe('s-remote', false);
			put.mockResolvedValue({ away: true });
			await awayMode.set('s-remote', true);
			// The owning worker confirmed; this worker's mirror has not caught up yet.
			awayMode.observe('s-remote', false);
			expect(awayMode.isAway('s-remote')).toBe(true);
			// The mirror catches up; from then on the row is followed again.
			awayMode.observe('s-remote', true);
			awayMode.observe('s-remote', false);
			expect(awayMode.isAway('s-remote')).toBe(false);

			put.mockResolvedValue({ away: true });
			await awayMode.set('s-remote', true);
			vi.advanceTimersByTime(AWAY_CONFIRM_GRACE_MS + 1);
			// Past the grace period a disagreeing row wins (changed elsewhere).
			awayMode.observe('s-remote', false);
			expect(awayMode.isAway('s-remote')).toBe(false);
		} finally {
			vi.useRealTimers();
		}
	});

	it('settles on a disagreeing row when the grace ends, without waiting for another row', async () => {
		// The composer only calls observe() when the row's value changes. If the
		// row already disagreed during the grace (another device turned Away back
		// off before this worker's mirror ever showed it on), no later call comes.
		vi.useFakeTimers();
		try {
			awayMode.observe('s-mask', false);
			put.mockResolvedValue({ away: true });
			await awayMode.set('s-mask', true);
			awayMode.observe('s-mask', false);
			expect(awayMode.isAway('s-mask')).toBe(true);
			vi.advanceTimersByTime(AWAY_CONFIRM_GRACE_MS + 1);
			expect(awayMode.isAway('s-mask')).toBe(false);
		} finally {
			vi.useRealTimers();
		}
	});

	it('never lets a row that cannot report the flag undo a confirmed toggle', async () => {
		// An owner too old to publish the flag leaves this worker's row at null.
		vi.useFakeTimers();
		try {
			awayMode.observe('s-old-owner', null);
			put.mockResolvedValue({ away: true });
			await awayMode.set('s-old-owner', true);
			awayMode.observe('s-old-owner', null);
			vi.advanceTimersByTime(AWAY_CONFIRM_GRACE_MS + 1);
			awayMode.observe('s-old-owner', null);
			expect(awayMode.isAway('s-old-owner')).toBe(true);
			// A real value from the owner still wins.
			awayMode.observe('s-old-owner', false);
			expect(awayMode.isAway('s-old-owner')).toBe(false);
		} finally {
			vi.useRealTimers();
		}
	});

	it('keeps the old value when the change fails', async () => {
		put.mockRejectedValue(new Error('unreachable'));
		await expect(awayMode.set('s-fail', true)).rejects.toThrow('unreachable');
		expect(awayMode.isAway('s-fail')).toBe(false);
		expect(awayMode.busy['s-fail']).toBe(false);
	});
});
