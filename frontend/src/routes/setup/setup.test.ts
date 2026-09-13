import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/svelte';
import type { NetworkStatus } from '$lib/api/network';

const mocks = vi.hoisted(() => ({
	getStatus: vi.fn(), create: vi.fn(), enroll: vi.fn(), cancel: vi.fn(), unlock: vi.fn(),
	get: vi.fn(), post: vi.fn(), patch: vi.fn(), delete: vi.fn(), goto: vi.fn(),
	workspace: vi.fn(), saveWorkspace: vi.fn(), setup: vi.fn(), complete: vi.fn(), checkAuth: vi.fn(),
	githubStatus: vi.fn(), githubWeb: vi.fn(), githubToken: vi.fn(),
	hasCredential: false, providers: [] as { id: string; name: string; provider_type: string }[],
	claudeAuthenticated: false, claudeSubscribers: new Set<(v: boolean) => void>(),
	claudeAccounts: [] as { id: string }[], anthropicKeySet: false, falKeySet: false,
	refreshProviders: vi.fn(), loadClaudeAccounts: vi.fn(), loadIntegrations: vi.fn()
}));
vi.mock('$app/navigation', () => ({ goto: mocks.goto }));
vi.mock('$lib/api/network', () => ({ getNetworkStatus: mocks.getStatus, createNetwork: mocks.create,
	startEnrollment: mocks.enroll, cancelEnrollment: mocks.cancel }));
vi.mock('$lib/api/client', () => ({ api: { get: mocks.get, post: mocks.post, patch: mocks.patch, delete: mocks.delete },
	getErrorMessage: (e: { detail?: string; message?: string }, fallback: string) => e.detail || e.message || fallback }));
vi.mock('$lib/api/auth', () => ({ getWorkspaceConfig: mocks.workspace, setWorkspaceConfig: mocks.saveWorkspace,
	setupInstallation: mocks.setup, completeOnboarding: mocks.complete,
	getGithubAuthStatus: mocks.githubStatus, startGithubWebLogin: mocks.githubWeb, loginGithubWithToken: mocks.githubToken, cancelGithubWebLogin: vi.fn(), logoutGithub: vi.fn() }));
vi.mock('$lib/stores/auth.svelte', () => ({
	auth: { checkAuth: mocks.checkAuth, current: { status: { setup_required: true, auth_mode: 'local' } }, logout: vi.fn(),
		subscribe(run: (v: unknown) => void) { run({ status: { auth_mode: 'local' } }); return () => {}; } },
	anyCredential: { subscribe(run: (v: boolean) => void) { run(mocks.hasCredential); return () => {}; } },
	claudeAuthenticated: { subscribe(run: (v: boolean) => void) { mocks.claudeSubscribers.add(run); run(mocks.claudeAuthenticated); return () => mocks.claudeSubscribers.delete(run); } }
}));
vi.mock('$lib/stores/providers.svelte', () => ({ providers: { current: {
	get providers() { return mocks.providers; }, get claudeAccounts() { return mocks.claudeAccounts; },
	get anthropicKeySet() { return mocks.anthropicKeySet; }, get falKeySet() { return mocks.falKeySet; }
}, ensureLoaded: vi.fn(), refreshProviders: mocks.refreshProviders, loadClaudeAccounts: mocks.loadClaudeAccounts, loadIntegrations: mocks.loadIntegrations } }));
vi.mock('$lib/components/DirectoryBrowser.svelte', () => import('$lib/test-stubs/EmptyRouteDependency.svelte'));
vi.mock('$lib/components/deck/cards/settings/providers/AddProviderModal.svelte', () => import('$lib/test-stubs/CompleteProviderModal.svelte'));
import Setup from './+page.svelte';

const fingerprint = 'abcd-1234-abcd-1234-abcd-1234-abcd-1234';
const fresh = (): NetworkStatus => ({ device: { device_id: 'local-device', fingerprint }, enrolled: false, platform: 'windows', version: 'test', instance_id: 'local-install', enrollment: null, hub: { linked: false } });
const enrolled = (): NetworkStatus => ({ ...fresh(), enrollment: undefined, enrolled: true, network: {
	network_id: 'network', name: 'Home', device_id: 'local-device', fingerprint, founder_device_id: 'local-device',
	generation: 0, key_version: 1, bootstrap_state: 'ready', member_count: 1
}, hub: { linked: false } });
const disconnected = { signed_in: false, status: 'disconnected', code: null, qr_svg: null, error: null };
const waiting = { ...disconnected, status: 'waiting', code: 'TESTONLYCODE', authorization_url: 'https://account.example.com/claim?code=TESTONLYCODE', signup_url: 'https://account.example.com/claim?code=TESTONLYCODE&screen=signup' };
const link = 'shuffle://network/join?endpoint=http%3A%2F%2F10.0.0.5%3A8000&code=ABCD-EFGH-JKLM&fp=' + fingerprint;
let status: NetworkStatus;
let connection: Record<string, unknown>;

