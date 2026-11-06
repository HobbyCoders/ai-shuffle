/**
 * Chat-open request budget — the CI ratchet for the Git fan-out.
 *
 * Opening an existing chat used to fire seven Git requests alongside the
 * transcript load (status, branches, commit graph, worktree list, diff stats,
 * changed files, PR state), then a remote fetch. Only the header's inputs may
 * go out before the transcript is on screen; the PR lookup and the remote
 * fetch wait for it, and the graph and worktree list are not loaded for an
 * existing chat at all. The API module is mocked; the store runs for real.
 */

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { writable } from 'svelte/store';

const mocks = vi.hoisted(() => ({
	getGitStatus: vi.fn(),
	getPrState: vi.fn(),
	getBranches: vi.fn(),
	getCommitGraph: vi.fn(),
	getWorktrees: vi.fn(),
	getDiffStats: vi.fn(),
	getChangedFiles: vi.fn(),
	fetchRemote: vi.fn()
}));

vi.mock('$lib/api/git', async (importOriginal) => {
	const actual = await importOriginal<typeof import('$lib/api/git')>();
	return { ...actual, ...mocks };
});

vi.mock('./github.svelte', () => ({
	github: new Proxy({}, { get: () => vi.fn().mockResolvedValue(undefined) })
}));

import { transformGitStatus, type PrState } from '$lib/api/git';
import { git, whenTranscriptSettled } from './git.svelte';

const HEADER_REQUESTS = ['getBranches', 'getChangedFiles', 'getDiffStats', 'getGitStatus'];

function issued(): string[] {
	return Object.entries(mocks)
		.filter(([, m]) => m.mock.calls.length > 0)
		.map(([name]) => name)
		.sort();
}

function requestCount(): number {
	return Object.values(mocks).reduce((n, m) => n + m.mock.calls.length, 0);
}

function prState(number = 7): PrState {
	return {
		current_branch: 'feat/x',
		has_github: true,
		open_pr: { number, title: 'PR', url: `https://x/pull/${number}`, draft: false, state: 'OPEN', mergeable: null },
		lookup_failed: false,
		served_stale: false
	};
}

function deferred<T = void>() {
	let resolve!: (value: T) => void;
	const promise = new Promise<T>((res) => (resolve = res));
	return { promise, resolve };
}

function checkoutStatus(blocked: boolean, branch = 'feat/x') {
	return transformGitStatus({ is_git_repo: true, current_branch: branch, mutations_blocked: blocked });
}

/** Let every already-settled promise in the load chain run. */
async function settle() {
	for (let i = 0; i < 20; i++) await Promise.resolve();
}

beforeEach(() => {
	for (const m of Object.values(mocks)) m.mockReset();
	git.clear();
	vi.spyOn(console, 'warn').mockImplementation(() => {});
	mocks.getGitStatus.mockResolvedValue(
		transformGitStatus({ is_git_repo: true, current_branch: 'feat/x', default_branch: 'main', modified: [], untracked: [], mutations_blocked: false })
	);
	mocks.getBranches.mockResolvedValue([{ name: 'feat/x', current: true, remote: false }]);
	mocks.getCommitGraph.mockResolvedValue({ commits: [] });
	mocks.getWorktrees.mockResolvedValue([]);
	mocks.getDiffStats.mockResolvedValue({ additions: 0, deletions: 0, files_changed: 0 });
	mocks.getChangedFiles.mockResolvedValue({ files: [] });
	mocks.getPrState.mockResolvedValue(prState());
	mocks.fetchRemote.mockResolvedValue(undefined);
});

afterEach(() => {
	vi.unstubAllEnvs();
	vi.useRealTimers();
	vi.restoreAllMocks();
});

