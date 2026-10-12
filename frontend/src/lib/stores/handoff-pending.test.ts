import { afterEach, describe, expect, it, vi } from 'vitest';
import { get } from 'svelte/store';

vi.mock('$lib/api/sessions', () => ({ getQueuedMove: vi.fn() }));

import { getQueuedMove } from '$lib/api/sessions';
import { HANDOFF_PENDING_READ_TIMEOUT_MS, HANDOFF_PENDING_RETRY_MS, beginHandoffPendingRead, handoffPending, recordHandoffPending, refreshHandoffPending, resetHandoffPending } from './handoff-pending';

afterEach(() => {
	vi.useRealTimers();
	resetHandoffPending();
	vi.mocked(getQueuedMove).mockReset();
});

describe('handoff-pending', () => {
	it('records what the owning worker reports, null included', async () => {
		vi.mocked(getQueuedMove).mockResolvedValueOnce({
			busy: { running: false, tasks: [] },
			pending: { handoff_id: 'X', worker: 'DesktopWorker', target_id: 'dev_gaming', by: 'agent', moving: true }
		});
		await refreshHandoffPending('s1', 'a');
		expect(get(handoffPending).s1).toEqual({ known: true, pending: expect.objectContaining({ handoff_id: 'X', target_id: 'dev_gaming' }), seq: expect.any(Number) });
		vi.mocked(getQueuedMove).mockResolvedValueOnce({ busy: { running: false, tasks: [] }, pending: null });
		await refreshHandoffPending('s1', 'b');
		expect(get(handoffPending).s1).toEqual({ known: true, pending: null, seq: expect.any(Number) });
	});

	it('a failed read keeps the previous view, so nothing settles on it', async () => {
		vi.mocked(getQueuedMove).mockRejectedValueOnce(new Error('offline'));
		await refreshHandoffPending('s2', 'a');
		expect(get(handoffPending).s2).toBeUndefined();
	});

	it('does not read the same reason twice at once', async () => {
		vi.mocked(getQueuedMove).mockResolvedValue({ busy: { running: false, tasks: [] }, pending: null });
		await Promise.all([refreshHandoffPending('s3', 'k'), refreshHandoffPending('s3', 'k')]);
		expect(getQueuedMove).toHaveBeenCalledTimes(1);
	});

	it('an older answer arriving last never replaces a newer one', async () => {
		const moving = { handoff_id: 'X', worker: 'DesktopWorker', target_id: 'dev_gaming', by: 'owner', moving: true };
		let answerOld!: (value: unknown) => void;
		vi.mocked(getQueuedMove).mockReturnValueOnce(new Promise((resolve) => { answerOld = resolve as (value: unknown) => void; }) as never);
		const old = refreshHandoffPending('s4', 'old-owner');
		vi.mocked(getQueuedMove).mockResolvedValueOnce({ busy: { running: false, tasks: [] }, pending: moving });
		await refreshHandoffPending('s4', 'new-owner');
		answerOld({ busy: { running: false, tasks: [] }, pending: null });
		await old;
		expect(get(handoffPending).s4?.pending).toEqual(moving);
		// The move menu's answer is ordered the same way, by when it was sent.
		const menu = beginHandoffPendingRead();
		vi.mocked(getQueuedMove).mockResolvedValueOnce({ busy: { running: false, tasks: [] }, pending: null });
		await refreshHandoffPending('s4', 'later');
		expect(recordHandoffPending('s4', moving, menu)).toBe(false);
		expect(get(handoffPending).s4?.pending).toBeNull();
	});

	it('a failed read is tried once more, and recovers', async () => {
		vi.useFakeTimers();
		vi.mocked(getQueuedMove).mockRejectedValueOnce(new Error('offline'));
		await refreshHandoffPending('s5', 'reload');
		expect(get(handoffPending).s5).toBeUndefined();
		vi.mocked(getQueuedMove).mockResolvedValueOnce({ busy: { running: false, tasks: [] }, pending: null });
		await vi.advanceTimersByTimeAsync(HANDOFF_PENDING_RETRY_MS + 10);
		expect(getQueuedMove).toHaveBeenCalledTimes(2);
		expect(get(handoffPending).s5).toMatchObject({ known: true, pending: null });
	});

	it('the timed retry is bounded: a second failure waits for the next change or remount', async () => {
		vi.useFakeTimers();
		vi.mocked(getQueuedMove).mockRejectedValue(new Error('offline'));
		await refreshHandoffPending('s6', 'reload');
		await vi.advanceTimersByTimeAsync(HANDOFF_PENDING_RETRY_MS * 5);
		expect(getQueuedMove).toHaveBeenCalledTimes(2);
		// A remount (the effect running again) reads again.
		await refreshHandoffPending('s6', 'reload');
		expect(getQueuedMove).toHaveBeenCalledTimes(3);
	});

	it('a read that never answers expires, frees the key, and its late answer is dropped', async () => {
		vi.useFakeTimers();
		let answerLate!: (value: unknown) => void;
		vi.mocked(getQueuedMove).mockReturnValueOnce(new Promise((resolve) => { answerLate = resolve as (value: unknown) => void; }) as never);
		const hung = refreshHandoffPending('s7', 'reload');
		// The same key while it hangs: still the one read.
		void refreshHandoffPending('s7', 'reload');
		expect(getQueuedMove).toHaveBeenCalledTimes(1);
		await vi.advanceTimersByTimeAsync(HANDOFF_PENDING_READ_TIMEOUT_MS + 10);
		await hung;
		expect(get(handoffPending).s7).toBeUndefined();
		// Past the deadline a remount reads again, and gets its answer.
		vi.mocked(getQueuedMove).mockResolvedValueOnce({ busy: { running: false, tasks: [] }, pending: { handoff_id: 'X', worker: 'DesktopWorker', target_id: 'dev_gaming', moving: true } } as never);
		await refreshHandoffPending('s7', 'reload');
		expect(getQueuedMove).toHaveBeenCalledTimes(2);
		expect(get(handoffPending).s7?.pending?.handoff_id).toBe('X');
		// The expired read's answer, arriving now, changes nothing.
		answerLate({ busy: { running: false, tasks: [] }, pending: null });
		await vi.advanceTimersByTimeAsync(0);
		expect(get(handoffPending).s7?.pending?.handoff_id).toBe('X');
	});

	it('an expired read is retried once by the timer', async () => {
		vi.useFakeTimers();
		vi.mocked(getQueuedMove).mockReturnValueOnce(new Promise(() => {}) as never);
		void refreshHandoffPending('s8', 'reload');
		vi.mocked(getQueuedMove).mockResolvedValueOnce({ busy: { running: false, tasks: [] }, pending: null });
		await vi.advanceTimersByTimeAsync(HANDOFF_PENDING_READ_TIMEOUT_MS + HANDOFF_PENDING_RETRY_MS + 20);
		expect(getQueuedMove).toHaveBeenCalledTimes(2);
		expect(get(handoffPending).s8).toMatchObject({ known: true, pending: null });
	});
});
