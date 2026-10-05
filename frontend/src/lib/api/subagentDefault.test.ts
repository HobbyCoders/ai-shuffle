import { beforeEach, describe, expect, it, vi } from 'vitest';

const mocks = vi.hoisted(() => ({ get: vi.fn(), put: vi.fn() }));
vi.mock('./client', () => ({ api: { get: mocks.get, put: mocks.put } }));

import { getSubagentDefault, normalizeSubagentDefault, putSubagentDefault } from './subagentDefault';

beforeEach(() => vi.clearAllMocks());

describe('subagent default client', () => {
	it('reads the contract shape', async () => {
		mocks.get.mockResolvedValue({ model: 'sonnet', allowed: ['sonnet', 'haiku'] });
		await expect(getSubagentDefault()).resolves.toEqual({ model: 'sonnet', allowed: ['sonnet', 'haiku'] });
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

	it('saves the model and the allowed list', async () => {
		mocks.put.mockResolvedValue({ model: 'opus', allowed: [] });
		await putSubagentDefault({ model: 'opus', allowed: [] });
		expect(mocks.put).toHaveBeenCalledWith('/settings/subagent-default', { model: 'opus', allowed: [] });
	});

	it('normalises missing or odd fields', () => {
		expect(normalizeSubagentDefault(null)).toEqual({ model: null, allowed: [] });
		expect(normalizeSubagentDefault({ model: '', allowed: ['a', 3, ''], extra: 1 })).toEqual({ model: null, allowed: ['a'] });
	});
});
