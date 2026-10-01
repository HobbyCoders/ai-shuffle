/**
 * SessionCard + the REAL sidebar checkout store (#1282).
 *
 * SessionCard.test.ts stubs `sessionGitState`, so it cannot see how often the
 * card re-leases its poll. Here only the HTTP layer is mocked: every
 * `getGitStatus` call below is a request the browser would really send. The
 * card is mounted through a harness that, like Sidebar.svelte, varies only the
 * `session` prop.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, render } from '@testing-library/svelte';

const mocks = vi.hoisted(() => ({
	getGitStatus: vi.fn(),
	getDiffStats: vi.fn(),
}));

vi.mock('$lib/api/git', () => ({
	getGitStatus: mocks.getGitStatus,
	getDiffStats: mocks.getDiffStats,
}));

vi.mock('$lib/stores/app-data.svelte', () => ({
	appData: {
		setSessionActive: vi.fn(),
		deleteSession: vi.fn(),
		renameSession: vi.fn(),
		loadSessions: vi.fn(),
	},
}));

vi.mock('$lib/stores/active-session', () => ({
	activeSession: {
		subscribe(run: (value: { sessionId: string | null }) => void) {
			run({ sessionId: null });
			return () => {};
		},
		newChat: vi.fn(),
		setTitle: vi.fn(),
	},
}));

vi.mock('$lib/api/client', () => ({ forkSession: vi.fn() }));
vi.mock('$lib/stores/notifications', () => ({ notify: vi.fn() }));

// No session is open in the main panel, so every active card leases a poll.
vi.mock('$lib/stores/git.svelte', () => ({
	git: {
		current: { hydrated: true, sessionId: null, projectId: null, worktreeId: null, status: null, diffStats: null },
	},
}));

import SessionCardRowHarness from '$lib/test-stubs/SessionCardRowHarness.svelte';
import { REFRESH_MS, sessionGitState } from '$lib/stores/session-git-state.svelte';

function gitStatus(over: Record<string, unknown> = {}) {
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
		unpushed: null,
		repository_id: null,
		default_branch: 'main',
		github_repo_name: null,
		files_fingerprint: 'x',
		...over,
	};
}

/** A sidebar row exactly as a fresh session-list response hands it over. */
function row(over: Record<string, unknown> = {}) {
	return {
		id: 'session-a',
		profile_id: 'default',
		project_id: 'project-a',
		worktree_id: 'wt-1',
		title: 'Sidebar work',
		status: 'active',
		is_active: true,
		total_cost_usd: 0,
		total_tokens_in: 0,
		total_tokens_out: 0,
		cache_creation_tokens: 0,
		cache_read_tokens: 0,
		context_tokens: 0,
		turn_count: 1,
		tags: [],
		parent_session_id: null,
		fork_point_message_index: null,
		has_forks: false,
		created_at: '2026-08-20T12:00:00Z',
		updated_at: '2026-08-20T12:05:00Z',
		is_remote: false,
		...over,
	};
}

/** Only `session` changes between renders, exactly as in Sidebar.svelte. */
function props(session: Record<string, unknown>) {
	return { session: session as never };
}

const flush = () => vi.advanceTimersByTimeAsync(0);

beforeEach(() => {
	vi.useFakeTimers();
	vi.clearAllMocks();
	Object.defineProperty(document, 'hidden', { configurable: true, value: false });
	Object.defineProperty(window, 'matchMedia', {
		writable: true,
		value: vi.fn().mockImplementation(() => ({ matches: false, addEventListener: vi.fn(), removeEventListener: vi.fn() })),
	});
	mocks.getGitStatus.mockResolvedValue(gitStatus());
	mocks.getDiffStats.mockResolvedValue({ additions: 3, deletions: 1, files_changed: 1 });
});

afterEach(() => {
	cleanup();
	sessionGitState.reset();
	vi.useRealTimers();
});

describe('SessionCard checkout lease', () => {
	it('keeps its poll when a session-list reload hands it a fresh row object', async () => {
		const view = render(SessionCardRowHarness, { props: props(row()) });
		await flush();
		expect(mocks.getGitStatus).toHaveBeenCalledTimes(1);
		expect(mocks.getDiffStats).toHaveBeenCalledTimes(1);

		// Every visibility return, reconnect and `resources_changed` reloads the
		// list, which replaces each row object without moving its checkout.
		for (const updated_at of ['2026-08-20T12:06:00Z', '2026-08-20T12:07:00Z', '2026-08-20T12:08:00Z']) {
			await view.rerender(props(row({ updated_at, title: `renamed ${updated_at}` })));
			await flush();
		}
		expect(mocks.getGitStatus).toHaveBeenCalledTimes(1);
		expect(mocks.getDiffStats).toHaveBeenCalledTimes(1);

		// The lease survived intact: exactly one request pair per interval.
		await vi.advanceTimersByTimeAsync(REFRESH_MS);
		expect(mocks.getGitStatus).toHaveBeenCalledTimes(2);
		expect(mocks.getDiffStats).toHaveBeenCalledTimes(2);
	});

	it('does not abort a slow poll when the row object is replaced mid-flight', async () => {
		let resolve!: (value: ReturnType<typeof gitStatus>) => void;
		let signal: AbortSignal | undefined;
		mocks.getGitStatus.mockImplementationOnce((_p: string, _w: string, options?: { signal?: AbortSignal }) => {
			signal = options?.signal;
			return new Promise((r) => { resolve = r; });
		});
		const view = render(SessionCardRowHarness, { props: props(row()) });
		await flush();
		await view.rerender(props(row({ updated_at: '2026-08-20T12:09:00Z' })));
		await flush();

		expect(signal?.aborted).toBe(false);
		expect(mocks.getGitStatus).toHaveBeenCalledTimes(1);
		resolve(gitStatus());
		await flush();
		expect(mocks.getDiffStats).toHaveBeenCalledTimes(1);
		expect(view.container.querySelector('.diff')?.textContent).toBe('+3 −1');
	});

	it('issues one request pair per interval for a dirty card and one request for a clean card', async () => {
		render(SessionCardRowHarness, { props: props(row()) });
		await flush();
		await vi.advanceTimersByTimeAsync(REFRESH_MS * 2);
		expect(mocks.getGitStatus).toHaveBeenCalledTimes(3);
		expect(mocks.getDiffStats).toHaveBeenCalledTimes(3);

		cleanup();
		sessionGitState.reset();
		vi.clearAllMocks();
		mocks.getGitStatus.mockResolvedValue(gitStatus({ has_changes: false, is_clean: true, modified: 0 }));
		render(SessionCardRowHarness, { props: props(row({ id: 'session-b' })) });
		await flush();
		await vi.advanceTimersByTimeAsync(REFRESH_MS * 2);
		expect(mocks.getGitStatus).toHaveBeenCalledTimes(3);
		expect(mocks.getDiffStats).not.toHaveBeenCalled();
	});

	it('re-leases when the row moves to another checkout', async () => {
		const view = render(SessionCardRowHarness, { props: props(row()) });
		await flush();
		expect(mocks.getGitStatus).toHaveBeenLastCalledWith('project-a', 'wt-1', expect.anything(), 'session-a');

		await view.rerender(props(row({ worktree_id: 'wt-2' })));
		await flush();
		expect(mocks.getGitStatus).toHaveBeenCalledTimes(2);
		expect(mocks.getGitStatus).toHaveBeenLastCalledWith('project-a', 'wt-2', expect.anything(), 'session-a');
	});
});
