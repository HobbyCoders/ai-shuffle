/**
 * Git store — refresh coalescing, context switches, error handling, branch
 * checkout and the slow background refresh.
 *
 * Complements git.svelte.test.ts (cache identity, versioning, inline payloads)
 * and git-prstate.test.ts (PR verdicts). The API module is mocked, as in those
 * files; the store's own state machine runs for real.
 */

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';

const mocks = vi.hoisted(() => ({
	getGitStatus: vi.fn(),
	getPrState: vi.fn(),
	getBranches: vi.fn(),
	getCommitGraph: vi.fn(),
	getWorktrees: vi.fn(),
	getDiffStats: vi.fn(),
	getChangedFiles: vi.fn(),
	fetchRemote: vi.fn(),
	checkout: vi.fn(),
	deleteWorktree: vi.fn(),
	forgetWorktree: vi.fn()
}));

// Records every property the git store reads off the GitHub store, so the
// background-refresh test can prove which GitHub calls a tick makes.
const githubAccess = vi.hoisted(() => ({ props: [] as string[], ensureRepoInfo: vi.fn() }));

vi.mock('$lib/api/git', async (importOriginal) => {
	const actual = await importOriginal<typeof import('$lib/api/git')>();
	return { ...actual, ...mocks };
});

vi.mock('./github.svelte', () => ({
	github: new Proxy(
		{},
		{
			get(_t, prop: string) {
				githubAccess.props.push(prop);
				if (prop === 'ensureRepoInfo') return githubAccess.ensureRepoInfo;
				return vi.fn().mockResolvedValue(undefined);
			}
		}
	)
}));

import { transformGitStatus, type GitStatus, type PrState } from '$lib/api/git';
import { git } from './git.svelte';

function status(overrides: Record<string, unknown> = {}): GitStatus {
	return transformGitStatus({
		is_git_repo: true,
		current_branch: 'feat/x',
		default_branch: 'main',
		repository_id: 'repo-1',
		modified: ['a.ts'],
		untracked: [],
		ahead: 0,
		differs_from_base: true,
		...overrides
	});
}

function prState(number: number | null = 7): PrState {
	return {
		current_branch: 'feat/x',
		has_github: true,
		open_pr:
			number === null
				? null
				: { number, title: 'PR', url: `https://x/pull/${number}`, draft: false, state: 'OPEN', mergeable: null },
		lookup_failed: false,
		served_stale: false
	};
}

function deferred<T>() {
	let resolve!: (v: T) => void;
	let reject!: (e: unknown) => void;
	const promise = new Promise<T>((res, rej) => {
		resolve = res;
		reject = rej;
	});
	return { promise, resolve, reject };
}

beforeEach(() => {
	for (const m of Object.values(mocks)) m.mockReset();
	githubAccess.props.length = 0;
	githubAccess.ensureRepoInfo.mockReset().mockResolvedValue(true);
	git.clear();
	vi.spyOn(console, 'warn').mockImplementation(() => {});
	vi.spyOn(console, 'error').mockImplementation(() => {});
	vi.spyOn(console, 'debug').mockImplementation(() => {});

	mocks.getGitStatus.mockResolvedValue(status());
	mocks.getBranches.mockResolvedValue([{ name: 'feat/x', current: true, remote: false }]);
	mocks.getCommitGraph.mockResolvedValue({ commits: [] });
	mocks.getWorktrees.mockResolvedValue([]);
	mocks.getDiffStats.mockResolvedValue({ additions: 1, deletions: 0, files_changed: 1 });
	mocks.getChangedFiles.mockResolvedValue({ files: [] });
	mocks.getPrState.mockResolvedValue(prState());
	mocks.fetchRemote.mockRejectedValue(new Error('no remote'));
});

afterEach(() => {
	git.stopBackgroundRefresh();
	vi.useRealTimers();
	vi.restoreAllMocks();
});

