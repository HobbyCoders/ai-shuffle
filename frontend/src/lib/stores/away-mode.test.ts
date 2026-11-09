import { beforeEach, describe, expect, it, vi } from 'vitest';

const get = vi.fn();
const getShared = vi.fn();
const put = vi.fn();
vi.mock('$lib/api/client', () => ({ api: {
	get: (...a: unknown[]) => get(...a), getShared: (...a: unknown[]) => getShared(...a), put: (...a: unknown[]) => put(...a)
} }));

import { AWAY_CONFIRM_GRACE_MS, awayMode, globalAwayOnMessage } from './away-mode.svelte';

describe('away mode store', () => {
	beforeEach(() => {
		get.mockReset();
		getShared.mockReset();
		put.mockReset();
		vi.unstubAllEnvs();
	});

	it('loads the persisted flag once per session from the metadata read, not the transcript', async () => {
		getShared.mockResolvedValue({ session_meta: { away_mode: true, is_worker: false } });
		await awayMode.load('s-load');
		await awayMode.load('s-load');
		expect(getShared).toHaveBeenCalledTimes(1);
		expect(getShared).toHaveBeenCalledWith('/sessions/s-load/live?include_messages=false&owner_fields=true');
		expect(get).not.toHaveBeenCalled();
		expect(awayMode.isChatAway('s-load')).toBe(true);
		expect(awayMode.isChatAway(null)).toBe(false);
	});

	it('falls back to the session row when the worker does not report the flag', async () => {
		getShared.mockResolvedValue({ session_meta: { mode: 'code' } });
		get.mockResolvedValue({ away_mode: true });
		await awayMode.load('s-old-worker');
		expect(get).toHaveBeenCalledWith('/sessions/s-old-worker');
		expect(awayMode.isChatAway('s-old-worker')).toBe(true);
	});

	it('reads the whole session row with VITE_PERF_SLIM_SESSION_READ=0', async () => {
		vi.stubEnv('VITE_PERF_SLIM_SESSION_READ', '0');
		get.mockResolvedValue({ away_mode: true });
		await awayMode.load('s-flag-off');
		expect(getShared).not.toHaveBeenCalled();
		expect(get).toHaveBeenCalledWith('/sessions/s-flag-off');
		expect(awayMode.isChatAway('s-flag-off')).toBe(true);
	});

	it('flips the flag through the owning worker and reports released questions', async () => {
		put.mockResolvedValue({ away: true, released_questions: 2 });
		const result = await awayMode.set('s-set', true);
		expect(put).toHaveBeenCalledWith('/sessions/s-set/away-mode', { away: true });
		expect(result.released_questions).toBe(2);
		expect(awayMode.isChatAway('s-set')).toBe(true);
		expect(awayMode.busy['s-set']).toBe(false);
	});

	it('follows the session row when another device or worker changes it', () => {
		awayMode.observe('s-row', true);
		expect(awayMode.isChatAway('s-row')).toBe(true);
		awayMode.observe('s-row', false);
		expect(awayMode.isChatAway('s-row')).toBe(false);
		// A flag never set is off; a row without the field (older server) changes nothing.
		awayMode.observe('s-row', true);
		awayMode.observe('s-row', undefined);
		expect(awayMode.isChatAway('s-row')).toBe(true);
		awayMode.observe('s-row', null);
		expect(awayMode.isChatAway('s-row')).toBe(false);
	});

	it('does not refetch a session whose row already reported the flag', async () => {
		awayMode.observe('s-row-first', true);
		await awayMode.load('s-row-first');
		expect(get).not.toHaveBeenCalled();
		expect(awayMode.isChatAway('s-row-first')).toBe(true);
	});

	it('keeps a confirmed toggle while a remote mirror still reports the old value', async () => {
		vi.useFakeTimers();
		try {
			awayMode.observe('s-remote', false);
			put.mockResolvedValue({ away: true });
			await awayMode.set('s-remote', true);
			// The owning worker confirmed; this worker's mirror has not caught up yet.
			awayMode.observe('s-remote', false);
			expect(awayMode.isChatAway('s-remote')).toBe(true);
			// The mirror catches up; from then on the row is followed again.
			awayMode.observe('s-remote', true);
			awayMode.observe('s-remote', false);
			expect(awayMode.isChatAway('s-remote')).toBe(false);

			put.mockResolvedValue({ away: true });
			await awayMode.set('s-remote', true);
			vi.advanceTimersByTime(AWAY_CONFIRM_GRACE_MS + 1);
			// Past the grace period a disagreeing row wins (changed elsewhere).
			awayMode.observe('s-remote', false);
			expect(awayMode.isChatAway('s-remote')).toBe(false);
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
			expect(awayMode.isChatAway('s-mask')).toBe(true);
			vi.advanceTimersByTime(AWAY_CONFIRM_GRACE_MS + 1);
			expect(awayMode.isChatAway('s-mask')).toBe(false);
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
			expect(awayMode.isChatAway('s-old-owner')).toBe(true);
			// A real value from the owner still wins.
			awayMode.observe('s-old-owner', false);
			expect(awayMode.isChatAway('s-old-owner')).toBe(false);
		} finally {
			vi.useRealTimers();
		}
	});

	it('keeps the old value when the change fails', async () => {
		put.mockRejectedValue(new Error('unreachable'));
		await expect(awayMode.set('s-fail', true)).rejects.toThrow('unreachable');
		expect(awayMode.isChatAway('s-fail')).toBe(false);
		expect(awayMode.busy['s-fail']).toBe(false);
	});
});

