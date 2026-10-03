import { beforeEach, describe, expect, it, vi } from 'vitest';

const get = vi.fn();
const put = vi.fn();
vi.mock('$lib/api/client', () => ({ api: { get: (...a: unknown[]) => get(...a), put: (...a: unknown[]) => put(...a) } }));

import { autoMerge } from './auto-merge.svelte';

describe('merge when green store', () => {
	beforeEach(() => {
		get.mockReset();
		put.mockReset();
	});

	it('loads and toggles a session watch', async () => {
		get.mockResolvedValue({ enabled: false, pr_number: null });
		await autoMerge.load('s1');
		expect(autoMerge.enabled('s1')).toBe(false);
		put.mockResolvedValue({ enabled: true, pr_number: 7 });
		await autoMerge.toggle('s1');
		expect(put).toHaveBeenCalledWith('/sessions/s1/auto-merge', { enabled: true });
		expect(autoMerge.enabled('s1')).toBe(true);
	});

	it('shows the refusal reason instead of flipping', async () => {
		put.mockRejectedValue(new Error('This session has no open pull request to merge.'));
		await autoMerge.toggle('s2');
		expect(autoMerge.enabled('s2')).toBe(false);
		expect(autoMerge.error.s2).toContain('no open pull request');
		expect(autoMerge.busy.s2).toBe(false);
	});
});