describe('opening an existing chat', () => {
	it.each([true, undefined])('does not auto-fetch when owner checkout activity is blocked/unknown (%s)', async (blocked) => {
		mocks.getGitStatus.mockResolvedValue(transformGitStatus({
			is_git_repo: true, current_branch: 'feat/x', mutations_blocked: blocked
		}));
		await git.loadRepository('p', false, 's1');
		await settle();
		expect(mocks.fetchRemote).not.toHaveBeenCalled();
		// Retry/reopen must not keep issuing a mutation the backend will refuse.
		await git.loadRepository('p', true, 's1');
		await settle();
		expect(mocks.fetchRemote).not.toHaveBeenCalled();
	});

	it('does not auto-fetch while a turn may be using the checkout (#803)', async () => {
		// A new chat's first load runs as its first reply starts: its status can
		// still say idle, but the owner refuses the fetch (409) once the turn runs.
		let busy = true;
		await git.loadRepository('p', false, 's1', { fetchBlocked: () => busy });
		await settle();
		expect(mocks.fetchRemote).not.toHaveBeenCalled();
		busy = false;
		await git.loadRepository('p', true, 's1', { fetchBlocked: () => busy });
		await settle();
		expect(mocks.fetchRemote).toHaveBeenCalledTimes(1);
	});

	it('runs the skipped fetch once the turn ends (owner-status refresh)', async () => {
		let busy = true;
		await git.loadRepository('p', false, 's1', { fetchBlocked: () => busy });
		await settle();
		expect(mocks.fetchRemote).not.toHaveBeenCalled();
		busy = false;
		git.forceRefresh();
		await settle();
		expect(mocks.fetchRemote).toHaveBeenCalledTimes(1);
		// Only once: a later stream end does not fetch again.
		git.forceRefresh();
		await settle();
		expect(mocks.fetchRemote).toHaveBeenCalledTimes(1);
	});

	it("does not release a parked chat's fetch when navigation clears its busy fields", async () => {
		let busy = true;
		await git.loadRepository('p', false, 's1', { fetchBlocked: () => busy });
		await settle();
		const key = git.current.contextKey;

		// MainPanel restores the sibling's shared cache synchronously, before
		// its debounced load. Switching clears the active chat's parked/tasks.
		git.restoreCacheImmediate('p', undefined, 'project', 's2', 'feat/x');
		expect(git.current.contextKey).toBe(key);
		busy = false;
		const owner = deferred<ReturnType<typeof checkoutStatus>>();
		mocks.getGitStatus.mockReturnValueOnce(owner.promise);
		git.forceRefresh(); // The busy -> idle effect also runs on navigation.
		await settle();
		expect(mocks.fetchRemote).not.toHaveBeenCalled();
		expect(mocks.getGitStatus).toHaveBeenCalledWith('p', undefined, expect.anything(), 's2');

		owner.resolve(checkoutStatus(true)); // A's job still owns this checkout.
		await settle();
		expect(mocks.fetchRemote).not.toHaveBeenCalled();

		// Neither the blocked refresh nor the chat switch consumes the retry.
		mocks.getGitStatus.mockResolvedValue(checkoutStatus(false));
		git.forceRefresh();
		await settle();
		expect(mocks.fetchRemote).toHaveBeenCalledExactlyOnceWith('p', undefined, 's2');
		git.forceRefresh();
		await settle();
		expect(mocks.fetchRemote).toHaveBeenCalledTimes(1);
	});

	it.each(['project', 'worktree'])('retries opening an already-busy %s once owner polling says idle', async (mode) => {
		mocks.getGitStatus.mockResolvedValue(checkoutStatus(true));
		const worktreeId = mode === 'worktree' ? 'wt1' : undefined;
		if (worktreeId) await git.loadForWorktree('p', worktreeId, false, 's1');
		else await git.loadRepository('p', false, 's1');
		await settle();
		expect(mocks.fetchRemote).not.toHaveBeenCalled();
		await git.refreshStatus();
		expect(mocks.fetchRemote).not.toHaveBeenCalled();

		mocks.getGitStatus.mockResolvedValue(checkoutStatus(false));
		await git.refreshStatus(); // Read-only polling must release the retry too.
		await settle();
		expect(mocks.fetchRemote).toHaveBeenCalledExactlyOnceWith('p', worktreeId, 's1');
		git.forceRefresh();
		await settle();
		expect(mocks.fetchRemote).toHaveBeenCalledTimes(1);
	});

	it('keeps the live busy gate even when an unchanged owner status says idle', async () => {
		let busy = true;
		await git.loadRepository('p', false, 's1', { fetchBlocked: () => busy });
		await settle();
		git.forceRefresh();
		await settle();
		expect(mocks.fetchRemote).not.toHaveBeenCalled();

		busy = false;
		// Owner status is unchanged: no-op write suppression must not skip retry.
		await git.refreshStatus();
		await settle();
		expect(mocks.fetchRemote).toHaveBeenCalledTimes(1);
	});

	it("drops a different checkout's deferral and rekeys the pending fetch to the new target", async () => {
		await git.loadRepository('p', false, 's1', { fetchBlocked: () => true });
		await settle();
		const oldKey = git.current.contextKey;
		mocks.getGitStatus.mockResolvedValue(checkoutStatus(true, 'feat/y'));
		git.restoreCacheImmediate('p', undefined, 'project', 's2', 'feat/y');
		expect(git.current.contextKey).not.toBe(oldKey);
		git.forceRefresh();
		await settle();
		expect(mocks.fetchRemote).not.toHaveBeenCalled();

		await git.loadRepository('p', false, 's2');
		mocks.getGitStatus.mockResolvedValue(checkoutStatus(false, 'feat/y'));
		await git.refreshStatus();
		await settle();
		expect(mocks.fetchRemote).toHaveBeenCalledExactlyOnceWith('p', undefined, 's2');
		await git.refreshStatus();
		await settle();
		expect(mocks.fetchRemote).toHaveBeenCalledTimes(1);
	});

	it.each(['project', 'worktree'])("ignores a previous chat's idle poll on the same %s checkout", async (mode) => {
		const worktreeId = mode === 'worktree' ? 'wt1' : undefined;
		let busy = true;
		const options = { fetchBlocked: () => busy };
		if (worktreeId) await git.loadForWorktree('p', worktreeId, false, 's1', options);
		else await git.loadRepository('p', false, 's1', options);
		await settle();
		const previousOwner = deferred<ReturnType<typeof checkoutStatus>>();
		mocks.getGitStatus.mockReturnValueOnce(previousOwner.promise);
		const refresh = git.refreshStatus();

		git.restoreCacheImmediate('p', worktreeId, worktreeId ? 'worktree' : 'project', 's2', 'feat/x');
		busy = false;
		previousOwner.resolve(checkoutStatus(false));
		await refresh;
		await settle();
		expect(mocks.fetchRemote).not.toHaveBeenCalled();

		mocks.getGitStatus.mockResolvedValue(checkoutStatus(true));
		if (worktreeId) await git.loadForWorktree('p', worktreeId, false, 's2');
		else await git.loadRepository('p', false, 's2');
		await git.refreshStatus();
		expect(mocks.fetchRemote).not.toHaveBeenCalled();
		mocks.getGitStatus.mockResolvedValue(checkoutStatus(false));
		await git.refreshStatus();
		await settle();
		expect(mocks.fetchRemote).toHaveBeenCalledExactlyOnceWith('p', worktreeId, 's2');
	});

	it.each(['project', 'worktree'])("does not release a departed chat's %s load after its transcript gate", async (mode) => {
		const worktreeId = mode === 'worktree' ? 'wt1' : undefined;
		let busy = true;
		const transcript = deferred();
		const options = { fetchBlocked: () => busy, afterCriticalPath: transcript.promise };
		const load = worktreeId
			? git.loadForWorktree('p', worktreeId, false, 's1', options)
			: git.loadRepository('p', false, 's1', options);
		await settle(); // A's idle status was read before its work parked.
		git.restoreCacheImmediate('p', worktreeId, worktreeId ? 'worktree' : 'project', 's2', 'feat/x');
		busy = false;
		transcript.resolve(); // Leaving A also settles its transcript gate.
		await load;
		await settle();
		expect(mocks.fetchRemote).not.toHaveBeenCalled();
		expect(git.current.sessionId).toBe('s2');

		mocks.getGitStatus.mockResolvedValue(checkoutStatus(true));
		if (worktreeId) await git.loadForWorktree('p', worktreeId, false, 's2');
		else await git.loadRepository('p', false, 's2');
		await settle();
		expect(mocks.fetchRemote).not.toHaveBeenCalled();
		mocks.getGitStatus.mockResolvedValue(checkoutStatus(false));
		await git.refreshStatus();
		await settle();
		expect(mocks.fetchRemote).toHaveBeenCalledExactlyOnceWith('p', worktreeId, 's2');
	});

	it('does not release a deferral from an aborted idle poll', async () => {
		let busy = true;
		await git.loadRepository('p', false, 's1', { fetchBlocked: () => busy });
		await settle();
		busy = false;
		const oldOwner = deferred<ReturnType<typeof checkoutStatus>>();
		mocks.getGitStatus.mockReturnValueOnce(oldOwner.promise);
		const oldRefresh = git.refreshStatus();
		mocks.getGitStatus.mockResolvedValue(checkoutStatus(true));
		git.forceRefresh(); // Cancels the old idle poll, then reads busy status.
		oldOwner.resolve(checkoutStatus(false));
		await oldRefresh;
		await settle();
		expect(mocks.fetchRemote).not.toHaveBeenCalled();

		mocks.getGitStatus.mockResolvedValue(checkoutStatus(false));
		await git.refreshStatus();
		await settle();
		expect(mocks.fetchRemote).toHaveBeenCalledTimes(1);
	});

	it.each([409, 503])('does not auto-fetch an unavailable checkout (%s)', async (status) => {
		mocks.getGitStatus.mockRejectedValue({ status, detail: 'Checkout unavailable' });
		vi.spyOn(console, 'error').mockImplementation(() => {});
		await git.loadRepository('p', false, 's1');
		await settle();
		expect(mocks.fetchRemote).not.toHaveBeenCalled();
		expect(git.current.error).toBe('Checkout unavailable');
	});

	it('issues only the header requests before the transcript is on screen', async () => {
		const transcript = deferred();
		const load = git.loadRepository('p', false, 's1', { afterCriticalPath: transcript.promise });
		await settle();

		expect(issued()).toEqual(HEADER_REQUESTS);
		expect(requestCount()).toBe(4);
		// The header keeps its placeholder until the PR verdict is in: one
		// coherent write, never an intermediate primary action.
		expect(git.current.hydrated).toBe(false);

		transcript.resolve();
		await load;
		await settle();

		expect(mocks.getPrState).toHaveBeenCalledTimes(1);
		expect(mocks.fetchRemote).toHaveBeenCalledTimes(1);
		expect(git.current.hydrated).toBe(true);
		expect(git.current.prState?.open_pr?.number).toBe(7);
		// Nothing on an existing chat renders these.
		expect(mocks.getCommitGraph).not.toHaveBeenCalled();
		expect(mocks.getWorktrees).not.toHaveBeenCalled();
	});

	it('drops the PR lookup of a chat the user already left', async () => {
		const transcript = deferred();
		const first = git.loadRepository('p', false, 's1', { afterCriticalPath: transcript.promise });
		await settle();
		await git.loadRepository('p', false, 's2');
		expect(mocks.getPrState).toHaveBeenCalledTimes(1);

		transcript.resolve();
		await first;
		expect(mocks.getPrState).toHaveBeenCalledTimes(1);
		expect(git.current.sessionId).toBe('s2');
	});

	it('still lists worktrees for the new-chat branch picker', async () => {
		await git.loadRepository('p');
		expect(mocks.getWorktrees).toHaveBeenCalledTimes(1);
		expect(mocks.getCommitGraph).not.toHaveBeenCalled();
	});

	it('restores the full fan-out when the kill switch is off', async () => {
		vi.stubEnv('VITE_PERF_DEFER_GIT_ON_OPEN', '0');
		const transcript = deferred();
		const load = git.loadRepository('p', false, 's1', { afterCriticalPath: transcript.promise });
		await settle();
		// The pre-change fan-out; the remote fetch follows once it lands.
		expect(issued().filter((name) => name !== 'fetchRemote')).toEqual(
			['getBranches', 'getChangedFiles', 'getCommitGraph', 'getDiffStats', 'getGitStatus', 'getPrState', 'getWorktrees']
		);
		transcript.resolve();
		await load;
	});
});

