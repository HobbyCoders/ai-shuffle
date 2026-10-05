import { beforeEach, describe, expect, it, vi } from 'vitest';

const mocks = vi.hoisted(() => ({ get: vi.fn(), put: vi.fn() }));
vi.mock('./client', () => ({ api: { get: mocks.get, put: mocks.put } }));

import { getSubagentDefault, normalizeSubagentDefault, putSubagentDefault } from './subagentDefault';

beforeEach(() => vi.clearAllMocks());

describe('subagent default client', () => {
	it('reads the contract shape', async () => {
		mocks.get.mockResolvedValue({ model: 'sonnet' });
		await expect(getSubagentDefault()).resolves.toEqual({ model: 'sonnet' });
		expect(mocks.get).toHaveBeenCalledWith('/settings/subagent-default');
	});

	it('answers null when the worker has no such endpoint', async () => {
		mocks.get.mockRejectedValue({ status: 404, detail: 'Not Found' });
		await expect(getSubagentDefault()).resolves.toBeNull();
	});

	it('passes other failures on', async () => {
		mocks.get.mockRejectedValue({ status: 500, detail: 'boom' });
		await expect(getSubagentDefault()).rejects.toMatchObject({ status: 500 });
	});

	it('saves the model', async () => {
		mocks.put.mockResolvedValue({ model: 'opus' });
		await putSubagentDefault({ model: 'opus' });
		expect(mocks.put).toHaveBeenCalledWith('/settings/subagent-default', { model: 'opus' });
	});

	it('normalises missing or odd fields', () => {
		expect(normalizeSubagentDefault(null)).toEqual({ model: null });
		// An older worker still sends the retired allowed list; it is dropped.
		expect(normalizeSubagentDefault({ model: '', allowed: ['a'], extra: 1 })).toEqual({ model: null });
	});
});
