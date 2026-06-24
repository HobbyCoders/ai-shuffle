/**
 * Tests for the git store's "cleaned-up / reused worktree" handling.
 *
 * Worktree sessions are reusable, so a worktree's folder can be removed while its
 * session is later reopened. When the backend reports the worktree is gone (404),
 * the store must fail closed: drop any stale `is_git_repo: true` and surface a
 * definitive no-repo status so the header hides Ship It and other git controls
 * instead of acting on a folder that no longer has a repo.
 *
 * The repo's convention is to mock the API module (see gitCheckoutAction.test.ts)
 * — here we mock getGitStatus to reject with a 404 and assert the resulting state.
 */

import { describe, it, expect, vi, beforeEach } from 'vitest';

const mocks = vi.hoisted(() => ({ getGitStatus: vi.fn() }));

vi.mock('$lib/api/git', async (importOriginal) => {
	const actual = await importOriginal<typeof import('$lib/api/git')>();
	return { ...actual, getGitStatus: mocks.getGitStatus };
});

import { git } from './git.svelte';

beforeEach(() => {
	mocks.getGitStatus.mockReset();
	git.clear();
	vi.spyOn(console, 'warn').mockImplementation(() => {});
	vi.spyOn(console, 'error').mockImplementation(() => {});
});

describe('git store — cleaned-up / reused worktree', () => {
	it('reports a definitive no-repo status when the worktree folder is gone (404)', async () => {
		mocks.getGitStatus.mockRejectedValue({ status: 404, detail: 'Worktree not found' });

		await git.loadForWorktree('proj-1', 'wt-gone');

		// Fail closed: is_git_repo must be false so Ship It / git controls hide,
		// not left at a stale true from a prior populated load.
		expect(git.current.status?.is_git_repo).toBe(false);
		expect(git.worktreeGone).toBe(true);
		// Stale collections cleared so nothing resurrects the old repo view.
		expect(git.current.branches).toEqual([]);
		expect(git.current.diffStats).toBeNull();
		expect(git.current.loading).toBe(false);
	});

	it('does NOT treat a project (non-worktree) load 404 as a gone worktree', async () => {
		// worktree-gone handling is scoped to worktree sessions; a project-level
		// failure keeps the normal error path (no false "no repo" signal).
		mocks.getGitStatus.mockRejectedValue({ status: 404, detail: 'boom' });

		await git.loadRepository('proj-1');

		expect(git.worktreeGone).toBe(false);
		expect(git.current.error).toBeTruthy();
	});
});
