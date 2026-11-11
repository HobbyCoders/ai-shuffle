import { beforeEach, describe, expect, it, vi } from 'vitest';

const get = vi.hoisted(() => vi.fn());
vi.mock('$lib/api/client', () => ({ api: { get } }));

import { memoryHealth } from './memory-health.svelte';

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
	get.mockReset();
	memoryHealth.reset();
});

describe('memoryHealth store (#1042)', () => {
	it('is empty before an answer, then holds the latest one', async () => {
		get.mockResolvedValueOnce(loss).mockResolvedValueOnce(healthy);
		expect(memoryHealth.result).toBeNull();
		await memoryHealth.refresh();
		expect(get).toHaveBeenCalledWith('/memory/health');
		expect(memoryHealth.result).toMatchObject({ status: 'ok', data: { state: 'attention' } });
		await memoryHealth.refresh();
		expect(memoryHealth.result).toMatchObject({ status: 'ok', data: { state: 'healthy' } });
	});

	it('shares one request between callers', async () => {
		get.mockResolvedValue(healthy);
		const first = memoryHealth.refresh();
		const second = memoryHealth.refresh();
		await Promise.all([first, second]);
		expect(get).toHaveBeenCalledTimes(1);
	});

	it('forgets the answer on reset (sign-out, account switch)', async () => {
		get.mockResolvedValue(loss);
		await memoryHealth.refresh();
		memoryHealth.reset();
		expect(memoryHealth.result).toBeNull();
	});

	it('drops an answer that lands after the store was reset', async () => {
		let answer!: (body: unknown) => void;
		get.mockReturnValueOnce(new Promise((done) => (answer = done)));
		const pending = memoryHealth.refresh();
		memoryHealth.reset();
		answer(loss);
		await pending;
		expect(memoryHealth.result).toBeNull();
	});
});
