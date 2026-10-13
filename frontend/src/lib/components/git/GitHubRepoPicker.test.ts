/**
 * GitHub repository picker for the clone dialog: loading, not-connected hint,
 * listing and selection, server-side search (debounced, stale answers
 * dropped), Load more, refresh, and error/retry.
 */
import { afterEach, beforeEach, describe, expect, it, vi, type Mock } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/svelte';
import type { GitHubRepo, GitHubRepoPage } from '$lib/api/github';

const githubApi = vi.hoisted(() => ({ listGitHubRepos: vi.fn() }));
vi.mock('$lib/api/github', () => githubApi);
import GitHubRepoPicker from './GitHubRepoPicker.svelte';

function repo(full_name: string, extra: Partial<GitHubRepo> = {}): GitHubRepo {
	const [owner, name] = full_name.split('/');
	return {
		full_name, name, owner, private: false, fork: false, archived: false, description: null,
		clone_url: `https://github.com/${full_name}.git`, html_url: `https://github.com/${full_name}`,
		default_branch: 'main', updated_at: new Date(Date.now() - 3 * 86_400_000).toISOString(), ...extra
	};
}

function pageOf(items: GitHubRepo[], extra: Partial<GitHubRepoPage> = {}): GitHubRepoPage {
	return { connected: true, items, total: items.length, page: 1, per_page: 30, has_more: false, truncated: false, ...extra };
}

const list = githubApi.listGitHubRepos as Mock;
let onSelect: Mock<(r: GitHubRepo) => void>;
let onConnect: Mock<() => void>;

beforeEach(() => {
	vi.clearAllMocks();
	onSelect = vi.fn();
	onConnect = vi.fn();
});
afterEach(() => {
	cleanup();
	vi.useRealTimers();
});

function mount(extra: Record<string, unknown> = {}) {
	return render(GitHubRepoPicker, { props: { onSelect, onConnect, ...extra } });
}

const repoButton = (name: RegExp) => screen.getByRole('button', { name });

