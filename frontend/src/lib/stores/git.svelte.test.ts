/**
 * Tests for the git store's state-coherence guarantees.
 *
 * Covered here (docs/PERFORMANCE_REVIEW_2026-08.md §3.3):
 *
 * - **G4 / cleaned-up worktree**: when the backend reports the worktree is gone
 *   (404), the store fails closed — drops any stale `is_git_repo: true` and
 *   surfaces a definitive no-repo status so the header hides Ship It instead of
 *   acting on a folder that no longer has a repo.
 * - **G4 / checkout-keyed cache**: two sessions resolving to the same checkout
 *   share one cache entry, so a switch never shows an empty header; an unknown
 *   context keeps the last-known values as a stale placeholder rather than
 *   blanking.
 * - **G4 / request versioning**: a slow in-flight response for a key never
 *   overwrites a newer one for the same key.
 * - **G6**: inline mutation payloads are applied, with no redundant refetches.
 * - **G7**: an unchanged status poll genuinely notifies nobody.
 * - **G10c**: a single GitHub-auth failure does not flip GitHub availability,
 *   and it never touches local repository state.
 *
 * The repo's convention is to mock the API module (see gitCheckoutAction.test.ts).
 */

import { describe, it, expect, vi, beforeEach } from 'vitest';

const mocks = vi.hoisted(() => ({
	getGitStatus: vi.fn(),
	getPrState: vi.fn(),
	getBranches: vi.fn(),
	getCommitGraph: vi.fn(),
	getWorktrees: vi.fn(),
	getDiffStats: vi.fn(),
	getChangedFiles: vi.fn(),
	fetchRemote: vi.fn(),
	commitChanges: vi.fn(),
	pushCommits: vi.fn(),
	pullFromRemote: vi.fn(),
	stageFiles: vi.fn()
}));

vi.mock('$lib/api/git', async (importOriginal) => {
	const actual = await importOriginal<typeof import('$lib/api/git')>();
	return { ...actual, ...mocks };
});

import { transformGitStatus, type GitStatus, type PrState } from '$lib/api/git';
import { git } from './git.svelte';

/** A canonical status, freshly constructed each call — reference equality can
 *  never accidentally make two of these "equal". */
function status(overrides: Record<string, unknown> = {}): GitStatus {
	return transformGitStatus({
		is_git_repo: true,
		current_branch: 'feat/x',
		default_branch: 'main',
		repository_id: 'repo-1',
		modified: ['a.ts', 'b.ts'],
		untracked: ['c.ts'],
		ahead: 1,
		differs_from_base: true,
		...overrides
	});
}

function prState(number: number | null = 7, extra: Partial<PrState> = {}): PrState {
	return {
		current_branch: 'feat/x',
		has_github: true,
		open_pr: number === null
			? null
			: { number, title: 'Do a thing', url: `https://x/pull/${number}`, draft: false, state: 'OPEN', mergeable: null },
		lookup_failed: false,
		served_stale: false,
		...extra
	};
}

function deferred<T>() {
	let resolve!: (v: T) => void;
	const promise = new Promise<T>((r) => { resolve = r; });
	return { promise, resolve };
}

beforeEach(() => {
	for (const m of Object.values(mocks)) m.mockReset();
	git.clear();
	vi.spyOn(console, 'warn').mockImplementation(() => {});
	vi.spyOn(console, 'error').mockImplementation(() => {});
	vi.spyOn(console, 'debug').mockImplementation(() => {});

	mocks.getGitStatus.mockResolvedValue(status());
	mocks.getBranches.mockResolvedValue([{ name: 'feat/x', current: true, remote: false }]);
	mocks.getCommitGraph.mockResolvedValue({ commits: [] });
	mocks.getWorktrees.mockResolvedValue([]);
	mocks.getDiffStats.mockResolvedValue({ additions: 3, deletions: 1, files_changed: 2 });
	mocks.getChangedFiles.mockResolvedValue({ files: [] });
	mocks.getPrState.mockResolvedValue(prState());
	// Background remote fetch is fire-and-forget; keep it inert.
	mocks.fetchRemote.mockRejectedValue(new Error('no remote'));
});

