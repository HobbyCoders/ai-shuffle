/**
 * GitHub store — status / pull-request loading, the 502 "unknown" contract,
 * and the stale-response guard across project switches.
 *
 * The API module is mocked; errors are thrown in the shape ApiClient really
 * throws (`{ status, detail }` plain objects).
 */

import { describe, it, expect, vi, beforeEach } from 'vitest';
import type { GitHubStatus, PullRequest } from '$lib/api/github';

const api = vi.hoisted(() => ({
	getGitHubStatus: vi.fn(),
	getPullRequests: vi.fn(),
	createPullRequest: vi.fn(),
	mergePullRequest: vi.fn(),
	closePullRequest: vi.fn()
}));

vi.mock('$lib/api/github', () => api);

import { github, githubRepoInfo } from './github.svelte';

function status(repo: string, default_branch = 'main', authenticated = true): GitHubStatus {
	return { authenticated, repo, repo_info: { full_name: repo, default_branch }, error: null };
}

function pr(number: number): PullRequest {
	return {
		number, title: `PR ${number}`, body: '', state: 'open', head_branch: `b${number}`,
		base_branch: 'main', author: 'me', url: `https://x/${number}`,
		created_at: '2026-01-01T00:00:00Z', updated_at: '2026-01-01T00:00:00Z', mergeable: true
	};
}

function deferred<T>() {
	let resolve!: (v: T) => void;
	let reject!: (e: unknown) => void;
	const promise = new Promise<T>((res, rej) => { resolve = res; reject = rej; });
	return { promise, resolve, reject };
}

const unavailable = { status: 502, detail: 'gh: could not reach GitHub' };

beforeEach(() => {
	for (const fn of Object.values(api)) fn.mockReset();
	github.clear();
});

describe('github store — initial state', () => {
	it('starts empty and not loading', () => {
		expect(github.current).toMatchObject({
			projectId: null, authenticated: false, repoInfo: null, pullRequests: [],
			pullRequestsStale: false, loading: false, loadingPRs: false, error: null
		});
		expect(githubRepoInfo.current).toBeNull();
	});
});

describe('github store — loadStatus', () => {
	it('stores repo info and returns the auth verdict', async () => {
		api.getGitHubStatus.mockResolvedValue(status('o/a', 'trunk'));
		await expect(github.loadStatus('A')).resolves.toBe(true);
		expect(github.current).toMatchObject({ projectId: 'A', authenticated: true, repoName: 'o/a', loading: false });
		expect(githubRepoInfo.current?.default_branch).toBe('trunk');
	});

	it('treats a 502 as unknown: returns null and keeps the last known repo info', async () => {
		api.getGitHubStatus.mockResolvedValueOnce(status('o/a', 'trunk'));
		await github.loadStatus('A');
		api.getGitHubStatus.mockRejectedValueOnce(unavailable);
		await expect(github.loadStatus('A')).resolves.toBeNull();
		expect(github.current.authenticated).toBe(true);
		expect(github.current.repoInfo?.default_branch).toBe('trunk');
		expect(github.current.error).toBe('gh: could not reach GitHub');
		expect(github.current.loading).toBe(false);
	});

	it('treats any other failure as a denial', async () => {
		api.getGitHubStatus.mockResolvedValueOnce(status('o/a'));
		await github.loadStatus('A');
		api.getGitHubStatus.mockRejectedValueOnce({ status: 401, detail: 'gh auth required' });
		await expect(github.loadStatus('A')).resolves.toBe(false);
		expect(github.current.authenticated).toBe(false);
		expect(github.current.error).toBe('gh auth required');
	});

	it("does not carry the previous project's repo info into a new project", async () => {
		api.getGitHubStatus.mockResolvedValueOnce(status('o/a', 'trunk'));
		await github.loadStatus('A');
		api.getGitHubStatus.mockRejectedValueOnce(unavailable);
		await expect(github.loadStatus('B')).resolves.toBeNull();
		expect(github.current.projectId).toBe('B');
		expect(github.current.repoInfo).toBeNull();
		expect(githubRepoInfo.current).toBeNull();
	});

	it('discards a slower response for a project the user already left', async () => {
		const slowA = deferred<GitHubStatus>();
		api.getGitHubStatus.mockReturnValueOnce(slowA.promise).mockResolvedValueOnce(status('o/b', 'dev'));
		const first = github.loadStatus('A');
		await github.loadStatus('B');
		slowA.resolve(status('o/a', 'trunk'));
		await first;
		expect(github.current.projectId).toBe('B');
		expect(github.current.repoName).toBe('o/b');
		expect(githubRepoInfo.current?.default_branch).toBe('dev');
	});

	it('discards a stale failure so it cannot flag the newer project', async () => {
		const slowA = deferred<GitHubStatus>();
		api.getGitHubStatus.mockReturnValueOnce(slowA.promise).mockResolvedValueOnce(status('o/b'));
		const first = github.loadStatus('A');
		await github.loadStatus('B');
		slowA.reject({ status: 401, detail: 'nope' });
		await first;
		expect(github.current.error).toBeNull();
		expect(github.current.authenticated).toBe(true);
	});
});

