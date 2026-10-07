import { beforeEach, expect, it, vi } from 'vitest';
import { getSessionTaskStopState, stopSessionTask } from './sessions';

const client = vi.hoisted(() => ({ get: vi.fn(), post: vi.fn() }));
vi.mock('./client', () => ({ api: client }));
beforeEach(() => vi.clearAllMocks());

it('reads fresh terminal-task eligibility with the caller abort signal', async () => {
	const signal = new AbortController().signal;
	client.get.mockResolvedValue({ eligible: true, turn_id: 'turn-one' });
	expect(await getSessionTaskStopState('parent/a', 'child b', { signal })).toEqual({
		eligible: true, turn_id: 'turn-one'
	});
	expect(client.get).toHaveBeenCalledExactlyOnceWith(
		'/sessions/parent%2Fa/tasks/child%20b/stop-state', { signal }
	);
});

it('binds a terminal Stop to the fresh turn identity without changing the legacy stop', async () => {
	await stopSessionTask('parent', 'child', 'turn/a?b');
	expect(client.post).toHaveBeenLastCalledWith(
		'/sessions/parent/tasks/child/stop?expected_turn_id=turn%2Fa%3Fb'
	);
	await stopSessionTask('parent', 'child');
	expect(client.post).toHaveBeenLastCalledWith('/sessions/parent/tasks/child/stop');
});

it('does not turn a stale-target refusal into success or a new unscoped Stop', async () => {
	client.post.mockResolvedValue({ ok: false, detail: 'stop target changed' });
	expect(await stopSessionTask('parent', 'child', 'old-turn')).toEqual({
		ok: false, detail: 'stop target changed'
	});
	expect(client.post).toHaveBeenCalledTimes(1);
});
