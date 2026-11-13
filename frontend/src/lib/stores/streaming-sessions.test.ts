import { beforeEach, describe, expect, it, vi } from 'vitest';

type Mod = typeof import('./streaming-sessions');
let mod: Mod;

beforeEach(async () => {
	vi.resetModules();
	mod = await import('./streaming-sessions');
});

const row = (id: string, is_streaming: boolean | null | undefined, extra: Record<string, unknown> = {}) => ({
	id, project_id: 'p1', is_streaming, ...extra
});

describe('seedStreamingFromList (#1527)', () => {
	it('marks the listed running chats before the socket snapshot arrives', () => {
		mod.seedStreamingFromList([
			row('a', true, { worktree_id: 'w1', branch: 'feat/x' }),
			row('b', false)
		]);
		expect([...mod.streamingSessions.current.keys()]).toEqual(['a']);
		expect(mod.streamingSessions.current.get('a')).toEqual({
			projectId: 'p1', worktreeId: 'w1', worktreeBranch: 'feat/x'
		});
	});

	it('follows a newer list until then, and leaves rows that do not say alone', () => {
		mod.seedStreamingFromList([row('a', true), row('b', true)]);
		mod.seedStreamingFromList([row('a', false), row('b', undefined), row('c', null)]);
		expect([...mod.streamingSessions.current.keys()]).toEqual(['b']);
	});

	it('changes nothing once the snapshot has arrived', () => {
		mod.streamingSessions.set(new Map([['b', { projectId: null, worktreeId: null }]]));
		mod.seedStreamingFromList([row('a', true), row('b', false)]);
		expect([...mod.streamingSessions.current.keys()]).toEqual(['b']);
	});

	it('never lets a slower list undo a live change that arrived before the snapshot', () => {
		// The socket broadcasts to a connection before it sends its snapshot, so a
		// live change can land while an older list request is still in flight.
		const live = (id: string, running: boolean) =>
			mod.streamingSessions.update((m) => {
				const next = new Map(m);
				if (running) next.set(id, { projectId: 'p1', worktreeId: null });
				else next.delete(id);
				return next;
			}, id);
		live('a', false); // a finished
		live('b', true); // b started
		mod.seedStreamingFromList([row('a', true), row('b', false), row('c', true)]);
		expect([...mod.streamingSessions.current.keys()].sort()).toEqual(['b', 'c']);

		// The snapshot is the whole truth again.
		mod.streamingSessions.set(new Map([['a', { projectId: null, worktreeId: null }]]));
		expect([...mod.streamingSessions.current.keys()]).toEqual(['a']);
	});

	it('does not notify when the list agrees with the map', () => {
		mod.seedStreamingFromList([row('a', true)]);
		const seen: unknown[] = [];
		const stop = mod.streamingSessions.subscribe((m) => seen.push(m));
		seen.length = 0;
		mod.seedStreamingFromList([row('a', true), row('b', false)]);
		stop();
		expect(seen).toEqual([]);
	});
});
