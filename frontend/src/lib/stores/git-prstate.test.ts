/**
 * Tests for the git store's PR-lifecycle state (prState).
 *
 * prState comes from the session-scoped GET /git/pr-state call and drives the
 * header's Finish / Create-PR decision. Covered here:
 *
 * - initial load fetches prState alongside status (parallel batch),
 * - the fetch is session-scoped (session_id reaches the API),
 * - refreshPrState honors its throttle unless forced,
 * - a context switch mid-flight never writes another context's PR state.
 */

import { describe, it, expect, vi, beforeEach } from 'vitest';

const mocks = vi.hoisted(() => ({
	getGitStatus: vi.fn(),
	getPrState: vi.fn(),
	getBranches: vi.fn(),
	getCommitGraph: vi.fn(),
	getWorktrees: vi.fn(),
	getDiffStats: vi.fn(),
	getChangedFiles: vi.fn()
}));

vi.mock('$lib/api/git', async (importOriginal) => {
	const actual = await importOriginal<typeof import('$lib/api/git')>();
	return { ...actual, ...mocks };
});

import { git } from './git.svelte';

function okStatus() {
	return {
		is_git_repo: true,
		current_branch: 'feat/x',
		remote_url: null,
		has_changes: false,
		staged: 0,
		modified: 0,
		untracked: 0,
		ahead: 1,
		behind: 0,
		differs_from_base: true
	};
}

function prState(number = 7) {
	return {
		current_branch: 'feat/x',
		has_github: true,
		open_pr: {
			number,
			title: 'Do a thing',
			url: `https://github.com/o/r/pull/${number}`,
			draft: false,
			state: 'OPEN',
			mergeable: null
		}
	};
}

beforeEach(() => {
	for (const m of Object.values(mocks)) m.mockReset();
	git.clear();
	vi.spyOn(console, 'warn').mockImplementation(() => {});
	vi.spyOn(console, 'error').mockImplementation(() => {});

	// Sane defaults for the parallel load batch.
	mocks.getGitStatus.mockResolvedValue(okStatus());
	mocks.getBranches.mockResolvedValue([]);
	mocks.getCommitGraph.mockResolvedValue({ commits: [] });
	mocks.getWorktrees.mockResolvedValue([]);
	mocks.getDiffStats.mockResolvedValue({ additions: 0, deletions: 0, files_changed: 0 });
	mocks.getChangedFiles.mockResolvedValue({ files: [] });
	mocks.getPrState.mockResolvedValue(prState());
});

describe('git store — prState', () => {
	it('loads prState in the initial parallel batch', async () => {
		await git.loadRepository('proj-1', false, 'ses-1');

		expect(git.current.prState?.open_pr?.number).toBe(7);
		expect(mocks.getPrState).toHaveBeenCalledWith('proj-1', undefined, 'ses-1');
	});

	it('worktree load scopes the pr-state call to the worktree', async () => {
		await git.loadForWorktree('proj-1', 'wt-1', false, 'ses-2');

		expect(mocks.getPrState).toHaveBeenCalledWith('proj-1', 'wt-1', 'ses-2');
	});

	it('a failed pr-state fetch degrades to null without failing the load', async () => {
		mocks.getPrState.mockRejectedValue(new Error('gh down'));

		await git.loadRepository('proj-1');

		expect(git.current.prState).toBeNull();
		expect(git.current.status?.is_git_repo).toBe(true);
		expect(git.current.loading).toBe(false);
	});

	it('refreshPrState is throttled unless forced', async () => {
		await git.loadRepository('proj-1', false, 'ses-1');
		const callsAfterLoad = mocks.getPrState.mock.calls.length;

		// Non-forced immediate second call is throttled away.
		await git.refreshPrState();
		expect(mocks.getPrState.mock.calls.length).toBe(callsAfterLoad);

		// Forced call bypasses the throttle.
		mocks.getPrState.mockResolvedValue(prState(9));
		await git.refreshPrState(true);
		expect(mocks.getPrState.mock.calls.length).toBe(callsAfterLoad + 1);
		expect(git.current.prState?.open_pr?.number).toBe(9);
	});

	it('context switch mid-flight drops the stale response', async () => {
		// Start a forced refresh under ses-1, then switch to a different
		// session's context before it resolves.
		await git.loadRepository('proj-1', false, 'ses-1');

		let resolveFetch!: (v: unknown) => void;
		const gate = new Promise((res) => {
			resolveFetch = res;
		});
		// Only the FIRST call (the forced refresh) is gated; the context-switch
		// load's own pr-state fetch resolves immediately.
		mocks.getPrState.mockImplementationOnce(() => gate);
		mocks.getPrState.mockResolvedValueOnce(prState());
		mocks.getPrState.mockResolvedValue(prState());

		const pending = git.refreshPrState(true);
		// Switch contexts — different sessionId ⇒ different cache key. Await it
		// fully so its parallel batch (including pr-state) has landed before the
		// gated stale response resolves.
		await git.loadRepository('proj-1', false, 'ses-2');

		resolveFetch(prState(42));
		await pending;

		// The stale ses-1 answer must NOT land in ses-2's state.
		expect(git.current.sessionId).toBe('ses-2');
		expect(git.current.prState?.open_pr?.number).not.toBe(42);
	});
});
