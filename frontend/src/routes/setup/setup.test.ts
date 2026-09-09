import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/svelte';
import type { NetworkStatus } from '$lib/api/network';

const mocks = vi.hoisted(() => ({
	getStatus: vi.fn(), create: vi.fn(), enroll: vi.fn(), cancel: vi.fn(), unlock: vi.fn(),
	get: vi.fn(), post: vi.fn(), delete: vi.fn(), goto: vi.fn(),
	workspace: vi.fn(), saveWorkspace: vi.fn(), setup: vi.fn(), complete: vi.fn(), checkAuth: vi.fn(),
	hasCredential: false, providers: [] as { id: string; name: string; provider_type: string }[]
}));
vi.mock('$app/navigation', () => ({ goto: mocks.goto }));
vi.mock('$lib/api/network', () => ({ getNetworkStatus: mocks.getStatus, createNetwork: mocks.create,
	startEnrollment: mocks.enroll, cancelEnrollment: mocks.cancel, unlockWithRecoveryKey: mocks.unlock }));
vi.mock('$lib/api/client', () => ({ api: { get: mocks.get, post: mocks.post, delete: mocks.delete },
	getErrorMessage: (e: { detail?: string; message?: string }, fallback: string) => e.detail || e.message || fallback }));
vi.mock('$lib/api/auth', () => ({ getWorkspaceConfig: mocks.workspace, setWorkspaceConfig: mocks.saveWorkspace,
	setupInstallation: mocks.setup, completeOnboarding: mocks.complete,
	getClaudeAuthStatus: async () => ({ authenticated: false }), getGithubAuthStatus: async () => ({ authenticated: false }),
	startClaudeLogin: vi.fn(), completeClaudeLogin: vi.fn(), pollClaudeLogin: vi.fn(), startGithubWebLogin: vi.fn(), loginGithubWithToken: vi.fn() }));
vi.mock('$lib/stores/auth.svelte', () => ({
	auth: { checkAuth: mocks.checkAuth, current: { status: { setup_required: true } },
		subscribe(run: (v: unknown) => void) { run({ status: { account_sign_in_available: true } }); return () => {}; } },
	anyCredential: { subscribe(run: (v: boolean) => void) { run(mocks.hasCredential); return () => {}; } }
}));
vi.mock('$lib/stores/providers.svelte', () => ({ providers: { current: { get providers() { return mocks.providers; } }, ensureLoaded: vi.fn(), refreshProviders: vi.fn() } }));
vi.mock('$lib/components/DirectoryBrowser.svelte', () => import('$lib/test-stubs/EmptyRouteDependency.svelte'));
vi.mock('$lib/components/deck/cards/settings/providers/AddProviderModal.svelte', () => import('$lib/test-stubs/EmptyRouteDependency.svelte'));
import Setup from './+page.svelte';

const fingerprint = 'abcd-1234-abcd-1234-abcd-1234-abcd-1234';
const fresh = (): NetworkStatus => ({ device: { device_id: 'local-device', fingerprint }, enrolled: false, platform: 'windows', version: 'test', instance_id: 'local-install' });
const enrolled = (): NetworkStatus => ({ ...fresh(), enrolled: true, network: {
	network_id: 'network', name: 'Home', device_id: 'local-device', fingerprint, founder_device_id: 'local-device',
	generation: 0, key_version: 1, bootstrap_state: 'ready', member_count: 1
} });
const disconnected = { signed_in: false, status: 'disconnected', code: null, qr_svg: null, error: null };
let status: NetworkStatus;

beforeEach(() => {
	vi.resetAllMocks(); status = fresh(); mocks.hasCredential = false; mocks.providers = [];
	mocks.getStatus.mockImplementation(async () => status);
	mocks.create.mockImplementation(async () => { status = enrolled(); return { network: status.network }; });
	mocks.workspace.mockResolvedValue({ workspace_path: 'D:\\Work', default_path: 'D:\\Work', is_local_mode: true });
	mocks.saveWorkspace.mockResolvedValue({}); mocks.checkAuth.mockResolvedValue(undefined);
	mocks.get.mockResolvedValue(disconnected);
});
afterEach(() => { cleanup(); vi.useRealTimers(); });

async function openFresh() {
	render(Setup);
	await fireEvent.click(await screen.findByRole('button', { name: /Start fresh/ }));
	await screen.findByLabelText('Or type a path manually');
}
async function continueToProviders() {
	await fireEvent.click(screen.getByRole('button', { name: 'Continue' }));
	await fireEvent.click(await screen.findByRole('button', { name: 'Skip for now' }));
	await screen.findByRole('button', { name: 'Finish Setup' });
}
async function openAccountSync() {
	render(Setup);
	await fireEvent.click(await screen.findByRole('button', { name: /Sync settings from an existing worker/ }));
	await fireEvent.click(screen.getByRole('button', { name: 'Find it through my Shuffle account' }));
}