describe('GitHubRepoPicker', () => {
	it('shows loading, then lists repos with owner, visibility and last update', async () => {
		let resolve!: (p: GitHubRepoPage) => void;
		list.mockReturnValue(new Promise((r) => (resolve = r)));
		mount();
		expect(screen.getByText('Loading your repositories...')).toBeTruthy();
		resolve(pageOf([repo('acme/private-app', { private: true }), repo('me/site', { fork: true })]));

		const priv = await screen.findByRole('button', { name: /^acme\/private-app, private, updated 3d ago$/ });
		expect(priv.textContent).toContain('acme/');
		expect(priv.textContent).toContain('Private');
		const pub = repoButton(/^me\/site, public/);
		expect(pub.textContent).toContain('Public');
		expect(pub.textContent).toContain('Fork');
		expect(screen.getByText('2 of 2')).toBeTruthy();
		expect(list).toHaveBeenCalledWith({ q: '', page: 1, perPage: 30, refresh: false }, expect.objectContaining({ signal: expect.any(AbortSignal) }));
	});

	it('picking a repo hands it to the dialog and marks the selected one', async () => {
		list.mockResolvedValue(pageOf([repo('acme/app'), repo('acme/api')]));
		const view = mount();
		await fireEvent.click(await screen.findByRole('button', { name: /^acme\/api,/ }));
		expect(onSelect).toHaveBeenCalledWith(expect.objectContaining({ clone_url: 'https://github.com/acme/api.git' }));

		await view.rerender({ selectedUrl: ' https://github.com/acme/api.git ' });
		expect(repoButton(/^acme\/api,/).getAttribute('aria-pressed')).toBe('true');
		expect(repoButton(/^acme\/app,/).getAttribute('aria-pressed')).toBe('false');
	});

	it('shows where to connect GitHub when it is not connected', async () => {
		list.mockResolvedValue({ ...pageOf([]), connected: false });
		mount();
		await fireEvent.click(await screen.findByRole('button', { name: /Connect GitHub/ }));
		expect(onConnect).toHaveBeenCalledOnce();
		expect(screen.queryByLabelText('Search repositories')).toBeNull();
	});

	it('searches on the server after typing stops, ignoring an older answer', async () => {
		list.mockResolvedValueOnce(pageOf([repo('acme/app'), repo('me/notes')]));
		mount();
		await screen.findByRole('button', { name: /^acme\/app,/ });

		vi.useFakeTimers();
		let resolveOld!: (p: GitHubRepoPage) => void;
		list.mockReturnValueOnce(new Promise((r) => (resolveOld = r)));
		list.mockResolvedValueOnce(pageOf([repo('me/notes')]));
		const search = screen.getByLabelText('Search repositories');
		await fireEvent.input(search, { target: { value: 'no' } });
		await vi.advanceTimersByTimeAsync(100);
		expect(list).toHaveBeenCalledTimes(1); // debounced
		await vi.advanceTimersByTimeAsync(200);
		await fireEvent.input(search, { target: { value: 'notes' } });
		await vi.advanceTimersByTimeAsync(300);
		vi.useRealTimers();

		expect(list).toHaveBeenNthCalledWith(2, expect.objectContaining({ q: 'no', page: 1 }), expect.anything());
		expect(list).toHaveBeenNthCalledWith(3, expect.objectContaining({ q: 'notes', page: 1 }), expect.anything());
		// The superseded request was aborted and its late answer is dropped.
		expect((list.mock.calls[1][1] as { signal: AbortSignal }).signal.aborted).toBe(true);
		resolveOld(pageOf([repo('stale/answer')]));
		await waitFor(() => expect(screen.queryByRole('button', { name: /^acme\/app,/ })).toBeNull());
		expect(screen.queryByRole('button', { name: /stale\/answer/ })).toBeNull();
		expect(repoButton(/^me\/notes,/)).toBeTruthy();
	});

	it('says when a search matches nothing', async () => {
		list.mockResolvedValueOnce(pageOf([repo('acme/app')]));
		mount();
		await screen.findByRole('button', { name: /^acme\/app,/ });
		list.mockResolvedValueOnce(pageOf([]));
		await fireEvent.input(screen.getByLabelText('Search repositories'), { target: { value: 'zzz' } });
		expect(await screen.findByText('No repositories match "zzz"')).toBeTruthy();
	});

	it('loads more pages and appends them', async () => {
		list.mockResolvedValueOnce(pageOf([repo('a/one'), repo('a/two')], { total: 3, has_more: true, per_page: 2 }));
		mount();
		await screen.findByRole('button', { name: 'Load more' });
		expect(screen.getByText('2 of 3')).toBeTruthy();
		expect(list).toHaveBeenCalledTimes(1);
		list.mockResolvedValueOnce(pageOf([repo('a/three')], { total: 3, page: 2, per_page: 2 }));
		await fireEvent.click(screen.getByRole('button', { name: 'Load more' }));
		await screen.findByRole('button', { name: /^a\/three,/ });
		expect(list).toHaveBeenLastCalledWith(expect.objectContaining({ page: 2 }), expect.anything());
		expect(repoButton(/^a\/one,/)).toBeTruthy();
		expect(screen.getByText('3 of 3')).toBeTruthy();
		expect(screen.queryByRole('button', { name: 'Load more' })).toBeNull();
	});

	it('refresh asks the server to refetch', async () => {
		list.mockResolvedValue(pageOf([repo('a/one')]));
		mount();
		await screen.findByRole('button', { name: /^a\/one,/ });
		await fireEvent.click(screen.getByRole('button', { name: 'Refresh repositories' }));
		await waitFor(() => expect(list).toHaveBeenLastCalledWith(expect.objectContaining({ refresh: true, page: 1 }), expect.anything()));
	});

	it('shows the error with a retry that recovers', async () => {
		list.mockRejectedValueOnce({ detail: 'GitHub could not list your repositories: HTTP 502' });
		mount();
		expect(await screen.findByText('GitHub could not list your repositories: HTTP 502')).toBeTruthy();
		list.mockResolvedValueOnce(pageOf([repo('a/one')]));
		await fireEvent.click(screen.getByRole('button', { name: 'Retry' }));
		expect(await screen.findByRole('button', { name: /^a\/one,/ })).toBeTruthy();
	});

	it('mentions when older repositories were left out', async () => {
		list.mockResolvedValue(pageOf([repo('a/one')], { truncated: true }));
		mount();
		expect(await screen.findByText(/1,000 most recently pushed/)).toBeTruthy();
	});

	it('disables every repo while the dialog is busy', async () => {
		list.mockResolvedValue(pageOf([repo('a/one')]));
		mount({ disabled: true });
		const button = (await screen.findByRole('button', { name: /^a\/one,/ })) as HTMLButtonElement;
		expect(button.disabled).toBe(true);
		await fireEvent.click(button);
		expect(onSelect).not.toHaveBeenCalled();
	});
});
