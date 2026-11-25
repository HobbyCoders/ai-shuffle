import { describe, expect, it } from 'vitest';
import { removalCheckError, removalConfirmText } from './project-removal';

describe('removalConfirmText', () => {
	it('names the number of chats the worker removes', () => {
		expect(removalConfirmText('Demo', 'Worker A', { chats: 3, worktrees: 0 }))
			.toBe('Remove Demo from Worker A and its 3 chats? This cannot be undone. Files stay on disk.');
		expect(removalConfirmText('Demo', null, { chats: 1, worktrees: null }))
			.toBe('Remove Demo and its 1 chat? This cannot be undone. Files stay on disk.');
	});

	it('says when nothing is lost and when the count is unknown', () => {
		expect(removalConfirmText('Demo', 'Worker A', { chats: 0, worktrees: 0 }))
			.toBe('Remove Demo from Worker A? It has no chats. Files stay on disk.');
		expect(removalConfirmText('Demo', 'Worker A', { chats: null, worktrees: null }))
			.toBe('Remove Demo from Worker A and all its chats? This cannot be undone. Files stay on disk.');
	});

	it('says the worktree folders stay on disk', () => {
		expect(removalConfirmText('Demo', null, { chats: 2, worktrees: 1 })).toContain('1 worktree folder stays on disk too.');
		expect(removalConfirmText('Demo', null, { chats: 2, worktrees: 4 })).toContain('4 worktree folders stay on disk too.');
	});
});

describe('removalCheckError', () => {
	it('confirms only a fresh ready row with a numeric count, zero included', () => {
		expect(removalCheckError({ state: 'ready', removal_chats: 0 }, 'Tower')).toBeNull();
		expect(removalCheckError({ state: 'ready', removal_chats: 4 }, 'Tower')).toBeNull();
	});

	it('names why anything else cannot be confirmed', () => {
		expect(removalCheckError(null, 'Tower')).toBe('Tower no longer lists this project.');
		expect(removalCheckError({ state: 'unknown', removal_chats: null }, 'Tower')).toMatch(/didn't answer/);
		expect(removalCheckError({ state: 'offline_has', removal_chats: 2 }, 'Tower')).toMatch(/doesn't have this project ready/);
		expect(removalCheckError({ state: 'ready', removal_chats: null }, 'Tower')).toMatch(/needs an update/);
		expect(removalCheckError({ state: 'ready' }, 'Tower')).toMatch(/needs an update/);
	});
});
