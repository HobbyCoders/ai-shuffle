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

function deferred() {
	let resolve!: () => void;
	const promise = new Promise<void>((res) => (resolve = res));
	return { promise, resolve };
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
		transformGitStatus({ is_git_repo: true, current_branch: 'feat/x', default_branch: 'main', modified: [], untracked: [] })
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