describe('git store — selected worker without a session', () => {
	it('loads canonical worktrees for the local worker scope', async () => {
		const target = { workerId: 'local', branch: 'feature/native' };
		git.setWorkerTarget(target);
		await git.loadRepository('canonical-project');
		expect(mocks.getGitStatus).toHaveBeenLastCalledWith('canonical-project', undefined, undefined, target);
		expect(mocks.getWorktrees).toHaveBeenCalledWith('canonical-project', undefined);
	});
	it('names the open worktree so the server keeps its status fresh', async () => {
		await git.loadForWorktree('proj-1', 'wt-open');
		expect(mocks.getWorktrees).toHaveBeenLastCalledWith('proj-1', 'wt-open');
		await git.loadWorktrees();
		expect(mocks.getWorktrees).toHaveBeenLastCalledWith('proj-1', 'wt-open');
	});
	it('targets both status and pull, isolating same-branch caches by worker', async () => {
		const a = { workerId: 'worker-a', branch: 'main' };
		const b = { workerId: 'worker-b', branch: 'main' };
		git.setWorkerTarget(a);
		mocks.getGitStatus.mockResolvedValue(status({ current_branch: 'main', behind: 0 }));
		await git.loadRepository('canonical-project');
		git.setWorkerTarget(b);
		mocks.getGitStatus.mockResolvedValue(status({ current_branch: 'main', behind: 1 }));
		await git.loadRepository('canonical-project');
		expect(mocks.getGitStatus).toHaveBeenLastCalledWith('canonical-project', undefined, undefined, b);
		expect(git.current.status?.behind).toBe(1);
		const target = git.captureTarget();
		const pending = deferred<any>();
		mocks.pullFromRemote.mockReturnValue(pending.promise);
		const pull = git.pull('merge', false, target);
		expect(mocks.pullFromRemote).toHaveBeenCalledWith('canonical-project', { strategy: 'merge', auto_stash: false }, undefined, b);
		git.setWorkerTarget(a);
		git.restoreCacheImmediate('canonical-project');
		expect(git.current.status?.behind).toBe(0);
		mocks.getGitStatus.mockResolvedValue(status({ current_branch: 'main', behind: 0 }));
		await git.loadRepository('canonical-project');
		expect(git.current.status?.behind).toBe(0);
		pending.resolve({ status: status({ behind: 5 }), diff_stats: null });
		await pull;
		expect(git.current.status?.behind).toBe(0);
		expect(mocks.getWorktrees).not.toHaveBeenCalled();
	});
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
		// A POSITIVE finding, not a switch placeholder — the header must render it.
		expect(git.current.hydrated).toBe(true);
		expect(git.current.stale).toBe(false);
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

describe('git store — optional worktree list', () => {
	it.each([404, 500])('keeps resumed session Git usable when listing fails (%s)', async (code) => {
		mocks.getWorktrees.mockRejectedValue({ status: code, detail: 'Project not found' });
		await git.loadRepository('pv2-only', false, 'resumed-here');
		expect(git.current.error).toBeNull();
		expect(git.current.hydrated).toBe(true);
		expect(git.current.stale).toBe(false);
		expect(git.current.status?.is_git_repo).toBe(true);
		expect(git.current.prState?.open_pr?.number).toBe(7);
		expect(git.current.worktrees).toEqual([]);
		await git.loadWorktrees();
		expect(git.current.error).toBeNull();
		expect(git.current.status?.current_branch).toBe('feat/x');
	});
});

describe('git store — initial owner failure recovery', () => {
	it.each([null, 'wt-remote'])('hydrates the complete header on the next poll (%s)', async (worktreeId) => {
		git.setRemoteSession('remote-session', true);
		mocks.getGitStatus.mockRejectedValueOnce({ status: 503, detail: 'token rejected' });
		if (worktreeId) await git.loadForWorktree('p1', worktreeId, false, 'remote-session');
		else await git.loadRepository('p1', false, 'remote-session');
		expect(git.current.hydrated).toBe(false);

		await git.refreshStatus();

		expect(git.current.hydrated).toBe(true);
		expect(git.current.error).toBeNull();
		expect(git.current.prState?.open_pr?.number).toBe(7);
		expect(git.current.branches).toHaveLength(1);
		expect(git.current.sessionId).toBe('remote-session');
		expect(mocks.getPrState).toHaveBeenCalledWith('p1', worktreeId ?? undefined, 'remote-session');
		expect(mocks.getWorktrees).not.toHaveBeenCalled();
	});
});

describe('git store — checkout-keyed cache (G4)', () => {
	it('keeps a remote checkout separate from the same local project and branch', async () => {
		await git.loadRepository('p1', false, 'local-session');
		const localKey = git.current.contextKey;
		git.setRemoteSession('remote-session', true);
		git.restoreCacheImmediate('p1', null, 'project', 'remote-session', 'feat/x');
		expect(git.current.contextKey).not.toBe(localKey);
		expect(git.current.hydrated).toBe(false);

		mocks.getPrState.mockResolvedValue(prState(99));
		mocks.getWorktrees.mockClear();
		await git.loadRepository('p1', false, 'remote-session');
		expect(git.current.prState?.open_pr?.number).toBe(99);
		expect(mocks.getGitStatus).toHaveBeenLastCalledWith('p1', undefined, undefined, 'remote-session');
		expect(mocks.getWorktrees).not.toHaveBeenCalled();

		git.restoreCacheImmediate('p1', null, 'project', 'local-session', 'feat/x');
		expect(git.current.contextKey).toBe(localKey);
		expect(git.current.prState?.open_pr?.number).toBe(7);
	});

	it('retries an unavailable owner using the same remote session', async () => {
		git.setRemoteSession('offline-session', true);
		mocks.getGitStatus.mockRejectedValueOnce({ status: 503, detail: 'Owning worker is unreachable' });
		await git.loadRepository('p1', false, 'offline-session');
		expect(git.current.error).toContain('Owning worker');
		await git.retryLoad();
		expect(mocks.getGitStatus).toHaveBeenLastCalledWith('p1', undefined, undefined, 'offline-session');
		expect(git.current.error).toBeNull();
		expect(git.current.hydrated).toBe(true);
	});

	it('two sessions on the same checkout share state — a switch is never empty', async () => {
		await git.loadRepository('p1', false, 'ses-1');
		expect(git.current.status?.current_branch).toBe('feat/x');
		expect(git.current.hydrated).toBe(true);

		// Switch to a DIFFERENT session on the same branch (⇒ same checkout dir).
		// With the branch hint the identity resolves in the same frame.
		git.restoreCacheImmediate('p1', null, 'project', 'ses-2', 'feat/x');

		expect(git.current.sessionId).toBe('ses-2');
		// Real data for this checkout — not an empty flash.
		expect(git.current.status?.current_branch).toBe('feat/x');
		expect(git.current.branches.length).toBe(1);
		expect(git.current.prState?.open_pr?.number).toBe(7);
		expect(git.current.hydrated).toBe(true);
		expect(git.current.stale).toBe(true); // revalidating
	});

	it('learns the checkout identity from the status response (no hint needed)', async () => {
		await git.loadRepository('p1', false, 'ses-1');
		// ses-2 loads and lands on the same identity...
		await git.loadRepository('p1', false, 'ses-2');
		// ...so a later switch back to ses-1 is an instant, hydrated cache hit.
		git.restoreCacheImmediate('p1', null, 'project', 'ses-1');
		expect(git.current.hydrated).toBe(true);
		expect(git.current.status?.current_branch).toBe('feat/x');
	});

	it('an unknown context keeps the last-known values as a stale placeholder', async () => {
		await git.loadRepository('p1', false, 'ses-1');

		// A session we have never loaded, on an unknown branch.
		git.restoreCacheImmediate('p1', null, 'project', 'ses-new');

		// NEVER reset to empty: the previous real values stay on screen…
		expect(git.current.status?.current_branch).toBe('feat/x');
		expect(git.current.branches.length).toBe(1);
		// …but they are flagged as not describing this context yet.
		expect(git.current.hydrated).toBe(false);
		expect(git.current.stale).toBe(true);
	});
});

describe('git store — request versioning (G4)', () => {
	it('keeps a saved commit/push flow on its captured owner after switching sessions', async () => {
		git.setRemoteSession('remote-commit', true);
		await git.loadRepository('p1', false, 'remote-commit');
		const target = git.captureTarget();
		await git.loadRepository('p2', false, 'local-other');
		const currentKey = git.current.contextKey;
		mocks.commitChanges.mockResolvedValue({ success: true, status: status({ current_branch: 'remote-branch' }) });
		mocks.pushCommits.mockResolvedValue({ success: true, status: status({ current_branch: 'remote-branch' }) });

		await git.commitChanges('Saved message', false, target);
		await git.pushCommits(undefined, true, false, target);
		expect(mocks.commitChanges).toHaveBeenCalledWith('p1', { message: 'Saved message', include_unstaged: false }, undefined, 'remote-commit');
		expect(mocks.pushCommits).toHaveBeenCalledWith('p1', { branch: undefined, set_upstream: true, force: false }, undefined, 'remote-commit');
		expect(git.current.contextKey).toBe(currentKey);
		expect(git.current.status?.current_branch).toBe('feat/x');
	});

	it('a slow in-flight response never overwrites a newer one for the same key', async () => {
		await git.loadRepository('p1', false, 'ses-1');

		const gate = deferred<GitStatus>();
		mocks.getGitStatus.mockImplementationOnce(() => gate.promise);
		const slow = git.refreshStatus();

		// A commit/push clears the in-flight guards (as the real flow does), so a
		// second status request can legitimately overlap the first.
		git.setOperationInProgress(true);
		git.setOperationInProgress(false);

		mocks.getGitStatus.mockResolvedValue(status({ ahead: 9 }));
		await git.refreshStatus();
		expect(git.current.status?.ahead).toBe(9);

		// The stale answer finally arrives — and must lose.
		gate.resolve(status({ ahead: 1 }));
		await slow;
		expect(git.current.status?.ahead).toBe(9);
	});
});

describe('git store — equality suppression (G7)', () => {
	it('an unchanged status poll notifies nobody', async () => {
		await git.loadRepository('p1', false, 'ses-1');

		let notifications = 0;
		const unsubscribe = git.subscribe(() => { notifications += 1; });
		notifications = 0; // subscribe() fires once synchronously

		// Same working tree, brand-new objects and arrays every time — exactly what
		// the backend sends on every 2 s poll.
		await git.refreshStatus();
		await git.refreshStatus();
		await git.refreshStatus();
		expect(notifications).toBe(0);

		// A real change still lands.
		mocks.getGitStatus.mockResolvedValue(status({ modified: ['a.ts', 'b.ts', 'd.ts'] }));
		await git.refreshStatus();
		expect(notifications).toBe(1);

		unsubscribe();
	});

	it('detects a same-size file-list change via the fingerprint', async () => {
		await git.loadRepository('p1', false, 'ses-1');
		expect(git.current.status?.modified).toBe(2);

		// Same COUNTS, different files — counts alone would miss this.
		mocks.getGitStatus.mockResolvedValue(status({ modified: ['a.ts', 'zz.ts'] }));
		let notifications = 0;
		const unsubscribe = git.subscribe(() => { notifications += 1; });
		notifications = 0;
		await git.refreshStatus();
		expect(notifications).toBe(1);
		unsubscribe();
	});
});

describe('git store — inline mutation payloads (G6)', () => {
	it('applies the commit response instead of refetching status + diff', async () => {
		await git.loadRepository('p1', false, 'ses-1');
		mocks.getGitStatus.mockClear();
		mocks.getDiffStats.mockClear();
		mocks.getBranches.mockClear();

		mocks.commitChanges.mockResolvedValue({
			success: true,
			message: 'Created commit abc',
			commit_sha: 'abc',
			// The backend's inline status: no repository record on it.
			status: transformGitStatus({
				is_git_repo: true,
				current_branch: 'feat/x',
				ahead: 2,
				differs_from_base: true
			}),
			diff_stats: { additions: 0, deletions: 0, files_changed: 0 }
		});

		await git.commitChanges('msg');

		// No redundant round trips…
		expect(mocks.getGitStatus).not.toHaveBeenCalled();
		expect(mocks.getDiffStats).not.toHaveBeenCalled();
		// …except the ONE field the inline payload can't cover: upstream
		// ahead/behind, which drives the Push badge.
		expect(mocks.getBranches).toHaveBeenCalledTimes(1);

		// State reflects the commit immediately — no stale-display window.
		expect(git.current.status?.ahead).toBe(2);
		expect(git.current.status?.has_changes).toBe(false);
		expect(git.current.diffStats).toEqual({ additions: 0, deletions: 0, files_changed: 0 });
		// Repository-record fields survive an inline payload that lacks them.
		expect(git.current.status?.default_branch).toBe('main');
		expect(git.current.status?.repository_id).toBe('repo-1');
	});

	it('applies the stage response and reconciles only the changed-files list', async () => {
		await git.loadRepository('p1', false, 'ses-1');
		mocks.getGitStatus.mockClear();
		mocks.getDiffStats.mockClear();
		mocks.getChangedFiles.mockClear();

		mocks.stageFiles.mockResolvedValue({
			success: true,
			message: 'Staged 1 file(s)',
			status: transformGitStatus({
				is_git_repo: true,
				current_branch: 'feat/x',
				staged: [{ file: 'a.ts', status: 'M' }]
			}),
			diff_stats: { additions: 1, deletions: 0, files_changed: 1 }
		});

		await git.stageFiles(['a.ts']);

		expect(mocks.getGitStatus).not.toHaveBeenCalled();
		expect(mocks.getDiffStats).not.toHaveBeenCalled();
		expect(mocks.getChangedFiles).toHaveBeenCalledTimes(1);
		expect(git.current.status?.staged).toBe(1);
		expect(git.current.diffStats).toEqual({ additions: 1, deletions: 0, files_changed: 1 });
	});

	it('a poll response that raced the commit cannot resurrect the pre-commit tree', async () => {
		await git.loadRepository('p1', false, 'ses-1');

		// A status poll is already in flight when the commit lands.
		const gate = deferred<GitStatus>();
		mocks.getGitStatus.mockImplementationOnce(() => gate.promise);
		const racingPoll = git.refreshStatus();

		mocks.commitChanges.mockResolvedValue({
			success: true,
			message: 'ok',
			commit_sha: 'abc',
			status: transformGitStatus({ is_git_repo: true, current_branch: 'feat/x', ahead: 2 }),
			diff_stats: { additions: 0, deletions: 0, files_changed: 0 }
		});
		await git.commitChanges('msg');
		expect(git.current.status?.has_changes).toBe(false);

		// The pre-commit poll answer arrives late; it must not win.
		gate.resolve(status());
		await racingPoll;
		expect(git.current.status?.has_changes).toBe(false);
		expect(git.current.status?.ahead).toBe(2);
	});
});

describe('git store — GitHub availability hysteresis (G10c)', () => {
	it('a single failed auth check does not flip GitHub availability', async () => {
		await git.loadRepository('p1', false, 'ses-1');
		git.setGithubAuth(false);
		expect(git.current.githubAvailable).toBe(true);
		// …and local repository state is untouched.
		expect(git.current.status?.is_git_repo).toBe(true);
		expect(git.current.branches.length).toBe(1);
	});

	it('recovers immediately when the next check succeeds', () => {
		git.setGithubAuth(false);
		git.setGithubAuth(true);
		expect(git.current.githubAvailable).toBe(true);
	});

	it('flips only after repeated failures spanning the grace window', () => {
		vi.useFakeTimers();
		try {
			git.setGithubAuth(false);
			vi.advanceTimersByTime(61_000);
			git.setGithubAuth(false);
			expect(git.current.githubAvailable).toBe(false);
		} finally {
			vi.useRealTimers();
		}
	});
});