beforeEach(() => {
	vi.resetAllMocks(); status = fresh(); connection = disconnected; mocks.hasCredential = false; mocks.providers = [];
	vi.spyOn(window, 'open').mockReturnValue(null);
	mocks.claudeAuthenticated = false; mocks.claudeAccounts = []; mocks.anthropicKeySet = false; mocks.falKeySet = false;
	mocks.refreshProviders.mockResolvedValue(undefined); mocks.loadClaudeAccounts.mockResolvedValue(undefined); mocks.loadIntegrations.mockResolvedValue(undefined);
	mocks.getStatus.mockImplementation(async () => status);
	mocks.create.mockImplementation(async () => { status = enrolled(); return { network: status.network }; });
	mocks.workspace.mockResolvedValue({ workspace_path: 'D:\\Work', default_path: 'D:\\Work', is_local_mode: true });
	mocks.saveWorkspace.mockResolvedValue({}); mocks.checkAuth.mockResolvedValue(undefined);
	mocks.githubStatus.mockResolvedValue({ authenticated: false, user: null });
	mocks.get.mockImplementation(async (path: string) => {
		if (path === '/mobile/hub/connection') return connection;
		if (path === '/mobile/hub/account') return { account: { id: 'a', username: 'owner', profile_complete: true }, servers: [] };
		throw new Error('Unexpected GET ' + path);
	});
});
afterEach(() => { cleanup(); vi.useRealTimers(); });

async function cleanInstall() {
	render(Setup);
	await fireEvent.click(await screen.findByRole('button', { name: /Continue without an account/ }));
	await screen.findByLabelText('Or type a path manually');
}
async function continueToProviders() {
	await fireEvent.click(screen.getByRole('button', { name: 'Continue' }));
	await screen.findByRole('button', { name: 'Add provider' });
}

