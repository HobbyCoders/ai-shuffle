import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const mocks = vi.hoisted(() => ({
	getGitStatus: vi.fn(),
	getDiffStats: vi.fn(),
}));

vi.mock('$lib/api/git', () => ({
	getGitStatus: mocks.getGitStatus,
	getDiffStats: mocks.getDiffStats,
}));

import { REFRESH_MS, sessionGitState } from './session-git-state.svelte';

function status(over: Record<string, unknown> = {}) {
	return {
		is_git_repo: true,
		current_branch: 'feat/thing',
		is_detached: false,
		head_commit: 'abc',
		remote_url: null,
		is_clean: false,
		has_changes: true,
		staged: 0,
		modified: 2,
		untracked: 0,
		conflicts: 0,
		ahead: 0,
		behind: 0,
		differs_from_base: true,
		repository_id: null,
		default_branch: 'main',
		github_repo_name: null,
		files_fingerprint: 'x',
		...over,
	};
}

/** Let the store's in-flight promises settle without leaving fake time. */
const flush = () => vi.advanceTimersByTimeAsync(0);

beforeEach(() => {
	vi.useFakeTimers();
	vi.clearAllMocks();
	mocks.getGitStatus.mockResolvedValue(status());
	mocks.getDiffStats.mockResolvedValue({ additions: 12, deletions: 3, files_changed: 1 });
});

afterEach(() => {
	sessionGitState.reset();
	vi.useRealTimers();
});

describe('sessionGitState', () => {
	it('fetches the checkout once a card watches it', async () => {
		sessionGitState.watch('s1', 'proj-1', 'wt-1');
		await flush();

		expect(mocks.getGitStatus).toHaveBeenCalledTimes(1);
		expect(mocks.getGitStatus.mock.calls[0][0]).toBe('proj-1');
		expect(mocks.getGitStatus.mock.calls[0][1]).toBe('wt-1');
		expect(mocks.getGitStatus.mock.calls[0][3]).toBe('s1');
		expect(sessionGitState.get('s1')?.status?.has_changes).toBe(true);
		expect(sessionGitState.get('s1')?.diffStats?.additions).toBe(12);
	});

	it('dedupes: two cards on one session share a single poll', async () => {
		sessionGitState.watch('s1', 'proj-1');
		sessionGitState.watch('s1', 'proj-1');
		await flush();

		expect(mocks.getGitStatus).toHaveBeenCalledTimes(1);
	});

	it('never fetches for a session with no project', async () => {
		const release = sessionGitState.watch('chat-1', null);
		await flush();

		expect(mocks.getGitStatus).not.toHaveBeenCalled();
		expect(sessionGitState.get('chat-1')).toBeNull();
		expect(() => release()).not.toThrow();
	});

	it('refreshes on the slow interval while a card stays mounted', async () => {
		sessionGitState.watch('s1', 'proj-1');
		await flush();
		expect(mocks.getGitStatus).toHaveBeenCalledTimes(1);

		await vi.advanceTimersByTimeAsync(REFRESH_MS);
		expect(mocks.getGitStatus).toHaveBeenCalledTimes(2);
	});

	it('stops polling once the last watcher releases', async () => {
		const first = sessionGitState.watch('s1', 'proj-1');
		const second = sessionGitState.watch('s1', 'proj-1');
		await flush();

		first();
		await vi.advanceTimersByTimeAsync(REFRESH_MS);
		expect(mocks.getGitStatus).toHaveBeenCalledTimes(2);

		second();
		await vi.advanceTimersByTimeAsync(REFRESH_MS * 3);
		expect(mocks.getGitStatus).toHaveBeenCalledTimes(2);
	});

	it('aborts the in-flight request when the card unmounts', async () => {
		let signal: AbortSignal | undefined;
		mocks.getGitStatus.mockImplementation((_p, _w, options) => {
			signal = options?.signal;
			return new Promise(() => {});
		});

		const release = sessionGitState.watch('s1', 'proj-1');
		await flush();
		expect(signal?.aborted).toBe(false);

		release();
		expect(signal?.aborted).toBe(true);
	});

	it('skips the diffstat request on a clean tree', async () => {
		mocks.getGitStatus.mockResolvedValue(status({ is_clean: true, has_changes: false, modified: 0 }));

		sessionGitState.watch('s1', 'proj-1');
		await flush();

		expect(mocks.getDiffStats).not.toHaveBeenCalled();
		expect(sessionGitState.get('s1')?.diffStats).toBeNull();
	});

	it('settles quietly on an API error', async () => {
		mocks.getGitStatus.mockRejectedValue(new Error('offline'));

		sessionGitState.watch('s1', 'proj-1');
		await flush();

		expect(sessionGitState.get('s1')).toEqual({ status: null, diffStats: null, loaded: true });
	});

	it('drops the cached answer when the session moves to another checkout', async () => {
		const release = sessionGitState.watch('s1', 'proj-1', 'wt-1');
		await flush();
		release();

		sessionGitState.watch('s1', 'proj-1', 'wt-2');
		expect(sessionGitState.get('s1')).toBeNull();
		await flush();
		expect(mocks.getGitStatus).toHaveBeenLastCalledWith('proj-1', 'wt-2', expect.anything(), 's1');
	});
});