describe('git store — status refresh', () => {
	it('coalesces overlapping polls into one request', async () => {
		await git.loadRepository('p', false, 's1');
		const calls = mocks.getGitStatus.mock.calls.length;

		const pending = deferred<GitStatus>();
		mocks.getGitStatus.mockReturnValueOnce(pending.promise);
		const first = git.refreshStatus();
		const second = git.refreshStatus();
		const third = git.refreshStatus();

		expect(mocks.getGitStatus.mock.calls.length).toBe(calls + 1);
		pending.resolve(status({ modified: ['a.ts', 'new.ts'] }));
		await Promise.all([first, second, third]);

		expect(git.current.status?.files_fingerprint).toBe('|a.ts,new.ts||');
		// The guard is released, so the next poll goes out again.
		await git.refreshStatus();
		expect(mocks.getGitStatus.mock.calls.length).toBe(calls + 2);
	});

	it('drops a poll response that lands after switching to another session', async () => {
		// Session s2 sits on branch "other"; s1 on "feat/x". Both are cached.
		mocks.getGitStatus.mockResolvedValue(status({ current_branch: 'other', modified: ['b-only.ts'] }));
		mocks.getBranches.mockResolvedValue([{ name: 'other', current: true, remote: false }]);
		await git.loadRepository('p', false, 's2');
		mocks.getGitStatus.mockResolvedValue(status());
		mocks.getBranches.mockResolvedValue([{ name: 'feat/x', current: true, remote: false }]);
		await git.loadRepository('p', false, 's1');

		const slow = deferred<GitStatus>();
		mocks.getGitStatus.mockReturnValueOnce(slow.promise);
		const poll = git.refreshStatus();

		git.restoreCacheImmediate('p', null, 'project', 's2');
		expect(git.current.status?.current_branch).toBe('other');

		slow.resolve(status({ modified: ['a.ts', 'late.ts'] }));
		await poll;

		expect(git.current.sessionId).toBe('s2');
		expect(git.current.status?.current_branch).toBe('other');
		expect(git.current.status?.files_fingerprint).toBe('|b-only.ts||');
	});

	it('keeps the last good status on errors and backs off after a 5xx', async () => {
		vi.useFakeTimers();
		await git.loadRepository('p', false, 's1');
		const good = git.current.status;

		mocks.getGitStatus.mockRejectedValueOnce({ status: 400, detail: 'bad ref' });
		await git.refreshStatus();
		expect(git.current.error).toBe('bad ref');
		expect(git.current.status).toBe(good);

		mocks.getGitStatus.mockRejectedValueOnce({ status: 503, detail: 'down' });
		await git.refreshStatus();
		expect(git.current.status).toBe(good);
		expect(git.serverBackoff).toBe(true);

		const before = mocks.getGitStatus.mock.calls.length;
		await git.refreshStatus();
		expect(mocks.getGitStatus.mock.calls.length).toBe(before); // skipped while backing off

		vi.advanceTimersByTime(2_100);
		mocks.getGitStatus.mockResolvedValueOnce(status({ modified: ['a.ts', 'recovered.ts'] }));
		await git.refreshStatus();
		expect(git.serverBackoff).toBe(false);
		expect(git.current.status?.files_fingerprint).toBe('|a.ts,recovered.ts||');
	});
});

describe('git store — branch checkout', () => {
	it('refreshes past an in-flight poll and ignores the poll when it lands late', async () => {
		await git.loadRepository('p', false, 's1');

		const stalePoll = deferred<GitStatus>();
		mocks.getGitStatus.mockReturnValueOnce(stalePoll.promise);
		const poll = git.refreshStatus();

		mocks.checkout.mockResolvedValue({ success: true, message: "Switched to 'feat/y'" });
		mocks.getGitStatus.mockResolvedValue(status({ current_branch: 'feat/y', modified: [] }));
		mocks.getBranches.mockResolvedValue([
			{ name: 'feat/x', current: false, remote: false },
			{ name: 'feat/y', current: true, remote: false }
		]);
		mocks.getPrState.mockResolvedValue({ ...prState(null), current_branch: 'feat/y' });
		const prCallsBefore = mocks.getPrState.mock.calls.length;

		const result = await git.checkout('feat/y');
		expect(result.success).toBe(true);
		expect(mocks.checkout).toHaveBeenCalledWith('p', 'feat/y', undefined, false, 's1');
		expect(git.current.status?.current_branch).toBe('feat/y');
		expect(git.current.branches.find((b) => b.current)?.name).toBe('feat/y');
		expect(git.current.loading).toBe(false);
		// The old branch's PR verdict is re-asked, not carried over.
		await vi.waitFor(() => expect(mocks.getPrState.mock.calls.length).toBe(prCallsBefore + 1));
		await vi.waitFor(() => expect(git.current.prState?.open_pr).toBeNull());

		stalePoll.resolve(status({ current_branch: 'feat/x' }));
		await poll;
		expect(git.current.status?.current_branch).toBe('feat/y');
	});

	it('surfaces a 409 (dirty tree) without touching the checked-out state', async () => {
		await git.loadRepository('p', false, 's1');
		const before = git.current.status;
		const branchCalls = mocks.getBranches.mock.calls.length;

		const conflict = { status: 409, detail: 'Your local changes would be overwritten by checkout' };
		mocks.checkout.mockRejectedValue(conflict);

		// The raw ApiError propagates so the caller can offer "Stash & switch".
		await expect(git.checkout('feat/y')).rejects.toBe(conflict);
		expect(git.current.error).toBe(conflict.detail);
		expect(git.current.loading).toBe(false);
		expect(git.current.status).toBe(before);
		expect(mocks.getBranches.mock.calls.length).toBe(branchCalls);

		mocks.checkout.mockResolvedValue({ success: true, message: 'stashed and switched' });
		await git.checkout('feat/y', true);
		expect(mocks.checkout).toHaveBeenLastCalledWith('p', 'feat/y', undefined, true, 's1');
		expect(git.current.error).toBeNull();
	});
});