describe('first-run setup', () => {
	it.each(['subscription', 'saved-account', 'api-key', 'fal'] as const)('shows a native %s provider without a gateway row', async (kind) => {
		mocks.claudeAuthenticated = kind === 'subscription';
		mocks.claudeAccounts = kind === 'saved-account' ? [{ id: 'claude' }] : [];
		mocks.anthropicKeySet = kind === 'api-key'; mocks.falKeySet = kind === 'fal';
		await cleanInstall(); await continueToProviders();
		expect(screen.getByText(kind === 'fal' ? 'fal.ai' : kind === 'api-key' ? 'Anthropic' : 'Claude Code')).toBeTruthy();
		expect(screen.queryByText(/Connect a Claude/)).toBeNull();
	});

	it('shows Claude immediately after completing Add provider alongside existing gateways', async () => {
		mocks.providers = [{ id: 'or', name: 'OpenRouter', provider_type: 'openrouter' }];
		await cleanInstall(); await continueToProviders();
		expect(screen.queryByText('Claude Code')).toBeNull();
		mocks.checkAuth.mockImplementation(async () => {
			mocks.claudeAuthenticated = true;
			for (const run of mocks.claudeSubscribers) run(true);
		});
		await fireEvent.click(screen.getByRole('button', { name: 'Add provider' }));
		await fireEvent.click(screen.getByRole('button', { name: 'Complete provider connection' }));
		expect(await screen.findByText('Claude Code')).toBeTruthy();
		expect(screen.getByText('OpenRouter')).toBeTruthy();
		expect(screen.getByText('Subscription added')).toBeTruthy();
		expect(mocks.loadClaudeAccounts).toHaveBeenCalledWith(true);
		expect(mocks.loadIntegrations).toHaveBeenCalledOnce();
	});

	it('shows sign-in immediately with local and invite alternatives and three steps', async () => {
		render(Setup);
		expect(await screen.findByRole('button', { name: /Continue without an account/ })).toBeTruthy();
		expect(await screen.findByRole('button', { name: 'Sign in' })).toBeTruthy();
			expect(screen.getByRole('button', { name: 'Create account' })).toBeTruthy();
			expect(mocks.post).not.toHaveBeenCalled();
		expect(screen.getByRole('button', { name: /Use a worker invite/ })).toBeTruthy();
		const progress = screen.getByRole('navigation', { name: 'Setup progress' });
		expect(progress.textContent).toContain('Start');
		expect(progress.textContent).toContain('Workspace');
		expect(progress.textContent).toContain('Providers & GitHub');
		expect(screen.queryByText(/Claude Code/)).toBeNull();
	});

	it.each([false, true])('finishes a clean install with workspace, providers and GitHub only (existing credential: %s)', async (existingCredential) => {
		mocks.hasCredential = existingCredential;
		if (existingCredential) mocks.providers = [{ id: 'openai', name: 'My OpenAI account', provider_type: 'openai' }];
		await cleanInstall();
		expect(mocks.create).toHaveBeenCalledExactlyOnceWith();
		await continueToProviders();
		expect(screen.getByText('GitHub', { exact: true })).toBeTruthy();
		expect(await screen.findByRole('button', { name: 'Sign in with GitHub' })).toBeTruthy();
		expect(screen.queryByText(/Claude Code/)).toBeNull();
		expect(screen.queryByText(/Connect Claude/)).toBeNull();
		if (existingCredential) expect(screen.getByText('My OpenAI account')).toBeTruthy();
		await fireEvent.click(screen.getByRole('button', { name: existingCredential ? 'Finish Setup' : 'Finish without a provider' }));
		await waitFor(() => expect(mocks.goto).toHaveBeenCalledWith('/'));
		expect(mocks.saveWorkspace).toHaveBeenCalledWith('D:\\Work');
		expect(mocks.setup).toHaveBeenCalledOnce(); expect(mocks.complete).toHaveBeenCalledOnce();
		expect(mocks.post).not.toHaveBeenCalled(); expect(mocks.enroll).not.toHaveBeenCalled(); expect(mocks.unlock).not.toHaveBeenCalled();
	});

	it('connects GitHub with a token during the last step', async () => {
		mocks.githubToken.mockResolvedValue({ success: true });
		mocks.githubStatus.mockResolvedValueOnce({ authenticated: false, user: null }).mockResolvedValue({ authenticated: true, user: 'octocat' });
		await cleanInstall(); await continueToProviders();
		await fireEvent.click(screen.getByRole('button', { name: 'Personal access token' }));
		await fireEvent.input(screen.getByLabelText('GitHub personal access token'), { target: { value: 'ghp_secret' } });
		await fireEvent.click(screen.getByRole('button', { name: 'Connect token' }));
		expect(mocks.githubToken).toHaveBeenCalledWith('ghp_secret');
		expect(await screen.findByText('GitHub is connected as @octocat.')).toBeTruthy();
	});

	it('resumes an initialized worker at the workspace step', async () => {
		await cleanInstall(); cleanup(); render(Setup);
		expect((await screen.findByLabelText('Or type a path manually') as HTMLInputElement).value).toBe('D:\\Work');
		expect(screen.queryByRole('button', { name: /Continue without an account/ })).toBeNull();
		await continueToProviders(); await fireEvent.click(screen.getByRole('button', { name: 'Finish without a provider' }));
		await waitFor(() => expect(mocks.complete).toHaveBeenCalledOnce());
		expect(mocks.create).toHaveBeenCalledOnce();
	});

	it('retries a clean install after a failed response without recreating persisted settings', async () => {
		mocks.create.mockImplementationOnce(async () => { status = enrolled(); throw new Error('Response lost'); });
		render(Setup); await fireEvent.click(await screen.findByRole('button', { name: /Continue without an account/ }));
		await screen.findByRole('alert');
		await fireEvent.click(screen.getByRole('button', { name: /Continue without an account/ }));
		await screen.findByLabelText('Or type a path manually'); expect(mocks.create).toHaveBeenCalledOnce();
	});

	it('signs in to an account, waits for another worker without any approval step, and lets setup continue', async () => {
		render(Setup);
		await screen.findByRole('button', { name: 'Sign in' });
		await fireEvent.input(screen.getByLabelText('Name for this worker'), { target: { value: 'Studio laptop' } });
		mocks.post.mockResolvedValue(waiting);
		await fireEvent.click(await screen.findByRole('button', { name: 'Create account' }));
		expect(mocks.post).toHaveBeenCalledWith('/mobile/hub/connection', { name: 'Studio laptop' });
		expect((await screen.findByRole('link', { name: 'Create account in browser' })).getAttribute('href')).toBe(waiting.signup_url);
		// The owner finished in the browser: the account now has a network founded on another worker.
		connection = { ...disconnected, signed_in: true, status: 'connecting' };
		status = { ...fresh(), hub: { linked: true, registered: false, status: 'pending', account_network: { network_id: 'n', name: null, founder_device_id: 'x', devices: [{ device_id: 'x', name: 'Office PC', platform: 'windows', kind: 'worker', status: 'active' }], recovery_available: true } } };
		expect((await screen.findByText(/already syncs settings between/, {}, { timeout: 5000 })).textContent).toContain('Office PC');
		expect(screen.queryByText(/fingerprint/i)).toBeNull();
		expect(screen.queryByText(/Pending approvals/)).toBeNull();
		expect(screen.queryByRole('button', { name: /Use your recovery key/ })).toBeNull();
		await fireEvent.click(screen.getByRole('button', { name: 'Continue setup' }));
		await screen.findByLabelText('Or type a path manually');
		expect(mocks.create).not.toHaveBeenCalled(); expect(mocks.unlock).not.toHaveBeenCalled();
	});

	it('shows the worker as ready once the account admitted it', async () => {
		connection = { ...disconnected, signed_in: true, status: 'connected' };
		status = { ...fresh(), hub: { linked: true, registered: false, status: 'pending' } };
		render(Setup);
		expect(await screen.findByText(/Joining your account's network/)).toBeTruthy();
		status = { ...enrolled(), network: { ...enrolled().network!, founder_device_id: 'other', member_count: 2 }, hub: { linked: true, registered: true, status: 'active' } };
		expect(await screen.findByText(/This worker now syncs settings with/, {}, { timeout: 5000 })).toBeTruthy();
		await fireEvent.click(screen.getByRole('button', { name: 'Continue' }));
		expect((await screen.findByLabelText('Or type a path manually') as HTMLInputElement).value).toBe('D:\\Work');
	});


	it('joins from an invite link without a fingerprint checkbox and keeps this machine local', async () => {
		render(Setup); await fireEvent.click(await screen.findByRole('button', { name: /Use a worker invite/ }));
		const join = screen.getByRole('button', { name: 'Join' }) as HTMLButtonElement;
		expect(join.disabled).toBe(true);
		expect(screen.queryByRole('checkbox')).toBeNull();
		await fireEvent.input(screen.getByLabelText('Invite link or code'), { target: { value: link } });
		expect(join.disabled).toBe(false);
		mocks.enroll.mockImplementation(async () => { status = { ...enrolled(), network: { ...enrolled().network!, founder_device_id: 'other-device', member_count: 2 } }; return { join_id: 'j', status: 'approved', inviter: {}, your_fingerprint: fingerprint }; });
		await fireEvent.click(join);
		expect(mocks.enroll).toHaveBeenCalledWith({ link, device_name: 'My worker' });
		await screen.findByText(/This worker now syncs settings with/);
		await fireEvent.click(screen.getByRole('button', { name: 'Continue' }));
		expect((screen.getByLabelText('Or type a path manually') as HTMLInputElement).value).toBe('D:\\Work');
		expect(mocks.create).not.toHaveBeenCalled(); expect(mocks.goto).not.toHaveBeenCalled();
	});

	it('continues local setup while an admitted join is still copying settings from an offline worker', async () => {
		status = { ...enrolled(), network: { ...enrolled().network!, bootstrap_state: 'syncing' } };
		render(Setup); await screen.findByText(/Copying settings from your other worker/);
		await fireEvent.click(screen.getByRole('button', { name: 'Continue setup' }));
		await continueToProviders(); await fireEvent.click(screen.getByRole('button', { name: 'Finish without a provider' }));
		await waitFor(() => expect(mocks.complete).toHaveBeenCalledOnce());
		expect(mocks.cancel).not.toHaveBeenCalled(); expect(mocks.create).not.toHaveBeenCalled();
	});

	it('keeps local setup available when account sign-in is unavailable', async () => {
		connection = { ...disconnected, status: 'unavailable', error: 'Missing remote connector' };
		render(Setup); await screen.findByRole('button', { name: 'Sign in' });
		await screen.findByText('Missing remote connector');
		expect((screen.getByRole('button', { name: 'Sign in' }) as HTMLButtonElement).disabled).toBe(true);
		await fireEvent.click(screen.getByRole('button', { name: /Continue without an account/ }));
		await screen.findByLabelText('Or type a path manually');
	});
});