describe('local worker onboarding', () => {
	it.each([false, true])('configures model providers only in Providers while tool connections remain optional (existing credential: %s)', async existingCredential => {
		mocks.hasCredential = existingCredential;
		if (existingCredential) mocks.providers = [{ id: 'openai', name: 'My OpenAI account', provider_type: 'openai' }];
		await openFresh();
		await fireEvent.click(screen.getByRole('button', { name: 'Continue' }));
		await screen.findByRole('button', { name: 'Connect Claude Code' });
		expect(screen.getByText('GitHub', { exact: true })).toBeTruthy();
		expect(screen.queryByRole('button', { name: /Add.*provider/i })).toBeNull();
		expect(screen.queryByText('My OpenAI account')).toBeNull();
		await fireEvent.click(screen.getByRole('button', { name: existingCredential ? 'Continue' : 'Skip for now' }));
		expect(screen.getAllByRole('button', { name: 'Add provider' })).toHaveLength(1);
		if (existingCredential) expect(screen.getByText('My OpenAI account')).toBeTruthy();
		expect(screen.queryByRole('button', { name: 'Connect Claude Code' })).toBeNull();
		await fireEvent.click(screen.getByRole('button', { name: 'Finish Setup' }));
		await waitFor(() => expect(mocks.complete).toHaveBeenCalledOnce());
		expect(mocks.post).not.toHaveBeenCalled(); expect(mocks.get).not.toHaveBeenCalled();
	});

	it('completes a standalone installation without account, remote, recovery or provider calls', async () => {
		await openFresh(); await continueToProviders();
		await fireEvent.click(screen.getByRole('button', { name: 'Finish Setup' }));
		await waitFor(() => expect(mocks.goto).toHaveBeenCalledWith('/'));
		expect(mocks.create).toHaveBeenCalledExactlyOnceWith();
		expect(mocks.saveWorkspace).toHaveBeenCalledWith('D:\\Work');
		expect(mocks.setup).toHaveBeenCalledOnce(); expect(mocks.complete).toHaveBeenCalledOnce();
		expect(mocks.get).not.toHaveBeenCalled(); expect(mocks.post).not.toHaveBeenCalled();
		expect(mocks.enroll).not.toHaveBeenCalled(); expect(mocks.unlock).not.toHaveBeenCalled();
	});

	it('resumes initialized settings and saved workspace after closing unfinished setup', async () => {
		await openFresh(); cleanup(); render(Setup);
		expect((await screen.findByLabelText('Or type a path manually') as HTMLInputElement).value).toBe('D:\\Work');
		expect(screen.queryByRole('button', { name: /Start fresh/ })).toBeNull();
		await continueToProviders(); await fireEvent.click(screen.getByRole('button', { name: 'Finish Setup' }));
		await waitFor(() => expect(mocks.complete).toHaveBeenCalledOnce());
		expect(mocks.create).toHaveBeenCalledOnce();
	});

	it('retries local initialization after a failed response without recreating persisted settings', async () => {
		mocks.create.mockImplementationOnce(async () => { status = enrolled(); throw new Error('Response lost'); });
		render(Setup); await fireEvent.click(await screen.findByRole('button', { name: /Start fresh/ }));
		await screen.findByRole('alert');
		await fireEvent.click(screen.getByRole('button', { name: /Start fresh/ }));
		await screen.findByLabelText('Or type a path manually'); expect(mocks.create).toHaveBeenCalledOnce();
	});

	it.each(['unavailable', 'locked'])('lets an optional account %s error be skipped and setup finish', async connectionStatus => {
		mocks.get.mockResolvedValue({ ...disconnected, status: connectionStatus, error: 'Remote connection unavailable' });
		await openFresh(); await continueToProviders();
		await fireEvent.click(screen.getByRole('button', { name: 'Sign in or create a Shuffle account (optional)' }));
		await screen.findByRole('alert');
		await fireEvent.click(screen.getByRole('button', { name: 'Skip account for now' }));
		expect(screen.queryByRole('alert')).toBeNull();
		await fireEvent.click(screen.getByRole('button', { name: 'Back' }));
		await fireEvent.click(screen.getByRole('button', { name: 'Skip for now' }));
		await fireEvent.click(screen.getByRole('button', { name: 'Finish Setup' }));
		await waitFor(() => expect(mocks.complete).toHaveBeenCalledOnce());
		expect(mocks.post).not.toHaveBeenCalled(); expect(mocks.create).toHaveBeenCalledOnce();
	});

	it('keeps a rejected optional sign-in scoped to the account panel', async () => {
		mocks.post.mockRejectedValue(new Error('Sign-in service unavailable'));
		await openFresh(); await continueToProviders();
		await fireEvent.click(screen.getByRole('button', { name: 'Sign in or create a Shuffle account (optional)' }));
		await fireEvent.click(await screen.findByRole('button', { name: 'Log in' }));
		await screen.findByRole('alert');
		await fireEvent.click(screen.getByRole('button', { name: 'Finish Setup' }));
		await waitFor(() => expect(mocks.complete).toHaveBeenCalledOnce());
	});

	it('lets a first account with no approving worker continue with fresh settings', async () => {
		mocks.get.mockResolvedValue({ ...disconnected, signed_in: true });
		await openAccountSync();
		await screen.findByText('Approve settings sync on an existing worker');
		await fireEvent.click(screen.getByRole('button', { name: 'Use fresh settings instead' }));
		await screen.findByLabelText('Or type a path manually');
		expect(mocks.create).toHaveBeenCalledOnce(); expect(mocks.unlock).not.toHaveBeenCalled();
	});

	it('allows back and fresh setup when account access cannot start', async () => {
		mocks.get.mockResolvedValue({ ...disconnected, status: 'unavailable', error: 'Missing remote connector' });
		await openAccountSync(); await screen.findByText('Missing remote connector');
		await fireEvent.click(screen.getByRole('button', { name: 'Back' }));
		await fireEvent.click(screen.getByRole('button', { name: 'Back' }));
		expect(screen.queryByText('Missing remote connector')).toBeNull();
		await fireEvent.click(screen.getByRole('button', { name: /Start fresh/ }));
		await screen.findByLabelText('Or type a path manually');
	});

	it('requires a complete compared fingerprint for settings sync and keeps this machine local', async () => {
		render(Setup); await fireEvent.click(await screen.findByRole('button', { name: /Sync settings from an existing worker/ }));
		await fireEvent.input(screen.getByLabelText('Worker address'), { target: { value: 'https://existing.example' } });
		await fireEvent.input(screen.getByLabelText('Join code'), { target: { value: 'ABCD-EFGH-JKLM' } });
		const submit = screen.getByRole('button', { name: 'Sync settings' }) as HTMLButtonElement;
		expect(submit.disabled).toBe(true);
		await fireEvent.input(screen.getByLabelText(/Inviter fingerprint \(read/), { target: { value: fingerprint } });
		expect(submit.disabled).toBe(true);
		await fireEvent.click(screen.getByRole('checkbox'));
		mocks.enroll.mockImplementation(async () => { status = { ...enrolled(), network: { ...enrolled().network!, founder_device_id: 'other-device', member_count: 2 } }; return { inviter: {} }; });
		await fireEvent.click(submit);
		await screen.findByText(/This local worker now syncs/);
		expect(screen.getByText(/Workspace folder — where checkouts live on this machine/)).toBeTruthy();
		expect(screen.getByText(/Claude Code and GitHub logins are per machine/)).toBeTruthy();
		await fireEvent.click(screen.getByRole('button', { name: 'Continue' }));
		expect((screen.getByLabelText('Or type a path manually') as HTMLInputElement).value).toBe('D:\\Work');
		expect(mocks.enroll).toHaveBeenCalledWith({ endpoint_url: 'https://existing.example', code: 'ABCD-EFGH-JKLM', inviter_fingerprint: fingerprint });
		expect(mocks.create).not.toHaveBeenCalled(); expect(mocks.goto).not.toHaveBeenCalled();
	});

	it('continues local setup while an approved sync is unfinished and its remote worker is offline', async () => {
		status = { ...enrolled(), network: { ...enrolled().network!, bootstrap_state: 'syncing' } };
		render(Setup); await screen.findByText(/Settings sync is approved/);
		await fireEvent.click(screen.getByRole('button', { name: 'Continue local setup' }));
		await continueToProviders(); await fireEvent.click(screen.getByRole('button', { name: 'Finish Setup' }));
		await waitFor(() => expect(mocks.complete).toHaveBeenCalledOnce());
		expect(status.network?.bootstrap_state).toBe('syncing');
		expect(mocks.cancel).not.toHaveBeenCalled(); expect(mocks.create).not.toHaveBeenCalled();
	});
});
