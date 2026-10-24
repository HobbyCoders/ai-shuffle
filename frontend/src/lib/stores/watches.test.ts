/** The shared watches store (#371 step 3): polling while tracked, and optimistic status changes. */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { get } from 'svelte/store';

const api = vi.hoisted(() => ({ listWatches: vi.fn(), setWatchStatus: vi.fn() }));
vi.mock('$lib/api/watches', () => api);

import { watches, __resetWatches, WATCH_POLL_MS } from './watches';

const row = (id: string, status = 'active') => ({ id, kind: 'watch', what: id, status, last_fired_at: null, cost_usd: 0, can_pause: status === 'active', can_stop: status !== 'stopped' });

beforeEach(() => {
	vi.useFakeTimers();
	__resetWatches();
	api.listWatches.mockReset().mockResolvedValue([row('a')]);
	api.setWatchStatus.mockReset().mockResolvedValue(row('a', 'paused'));
});
afterEach(() => vi.useRealTimers());

describe('watches store', () => {
	it('fetches when first tracked, polls while tracked, and stops when released', async () => {
		const releaseA = watches.track();
		const releaseB = watches.track();
		await vi.advanceTimersByTimeAsync(0);
		expect(api.listWatches).toHaveBeenCalledTimes(1);
		expect(get(watches).list.map((w) => w.id)).toEqual(['a']);
		await vi.advanceTimersByTimeAsync(WATCH_POLL_MS);
		expect(api.listWatches).toHaveBeenCalledTimes(2);
		releaseA();
		releaseA();
		await vi.advanceTimersByTimeAsync(WATCH_POLL_MS);
		expect(api.listWatches).toHaveBeenCalledTimes(3);
		releaseB();
		await vi.advanceTimersByTimeAsync(WATCH_POLL_MS * 2);
		expect(api.listWatches).toHaveBeenCalledTimes(3);
	});

	it('keeps the last list and flags an error when a refresh fails', async () => {
		await watches.refresh();
		api.listWatches.mockRejectedValueOnce(new Error('offline'));
		await watches.refresh();
		expect(get(watches)).toMatchObject({ error: true, loaded: true });
		expect(get(watches).list).toHaveLength(1);
	});

	it('changes a status at once and refetches; a failure puts it back', async () => {
		await watches.refresh();
		const pending = watches.setStatus('a', 'paused');
		expect(get(watches).list[0]).toMatchObject({ status: 'paused', can_pause: false });
		await pending;
		expect(api.setWatchStatus).toHaveBeenCalledWith('a', 'paused');
		await vi.advanceTimersByTimeAsync(0);
		expect(api.listWatches).toHaveBeenCalledTimes(2);

		api.setWatchStatus.mockRejectedValueOnce(new Error('nope'));
		api.listWatches.mockResolvedValue([row('a', 'paused')]);
		await vi.advanceTimersByTimeAsync(0);
		await expect(watches.setStatus('a', 'stopped')).rejects.toThrow('nope');
		expect(get(watches).list[0].status).toBe('paused');
	});
});
