/** The shared watches store (#371 step 3): polling while tracked, and optimistic status changes. */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { get } from 'svelte/store';

const api = vi.hoisted(() => ({ listWatches: vi.fn(), setWatchStatus: vi.fn(), moveWatch: vi.fn(), setWatchPreference: vi.fn() }));
vi.mock('$lib/api/watches', () => api);

import { watches, __resetWatches, WATCH_POLL_MS, MOVED_NOTE_MS, moveFailure } from './watches';

const row = (id: string, status = 'active', over: Record<string, unknown> = {}) => ({ id, kind: 'watch', what: id, status, last_fired_at: null, cost_usd: 0, can_pause: status === 'active', can_stop: status !== 'stopped', ...over });
const listing = (watches: unknown[], preferredWorkerId: string | null = null) => ({ watches, preferredWorkerId });

beforeEach(() => {
	vi.useFakeTimers();
	__resetWatches();
	api.listWatches.mockReset().mockResolvedValue(listing([row('a')]));
	api.setWatchStatus.mockReset().mockResolvedValue(row('a', 'paused'));
	api.moveWatch.mockReset();
	api.setWatchPreference.mockReset();
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
		api.listWatches.mockResolvedValue(listing([row('a', 'paused')]));
		await vi.advanceTimersByTimeAsync(0);
		await expect(watches.setStatus('a', 'stopped')).rejects.toThrow('nope');
		expect(get(watches).list[0].status).toBe('paused');
	});

	it('reads where new watches run from the listing and saves a new choice', async () => {
		api.listWatches.mockResolvedValue(listing([row('a')], 'dev_server'));
		await watches.refresh();
		expect(get(watches).preferredWorkerId).toBe('dev_server');
		api.setWatchPreference.mockResolvedValueOnce(null);
		await watches.setPreference(null);
		expect(api.setWatchPreference).toHaveBeenCalledWith(null);
		expect(get(watches).preferredWorkerId).toBeNull();
		api.setWatchPreference.mockRejectedValueOnce({ status: 409, detail: 'Preferred watch worker is unavailable' });
		await expect(watches.setPreference('dev_off')).rejects.toMatchObject({ status: 409 });
		expect(get(watches).preferredWorkerId).toBeNull();
	});

	it('a confirmed move reads Moved briefly, then the row speaks for itself', async () => {
		await watches.refresh();
		let answer!: (v: unknown) => void;
		api.moveWatch.mockReturnValueOnce(new Promise((r) => (answer = r)));
		const pending = watches.move('a', 'dev_server');
		expect(get(watches).moves.a).toEqual({ target: 'dev_server', phase: 'pending', message: null, confirming: false });
		// A second click while it runs does not send another move.
		expect(await watches.move('a', 'dev_server')).toBe('pending');
		expect(api.moveWatch).toHaveBeenCalledTimes(1);
		answer(row('a', 'active', { worker_id: 'ServerWorker' }));
		expect(await pending).toBe('moved');
		expect(api.moveWatch).toHaveBeenCalledWith('a', 'dev_server');
		expect(get(watches).moves.a.phase).toBe('moved');
		expect(api.listWatches).toHaveBeenCalledTimes(2);
		await vi.advanceTimersByTimeAsync(MOVED_NOTE_MS);
		expect(get(watches).moves.a).toBeUndefined();
	});

	it('a refused move keeps the reason; an unanswered one is uncertain, never moved', async () => {
		await watches.refresh();
		api.moveWatch.mockRejectedValueOnce({ status: 409, detail: 'Telegram watches cannot move: receive journals are worker-local' });
		expect(await watches.move('a', 'dev_server')).toBe('failed');
		expect(get(watches).moves.a).toEqual({
			target: 'dev_server',
			phase: 'failed',
			message: 'Telegram watches cannot move: receive journals are worker-local',
			confirming: false
		});
		for (const error of [{ status: 503, detail: 'Watch worker is unavailable; retry the same operation' }, new TypeError('Failed to fetch'), { status: 500, detail: 'Internal Server Error' }]) {
			api.moveWatch.mockRejectedValueOnce(error);
			expect(await watches.move('a', 'dev_server')).toBe('uncertain');
			expect(get(watches).moves.a.phase).toBe('uncertain');
		}
		// The list is refetched after each, since only it says whether the source is fenced.
		expect(api.listWatches).toHaveBeenCalledTimes(5);
		watches.dismissMove('a');
		expect(get(watches).moves.a).toBeUndefined();
	});

	it('an unconfirmed move listed on its target is re-sent once; only the answer reads Moved', async () => {
		await watches.refresh();
		// Import committed on the target, but the answer was lost.
		api.moveWatch.mockRejectedValueOnce({ status: 503, detail: 'retry the same operation' });
		await watches.move('a', 'dev_server');
		expect(get(watches).moves.a).toMatchObject({ phase: 'uncertain', confirming: false });
		// The listing now shows it on the target: the same PATCH goes again so the
		// server closes the old worker's receipt. Until it answers, nothing says Moved.
		let answer!: (v: unknown) => void;
		api.moveWatch.mockReturnValueOnce(new Promise((r) => (answer = r)));
		const confirming = watches.reconcileMove('a');
		expect(get(watches).moves.a).toMatchObject({ phase: 'pending', confirming: true });
		expect(api.moveWatch).toHaveBeenCalledTimes(2);
		expect(api.moveWatch).toHaveBeenLastCalledWith('a', 'dev_server');
		answer(row('a', 'active', { placement_device_id: 'dev_server' }));
		expect(await confirming).toBe('moved');
		expect(get(watches).moves.a.phase).toBe('moved');
		await vi.advanceTimersByTimeAsync(MOVED_NOTE_MS);
		expect(get(watches).moves.a).toBeUndefined();
	});

	it('a failed confirmation stays unconfirmed and is not re-sent again by itself', async () => {
		await watches.refresh();
		api.moveWatch.mockRejectedValueOnce({ status: 503 }).mockRejectedValueOnce({ status: 503 });
		await watches.move('a', 'dev_server');
		expect(await watches.reconcileMove('a')).toBe('uncertain');
		expect(get(watches).moves.a).toMatchObject({ phase: 'uncertain', confirming: true });
		expect(watches.reconcileMove('a')).toBeNull();
		expect(api.moveWatch).toHaveBeenCalledTimes(2);
		// The owner's own Retry starts a fresh attempt.
		api.moveWatch.mockResolvedValueOnce(row('a'));
		expect(await watches.move('a', 'dev_server')).toBe('moved');
	});

	it('classifies only a 409 as a refusal', () => {
		expect(moveFailure({ status: 409, detail: '  ' })).toEqual({ phase: 'failed', message: 'The worker refused the move.' });
		expect(moveFailure({ status: 503 })).toEqual({ phase: 'uncertain', message: null });
		expect(moveFailure(null)).toEqual({ phase: 'uncertain', message: null });
	});
});