describe('github store — ensureRepoInfo', () => {
	it('loads once per project and then answers from the cache', async () => {
		api.getGitHubStatus.mockResolvedValue(status('o/a'));
		await expect(github.ensureRepoInfo('A')).resolves.toBe(true);
		await expect(github.ensureRepoInfo('A')).resolves.toBe(true);
		expect(api.getGitHubStatus).toHaveBeenCalledTimes(1);
		await github.ensureRepoInfo('B');
		expect(api.getGitHubStatus).toHaveBeenCalledTimes(2);
		expect(api.getGitHubStatus).toHaveBeenLastCalledWith('B');
	});
});

describe('github store — pull requests', () => {
	it('is a no-op without a project', async () => {
		await github.loadPullRequests();
		expect(api.getPullRequests).not.toHaveBeenCalled();
	});

	it('loads the list for the current project and state filter', async () => {
		api.getGitHubStatus.mockResolvedValue(status('o/a'));
		await github.loadStatus('A');
		api.getPullRequests.mockResolvedValue([pr(1), pr(2)]);
		await github.loadPullRequests('all');
		expect(api.getPullRequests).toHaveBeenCalledWith('A', 'all');
		expect(github.current.pullRequests.map((p) => p.number)).toEqual([1, 2]);
		expect(github.current.loadingPRs).toBe(false);
	});

	it('keeps the last list and flags it stale on a 502', async () => {
		api.getGitHubStatus.mockResolvedValue(status('o/a'));
		await github.loadStatus('A');
		api.getPullRequests.mockResolvedValueOnce([pr(1)]).mockRejectedValueOnce(unavailable);
		await github.loadPullRequests();
		await github.loadPullRequests();
		expect(github.current.pullRequests.map((p) => p.number)).toEqual([1]);
		expect(github.current.pullRequestsStale).toBe(true);
		api.getPullRequests.mockResolvedValueOnce([]);
		await github.loadPullRequests();
		expect(github.current.pullRequests).toEqual([]);
		expect(github.current.pullRequestsStale).toBe(false);
	});

	it("drops a PR list that arrives after switching project", async () => {
		api.getGitHubStatus.mockResolvedValue(status('o/a'));
		await github.loadStatus('A');
		const slow = deferred<PullRequest[]>();
		api.getPullRequests.mockReturnValueOnce(slow.promise);
		const pending = github.loadPullRequests();
		await github.loadStatus('B');
		slow.resolve([pr(7)]);
		await pending;
		expect(github.current.projectId).toBe('B');
		expect(github.current.pullRequests).toEqual([]);
	});

	it('create refreshes the list; a failure surfaces the detail and rethrows', async () => {
		await expect(github.createPullRequest({ title: 't' } as never)).rejects.toThrow('No project selected');
		api.getGitHubStatus.mockResolvedValue(status('o/a'));
		await github.loadStatus('A');
		api.createPullRequest.mockResolvedValueOnce(pr(3));
		api.getPullRequests.mockResolvedValueOnce([pr(3)]);
		await expect(github.createPullRequest({ title: 't' } as never)).resolves.toMatchObject({ number: 3 });
		expect(github.current.pullRequests.map((p) => p.number)).toEqual([3]);
		expect(github.current.loading).toBe(false);

		const refusal = { status: 422, detail: 'A pull request already exists' };
		api.mergePullRequest.mockRejectedValueOnce(refusal);
		await expect(github.mergePullRequest(3, 'squash')).rejects.toBe(refusal);
		expect(api.mergePullRequest).toHaveBeenCalledWith('A', 3, 'squash');
		expect(github.current).toMatchObject({ loading: false, error: 'A pull request already exists' });
		github.clearError();
		expect(github.current.error).toBeNull();
	});
});
