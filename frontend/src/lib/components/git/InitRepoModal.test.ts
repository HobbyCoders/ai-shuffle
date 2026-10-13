/**
 * Set up Git modal: init payloads (initial commit, GitHub repo name and
 * visibility), clone payload, submit guards, worker errors and reset on reopen,
 * and the GitHub repository picker inside the clone section.
 */
import { afterEach, beforeEach, describe, expect, it, vi, type Mock } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/svelte';

const gitApi = vi.hoisted(() => ({ initRepository: vi.fn(), cloneRepository: vi.fn() }));
vi.mock('$lib/api/git', () => gitApi);
const githubApi = vi.hoisted(() => ({ listGitHubRepos: vi.fn() }));
vi.mock('$lib/api/github', () => githubApi);
const layoutMock = vi.hoisted(() => ({ openFeaturePage: vi.fn() }));
vi.mock('$lib/stores/layout.svelte', () => ({ layout: layoutMock }));
import InitRepoModal from './InitRepoModal.svelte';

const submit = (name: RegExp) => screen.getAllByRole('button', { name }).find((b) => !b.hasAttribute('aria-pressed')) as HTMLButtonElement;
const modeButton = (name: string) => screen.getAllByRole('button', { name }).find((b) => b.hasAttribute('aria-pressed')) as HTMLButtonElement;

function reposPage(names: string[], connected = true) {
	const items = names.map((full_name) => ({
		full_name, name: full_name.split('/')[1], owner: full_name.split('/')[0], private: true, fork: false,
		archived: false, description: null, clone_url: `https://github.com/${full_name}.git`,
		html_url: `https://github.com/${full_name}`, default_branch: 'main', updated_at: '2026-01-01T00:00:00Z'
	}));
	return { connected, items, total: items.length, page: 1, per_page: 30, has_more: false, truncated: false };
}

let onClose: Mock<() => void>;
let onInitialized: Mock<() => void>;
beforeEach(() => {
	vi.clearAllMocks();
	gitApi.initRepository.mockResolvedValue({});
	gitApi.cloneRepository.mockResolvedValue({});
	githubApi.listGitHubRepos.mockResolvedValue(reposPage([]));
	onClose = vi.fn();
	onInitialized = vi.fn();
});
afterEach(() => cleanup());

function mount(extra: Record<string, unknown> = {}) {
	return render(InitRepoModal, { props: { open: true, projectId: 'p1', sessionId: 's1', projectName: 'My Cool App', onClose, onInitialized, ...extra } });
}

describe('InitRepoModal init', () => {
	it('initialises with an initial commit and no GitHub repo by default', async () => {
		mount();
		expect(modeButton('New repo').getAttribute('aria-pressed')).toBe('true');
		await fireEvent.click(submit(/Initialize/));
		await waitFor(() => expect(onClose).toHaveBeenCalledOnce());
		expect(gitApi.initRepository).toHaveBeenCalledWith('p1', {
			create_initial_commit: true, create_github_repo: false, github_repo_name: undefined, github_private: true
		}, 's1');
		expect(onInitialized).toHaveBeenCalledOnce();
	});

	it('creates a public GitHub repo named from the project, and needs a name', async () => {
		mount();
		await fireEvent.click(screen.getByRole('checkbox', { name: /Create initial commit/ }));
		await fireEvent.click(screen.getByRole('checkbox', { name: /Create GitHub repository/ }));
		const name = screen.getByLabelText('Repository name') as HTMLInputElement;
		expect(name.value).toBe('my-cool-app');
		await fireEvent.input(name, { target: { value: '  ' } });
		expect(submit(/Initialize/).disabled).toBe(true);
		await fireEvent.input(name, { target: { value: ' cool-app ' } });
		await fireEvent.click(screen.getByRole('button', { name: 'Public' }));
		expect(screen.getByRole('button', { name: 'Public' }).getAttribute('aria-pressed')).toBe('true');
		expect(screen.getByRole('button', { name: 'Private' }).getAttribute('aria-pressed')).toBe('false');
		await fireEvent.click(submit(/Initialize/));
		await waitFor(() => expect(gitApi.initRepository).toHaveBeenCalledWith('p1', {
			create_initial_commit: false, create_github_repo: true, github_repo_name: 'cool-app', github_private: false
		}, 's1'));
	});

	it('shows the worker error and keeps the dialog open', async () => {
		gitApi.initRepository.mockRejectedValue({ detail: 'GitHub is not connected' });
		mount();
		await fireEvent.click(submit(/Initialize/));
		expect((await screen.findByRole('alert')).textContent).toBe('GitHub is not connected');
		expect(onClose).not.toHaveBeenCalled();
		expect(submit(/Initialize/).disabled).toBe(false);
	});

	it('disables every control while initialising', async () => {
		let resolve!: () => void;
		gitApi.initRepository.mockReturnValue(new Promise<void>((r) => (resolve = r)));
		mount();
		await fireEvent.click(submit(/Initialize/));
		expect(submit(/Initializing/).disabled).toBe(true);
		expect(modeButton('Clone').disabled).toBe(true);
		expect((screen.getByRole('checkbox', { name: /Create initial commit/ }) as HTMLInputElement).disabled).toBe(true);
		// Enter does not submit a second time.
		await fireEvent.keyDown(screen.getByRole('dialog'), { key: 'Enter' });
		expect(gitApi.initRepository).toHaveBeenCalledOnce();
		resolve();
		await waitFor(() => expect(onClose).toHaveBeenCalledOnce());
	});
});

