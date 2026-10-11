import { afterEach, describe, expect, it, vi } from 'vitest';
import { get } from 'svelte/store';

vi.mock('$lib/api/sessions', () => ({ getQueuedMove: vi.fn() }));

import { getQueuedMove } from '$lib/api/sessions';
import { handoffPending, refreshHandoffPending, resetHandoffPending } from './handoff-pending';

afterEach(() => {
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
		expect(get(handoffPending).s1).toEqual({ known: true, pending: expect.objectContaining({ handoff_id: 'X', target_id: 'dev_gaming' }) });
		vi.mocked(getQueuedMove).mockResolvedValueOnce({ busy: { running: false, tasks: [] }, pending: null });
		await refreshHandoffPending('s1', 'b');
		expect(get(handoffPending).s1).toEqual({ known: true, pending: null });
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
});
