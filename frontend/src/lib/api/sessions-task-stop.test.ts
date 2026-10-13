import { beforeEach, expect, it, vi } from 'vitest';
import { stopSessionTask } from './sessions';

const client = vi.hoisted(() => ({ post: vi.fn() }));
vi.mock('./client', () => ({ api: client }));
beforeEach(() => vi.clearAllMocks());

it('stops a running task with encoded session and task ids', async () => {
	await stopSessionTask('parent/a', 'child b');
	expect(client.post).toHaveBeenCalledExactlyOnceWith('/sessions/parent%2Fa/tasks/child%20b/stop');
});

it('preserves a failed stop result without retrying', async () => {
	client.post.mockResolvedValue({ ok: false, detail: 'task not found' });
	expect(await stopSessionTask('parent', 'child')).toEqual({
		ok: false, detail: 'task not found'
	});
	expect(client.post).toHaveBeenCalledTimes(1);
});
