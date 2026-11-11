import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const get = vi.hoisted(() => vi.fn());
vi.mock('$lib/api/client', () => ({ api: { get } }));

import { MEMORY_HEALTH_REFRESH_MS, memoryHealth } from './memory-health.svelte';

const loss = {
	state: 'attention',
	checked_at: 1,
	signals: [
		{ id: 'capacity', label: 'Capacity', state: 'attention', reason: 'Automatic cleanup has recorded 3 permanent deletions of older facts. Recall cannot recover a deleted copy.' }
	],
	repairs: []
};
const healthy = { state: 'healthy', checked_at: 1, signals: [], repairs: [] };

beforeEach(() => {
	vi.useFakeTimers();
	get.mockReset();
	memoryHealth.reset();
});
afterEach(() => {
	memoryHealth.reset();
	vi.useRealTimers();
});

describe('memoryHealth store (#1042)', () => {
	it('has no warning before an answer, and the first reason once the check reports attention', async () => {
		get.mockResolvedValue(loss);
		expect(memoryHealth.result).toBeNull();
		expect(memoryHealth.alert).toBe('');
		await memoryHealth.refresh();
		expect(get).toHaveBeenCalledWith('/memory/health');
		expect(memoryHealth.alert).toBe('Capacity: Automatic cleanup has recorded 3 permanent deletions of older facts.');
	});

	it('clears the warning when the check reports healthy', async () => {
		get.mockResolvedValueOnce(loss).mockResolvedValueOnce(healthy);
		await memoryHealth.refresh();
		expect(memoryHealth.alert).not.toBe('');
		await memoryHealth.refresh();
		expect(memoryHealth.alert).toBe('');
	});

	it.each([
		['a server error', Object.assign(new Error('Server error'), { status: 500 })],
		['no approval', Object.assign(new Error('Owner access required'), { status: 403 })],
		['an unreachable worker', new TypeError('Failed to fetch')]
	])('never warns on %s', async (_name, error) => {
		get.mockResolvedValueOnce(loss).mockRejectedValueOnce(error);
		await memoryHealth.refresh();
		await memoryHealth.refresh();
		expect(memoryHealth.result?.status).not.toBe('ok');
		expect(memoryHealth.alert).toBe('');
	});

	it.each([
		['an empty body', {}],
		['a healthy store with a note', { ...healthy, signals: [{ id: 'capacity', label: 'Capacity', state: 'healthy', note: true, reason: 'Past deletions.' }] }],
		['an unknown state', { ...loss, state: 'unknown' }]
	])('never warns for %s', async (_name, body) => {
		get.mockResolvedValue(body);
		await memoryHealth.refresh();
		expect(memoryHealth.alert).toBe('');
	});

	it('asks once at start, then on its interval, and shares one request between callers', async () => {
		get.mockResolvedValue(loss);
		const stop = memoryHealth.start();
		const again = memoryHealth.start();
		expect(get).toHaveBeenCalledTimes(1);
		// Settings asking while the start request is in flight shares it.
		await memoryHealth.refresh();
		expect(get).toHaveBeenCalledTimes(1);
		await vi.advanceTimersByTimeAsync(MEMORY_HEALTH_REFRESH_MS - 1);
		expect(get).toHaveBeenCalledTimes(1);
		await vi.advanceTimersByTimeAsync(1);
		expect(get).toHaveBeenCalledTimes(2);
		again();
		await vi.advanceTimersByTimeAsync(MEMORY_HEALTH_REFRESH_MS);
		expect(get).toHaveBeenCalledTimes(3);
		stop();
		await vi.advanceTimersByTimeAsync(MEMORY_HEALTH_REFRESH_MS * 2);
		expect(get).toHaveBeenCalledTimes(3);
	});

	it('does not refresh more often than the worker sweeps its check', () => {
		// The worker's maintenance sweep wakes every 5 minutes and checks a
		// store at most hourly (app/core/fluid_memory.py).
		expect(MEMORY_HEALTH_REFRESH_MS).toBeGreaterThanOrEqual(5 * 60_000);
		expect(MEMORY_HEALTH_REFRESH_MS).toBeLessThanOrEqual(60 * 60_000);
	});

	it('forgets the answer when the last holder stops (sign-out, account switch)', async () => {
		get.mockResolvedValue(loss);
		const stop = memoryHealth.start();
		await vi.advanceTimersByTimeAsync(0);
		expect(memoryHealth.alert).not.toBe('');
		stop();
		expect(memoryHealth.result).toBeNull();
		expect(memoryHealth.alert).toBe('');
	});

	it('drops an answer that lands after the store was reset', async () => {
		let answer!: (body: unknown) => void;
		get.mockReturnValueOnce(new Promise((done) => (answer = done)));
		const pending = memoryHealth.refresh();
		memoryHealth.reset();
		answer(loss);
		await pending;
		expect(memoryHealth.result).toBeNull();
		expect(memoryHealth.alert).toBe('');
	});
});