describe('Away for all chats', () => {
	beforeEach(() => {
		awayMode.setGlobalAccess(false);
		get.mockReset();
		getShared.mockReset();
		put.mockReset();
	});

	const flush = () => new Promise<void>((resolve) => setTimeout(resolve, 0));

	it('sends nothing for someone who is not an admin', async () => {
		await awayMode.refreshGlobal();
		await expect(awayMode.setGlobal(true)).rejects.toThrow();
		expect(get).not.toHaveBeenCalled();
		expect(put).not.toHaveBeenCalled();
		expect(awayMode.globalLoaded).toBe(false);
	});

	it('is not known until the worker has answered', async () => {
		let answer!: (value: unknown) => void;
		get.mockReturnValue(new Promise((resolve) => { answer = resolve; }));
		awayMode.setGlobalAccess(true);
		expect(get).toHaveBeenCalledWith('/away-mode');
		expect(awayMode.globalLoaded).toBe(false);
		answer({ away: true });
		await flush();
		expect(awayMode.globalLoaded).toBe(true);
		expect(awayMode.globalAway).toBe(true);
	});

	it('forgets the value when admin access goes away', async () => {
		get.mockResolvedValue({ away: true });
		awayMode.setGlobalAccess(true);
		await flush();
		awayMode.setGlobalAccess(false);
		expect(awayMode.globalAway).toBe(false);
		expect(awayMode.globalLoaded).toBe(false);
	});

	it('keeps a chat\'s own flag apart from what the chat obeys', async () => {
		getShared.mockResolvedValue({ session_meta: { away_mode: false, is_worker: false } });
		await awayMode.load('s-held');
		get.mockResolvedValue({ away: true });
		awayMode.setGlobalAccess(true);
		await flush();
		expect(awayMode.isChatAway('s-held')).toBe(false);
		expect(awayMode.isEffectivelyAway('s-held')).toBe(true);
		expect(awayMode.isEffectivelyAway(null)).toBe(true);
		get.mockResolvedValue({ away: false });
		await awayMode.refreshGlobal();
		expect(awayMode.isEffectivelyAway('s-held')).toBe(false);
		expect(put).not.toHaveBeenCalled();
	});

	it('shows the worker\'s answer and returns its counts', async () => {
		get.mockResolvedValue({ away: false });
		awayMode.setGlobalAccess(true);
		await flush();
		put.mockResolvedValue({ away: true, released_questions: 2, deferred_approvals: 1 });
		const change = awayMode.setGlobal(true);
		expect(awayMode.globalBusy).toBe(true);
		expect(awayMode.globalAway).toBe(false);
		await expect(change).resolves.toEqual({ away: true, released_questions: 2, deferred_approvals: 1 });
		expect(put).toHaveBeenCalledWith('/away-mode', { away: true });
		expect(awayMode.globalAway).toBe(true);
		expect(awayMode.globalBusy).toBe(false);
	});

	it('stays truthful when the change fails, and reads again in case it landed', async () => {
		get.mockResolvedValue({ away: false });
		awayMode.setGlobalAccess(true);
		await flush();
		get.mockClear();
		put.mockRejectedValue(new Error('unreachable'));
		await expect(awayMode.setGlobal(true)).rejects.toThrow('unreachable');
		expect(awayMode.globalAway).toBe(false);
		expect(awayMode.globalBusy).toBe(false);
		expect(get).toHaveBeenCalledTimes(1);
	});

	it('drops a read that started before a write', async () => {
		get.mockResolvedValue({ away: false });
		awayMode.setGlobalAccess(true);
		await flush();
		let staleAnswer!: (value: unknown) => void;
		get.mockReturnValue(new Promise((resolve) => { staleAnswer = resolve; }));
		const read = awayMode.refreshGlobal();
		put.mockResolvedValue({ away: true, released_questions: 0, deferred_approvals: 0 });
		await awayMode.setGlobal(true);
		staleAnswer({ away: false });
		await read;
		expect(awayMode.globalAway).toBe(true);
	});

	it('reads again after a write when a change signal landed during it', async () => {
		get.mockResolvedValue({ away: false });
		awayMode.setGlobalAccess(true);
		await flush();
		get.mockClear();
		let finish!: (value: unknown) => void;
		put.mockReturnValue(new Promise((resolve) => { finish = resolve; }));
		const change = awayMode.setGlobal(true);
		await awayMode.refreshGlobal();
		expect(get).not.toHaveBeenCalled();
		get.mockResolvedValue({ away: true });
		finish({ away: true, released_questions: 0, deferred_approvals: 0 });
		await change;
		expect(get).toHaveBeenCalledTimes(1);
	});

	it('words the confirmation from the counts', () => {
		expect(globalAwayOnMessage({ released_questions: 0, deferred_approvals: 0 })).toBe('Away for all chats is on.');
		expect(globalAwayOnMessage({ released_questions: 1, deferred_approvals: 0 })).toBe('Away for all chats is on. Answered 1 waiting question.');
		expect(globalAwayOnMessage({ released_questions: 0, deferred_approvals: 3 })).toBe('Away for all chats is on. Put off 3 approvals.');
		expect(globalAwayOnMessage({ released_questions: 2, deferred_approvals: 1 })).toBe('Away for all chats is on. Answered 2 waiting questions and put off 1 approval.');
	});
});