describe('whenTranscriptSettled', () => {
	function source(sessionId: string | null, isLoadingSession: boolean) {
		return writable({ sessionId, isLoadingSession });
	}

	it('resolves when the transcript finishes loading', async () => {
		const store = source('s1', true);
		let done = false;
		void whenTranscriptSettled(store, 's1', 60_000).then(() => (done = true));
		await settle();
		expect(done).toBe(false);
		store.set({ sessionId: 's1', isLoadingSession: false });
		await settle();
		expect(done).toBe(true);
	});

	it('resolves at once for a transcript that is already loaded', async () => {
		let done = false;
		void whenTranscriptSettled(source('s1', false), 's1', 60_000).then(() => (done = true));
		await settle();
		expect(done).toBe(true);
	});

	it('resolves when the user moves to another chat', async () => {
		const store = source('s1', true);
		let done = false;
		void whenTranscriptSettled(store, 's1', 60_000).then(() => (done = true));
		store.set({ sessionId: 's2', isLoadingSession: true });
		await settle();
		expect(done).toBe(true);
	});

	it('gives up waiting after the cap', async () => {
		vi.useFakeTimers();
		let done = false;
		void whenTranscriptSettled(source('s1', true), 's1', 2_000).then(() => (done = true));
		await vi.advanceTimersByTimeAsync(1_999);
		expect(done).toBe(false);
		await vi.advanceTimersByTimeAsync(1);
		expect(done).toBe(true);
	});
});