describe('InitRepoModal clone', () => {
	it('clones the trimmed URL and needs one first', async () => {
		mount();
		await fireEvent.click(modeButton('Clone'));
		expect(screen.getByText('Clone an existing repository into this project.')).toBeTruthy();
		expect(submit(/^Clone$/).disabled).toBe(true);
		await fireEvent.input(screen.getByLabelText('Repository URL'), { target: { value: ' git@github.com:me/app.git ' } });
		await fireEvent.keyDown(screen.getByLabelText('Repository URL'), { key: 'Enter' });
		await waitFor(() => expect(gitApi.cloneRepository).toHaveBeenCalledWith('p1', { clone_url: 'git@github.com:me/app.git' }, 's1'));
		expect(gitApi.initRepository).not.toHaveBeenCalled();
		expect(onInitialized).toHaveBeenCalledOnce();
	});

	it('falls back to a clone-specific error message', async () => {
		gitApi.cloneRepository.mockRejectedValue({});
		mount();
		await fireEvent.click(modeButton('Clone'));
		await fireEvent.input(screen.getByLabelText('Repository URL'), { target: { value: 'https://x/y.git' } });
		await fireEvent.click(submit(/^Clone$/));
		expect((await screen.findByRole('alert')).textContent).toBe('Failed to clone repository');
	});

	it('resets to a new repo with no error when reopened', async () => {
		gitApi.cloneRepository.mockRejectedValue({ detail: 'auth failed' });
		const view = mount();
		await fireEvent.click(modeButton('Clone'));
		await fireEvent.input(screen.getByLabelText('Repository URL'), { target: { value: 'https://x/y.git' } });
		await fireEvent.click(submit(/^Clone$/));
		await screen.findByRole('alert');
		await view.rerender({ open: false });
		await view.rerender({ open: true });
		expect(modeButton('New repo').getAttribute('aria-pressed')).toBe('true');
		expect(screen.queryByRole('alert')).toBeNull();
	});

	it('closes on Escape without submitting', async () => {
		mount();
		await fireEvent.keyDown(window, { key: 'Escape' });
		expect(onClose).toHaveBeenCalledOnce();
		expect(gitApi.initRepository).not.toHaveBeenCalled();
	});
});

describe('InitRepoModal GitHub picker', () => {
	it('is only asked for in clone mode', async () => {
		mount();
		expect(githubApi.listGitHubRepos).not.toHaveBeenCalled();
		await fireEvent.click(modeButton('Clone'));
		await waitFor(() => expect(githubApi.listGitHubRepos).toHaveBeenCalledOnce());
	});

	it('picking a repo fills the URL and clones it', async () => {
		githubApi.listGitHubRepos.mockResolvedValue(reposPage(['acme/private-app', 'me/site']));
		mount();
		await fireEvent.click(modeButton('Clone'));
		await fireEvent.click(await screen.findByRole('button', { name: /^acme\/private-app, private/ }));
		const url = screen.getByLabelText('Repository URL') as HTMLInputElement;
		expect(url.value).toBe('https://github.com/acme/private-app.git');
		await fireEvent.click(submit(/^Clone$/));
		await waitFor(() => expect(gitApi.cloneRepository).toHaveBeenCalledWith('p1', { clone_url: 'https://github.com/acme/private-app.git' }, 's1'));
	});

	it('Enter in the repository search does not clone', async () => {
		githubApi.listGitHubRepos.mockResolvedValue(reposPage(['me/site']));
		mount();
		await fireEvent.click(modeButton('Clone'));
		await fireEvent.input(screen.getByLabelText('Repository URL'), { target: { value: 'https://x/y.git' } });
		const search = await screen.findByLabelText('Search repositories');
		await fireEvent.keyDown(search, { key: 'Enter' });
		expect(gitApi.cloneRepository).not.toHaveBeenCalled();
	});

	it('links to GitHub settings when GitHub is not connected', async () => {
		githubApi.listGitHubRepos.mockResolvedValue(reposPage([], false));
		mount();
		await fireEvent.click(modeButton('Clone'));
		await fireEvent.click(await screen.findByRole('button', { name: /Connect GitHub/ }));
		expect(onClose).toHaveBeenCalledOnce();
		expect(layoutMock.openFeaturePage).toHaveBeenCalledWith('settings', 'category=integrations&item=github');
	});
});