describe('git store — worktree selection', () => {
	it('forgets canonical and legacy stale rows through the metadata-only API', async () => {
		await git.loadRepository('p', false, 's1');
		mocks.forgetWorktree.mockResolvedValue(undefined);
		mocks.getWorktrees.mockResolvedValue([]);
		await git.forgetWorktree('ck-stale');
		expect(mocks.forgetWorktree).toHaveBeenCalledWith('p', 'ck-stale');
		expect(mocks.deleteWorktree).not.toHaveBeenCalled();
		expect(git.current.worktrees).toEqual([]);
	});

	it('scopes every load call to the selected worktree and refuses to delete a session checkout', async () => {
		mocks.getWorktrees.mockResolvedValue([
			{ id: 'wt-1', branch_name: 'feat/x', resource_kind: 'worktree' },
			{ id: 'chk-1', branch_name: 'feat/z', resource_kind: 'checkout' }
		]);
		await git.loadForWorktree('p', 'wt-1', false, 's1');

		expect(git.current.mode).toBe('worktree');
		expect(git.current.worktreeId).toBe('wt-1');
		expect(mocks.getGitStatus).toHaveBeenCalledWith('p', 'wt-1', undefined, 's1');
		expect(mocks.getBranches).toHaveBeenCalledWith('p', true, 'wt-1', undefined, 's1');
		expect(mocks.getDiffStats).toHaveBeenCalledWith('p', true, 'wt-1', undefined, 's1');
		expect(mocks.getChangedFiles).toHaveBeenCalledWith('p', 'wt-1', 's1');

		await expect(git.deleteWorktree('chk-1')).rejects.toThrow(/session lifecycle/);
		expect(mocks.deleteWorktree).not.toHaveBeenCalled();

		mocks.deleteWorktree.mockResolvedValue(undefined);
		mocks.getWorktrees.mockResolvedValue([{ id: 'chk-1', branch_name: 'feat/z', resource_kind: 'checkout' }]);
		await git.deleteWorktree('wt-1', false);
		expect(mocks.deleteWorktree).toHaveBeenCalledWith('p', 'wt-1', false);
		expect(git.current.worktrees.map((w) => w.id)).toEqual(['chk-1']);
	});
});

describe('git store — slow background refresh', () => {
	it('asks only for PR state and repo info, never GitHub checks, and stops cleanly', async () => {
		vi.useFakeTimers();
		await git.loadRepository('p', false, 's1');
		const statusCalls = mocks.getGitStatus.mock.calls.length;
		const prCalls = mocks.getPrState.mock.calls.length;
		githubAccess.props.length = 0;

		git.startBackgroundRefresh();
		await vi.advanceTimersByTimeAsync(56_000); // one tick (45 s ± 10 s jitter)

		expect(mocks.getPrState.mock.calls.length).toBe(prCalls + 1);
		expect(githubAccess.ensureRepoInfo).toHaveBeenCalledWith('p');
		expect(new Set(githubAccess.props)).toEqual(new Set(['ensureRepoInfo']));
		// Local status is the fast poller's job, not this refresh's.
		expect(mocks.getGitStatus.mock.calls.length).toBe(statusCalls);

		git.stopBackgroundRefresh();
		await vi.advanceTimersByTimeAsync(10 * 60_000);
		expect(mocks.getPrState.mock.calls.length).toBe(prCalls + 1);
	});
});
