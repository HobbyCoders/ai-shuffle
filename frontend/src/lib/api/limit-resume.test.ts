import { beforeEach, describe, expect, it, vi } from 'vitest';
import { compactSession } from './limit-resume';
import { api } from './client';
vi.mock('./client', () => ({ api: { post: vi.fn() } }));

describe('compactSession', () => {
	beforeEach(() => vi.clearAllMocks());
	it('keeps the no-body call compatible', async () => {
		vi.mocked(api.post).mockResolvedValue({ turn_id: 'c', session_id: 'a/b' });
		await expect(compactSession('a/b')).resolves.toEqual({ turn_id: 'c', session_id: 'a/b' });
		expect(api.post).toHaveBeenCalledWith('/sessions/a%2Fb/compact', undefined);
	});
	it('posts the whole query and returns the queued turn id', async () => {
		const query = { type: 'query', prompt: 'hello', upload_paths: ['/uploads/a.png'], overrides: { effort: 'high' } };
		vi.mocked(api.post).mockResolvedValue({ turn_id: 'c', session_id: 's', queued_turn_id: 'q' });
		expect((await compactSession('s', query)).queued_turn_id).toBe('q');
		expect(api.post).toHaveBeenCalledWith('/sessions/s/compact', { then_send: query });
	});
});
