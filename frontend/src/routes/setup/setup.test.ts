import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/svelte';
import type { NetworkStatus } from '$lib/api/network';

const mocks = vi.hoisted(() => ({
	getStatus: vi.fn(), create: vi.fn(), enroll: vi.fn(), cancel: vi.fn(),
	get: vi.fn(), post: vi.fn(), patch: vi.fn(), delete: vi.fn(), goto: vi.fn(),
	workspace: vi.fn(), saveWorkspace: vi.fn(), setup: vi.fn(), complete: vi.fn(), checkAuth: vi.fn(),
	githubStatus: vi.fn(), githubWeb: vi.fn(), githubToken: vi.fn(),
	ownerStatus: null as Record<string, unknown> | null, ownerRefresh: vi.fn(),
	authStatus: {} as Record<string, unknown>,
	getMode: vi.fn(), setMode: vi.fn(), ready: vi.fn(), keyState: vi.fn(), saveKey: vi.fn(),
	license: vi.fn(), agent: vi.fn(), run: vi.fn(),
}));
vi.mock('$app/navigation', () => ({ goto: mocks.goto }));
vi.mock('$lib/api/network', () => ({ getNetworkStatus: mocks.getStatus, createNetwork: mocks.create,
	startEnrollment: mocks.enroll, cancelEnrollment: mocks.cancel }));
vi.mock('$lib/api/client', () => ({ api: { get: mocks.get, post: mocks.post, patch: mocks.patch, delete: mocks.delete },
	isApiError: (e: unknown) => !!e && typeof e === 'object' && 'status' in e,
	getErrorMessage: (e: { detail?: string; message?: string }, fallback: string) => e?.detail || e?.message || fallback }));
vi.mock('$lib/api/auth', () => ({ getWorkspaceConfig: mocks.workspace, setWorkspaceConfig: mocks.saveWorkspace,
	setupInstallation: mocks.setup, completeOnboarding: mocks.complete,
	getGithubAuthStatus: mocks.githubStatus, startGithubWebLogin: mocks.githubWeb, loginGithubWithToken: mocks.githubToken, cancelGithubWebLogin: vi.fn(), logoutGithub: vi.fn() }));
vi.mock('$lib/stores/auth.svelte', () => ({
	auth: { checkAuth: mocks.checkAuth, get current() { return { status: mocks.authStatus }; }, logout: vi.fn(),
		subscribe(run: (v: unknown) => void) { run({ status: mocks.authStatus }); return () => {}; } },
	isAdmin: { subscribe(run: (v: boolean) => void) { run(true); return () => {}; } },
}));
vi.mock('$lib/stores/owner-access.svelte', () => ({ ownerAccess: { get status() { return mocks.ownerStatus; }, refresh: mocks.ownerRefresh } }));
vi.mock('$lib/api/agent-credentials', async (importOriginal) => ({
	...(await importOriginal<typeof import('$lib/api/agent-credentials')>()),
	getClaudeAuthMode: mocks.getMode, setClaudeAuthMode: mocks.setMode, getAgentCredentials: mocks.ready,
	getAnthropicKeyState: mocks.keyState, saveAnthropicApiKey: mocks.saveKey,
}));
vi.mock('$lib/api/license', async (importOriginal) => ({
	...(await importOriginal<typeof import('$lib/api/license')>()), getLicense: mocks.license,
}));
vi.mock('$lib/services/first-agent-run', async (importOriginal) => ({
	...(await importOriginal<typeof import('$lib/services/first-agent-run')>()),
	ensureStarterAgent: mocks.agent, startFirstAgentRun: mocks.run,
}));
vi.mock('$lib/components/DirectoryBrowser.svelte', () => import('$lib/test-stubs/EmptyRouteDependency.svelte'));
vi.mock('$lib/components/settings/ClaudeCliPanel.svelte', () => import('$lib/test-stubs/EmptyRouteDependency.svelte'));
import Setup from './+page.svelte';

const fingerprint = 'abcd-1234-abcd-1234-abcd-1234-abcd-1234';
const fresh = (): NetworkStatus => ({ device: { device_id: 'local-device', fingerprint }, enrolled: false, platform: 'linux', version: 'test', instance_id: 'local-install', enrollment: null, hub: { linked: false } });
const enrolled = (): NetworkStatus => ({ ...fresh(), enrollment: undefined, enrolled: true, network: {
	network_id: 'network', name: 'Home', device_id: 'local-device', fingerprint, founder_device_id: 'local-device',
	generation: 0, key_version: 1, bootstrap_state: 'ready', member_count: 1
}, hub: { linked: false } });
const disconnected = { signed_in: false, status: 'disconnected', code: null, qr_svg: null, error: null };
const waiting = { ...disconnected, status: 'waiting', code: 'TESTONLYCODE', authorization_url: 'https://account.example.com/claim?code=TESTONLYCODE', signup_url: 'https://account.example.com/claim?code=TESTONLYCODE&screen=signup' };
const link = 'shuffle://network/join?endpoint=http%3A%2F%2F10.0.0.5%3A8000&code=ABCD-EFGH-JKLM&fp=' + fingerprint;
const dockerOwner = { password_set: true, setup_allowed: false, required: true, trusted_lan: false, login_available: true, desktop_handoff: false, min_length: 12 };
const beta = { mode: 'beta', state: 'beta', allowed: true, reason: null, banner: null, plan: 'beta', seats: null, seats_used: null,
	account_id: null, license_id: null, key_hint: null, install_id: null, issued_at: null, expires_at: null, grace: false,
	grace_until: null, last_refresh_at: null, next_refresh_at: null, last_error: null, server_status: null, server_configured: false };
let status: NetworkStatus;
let connection: Record<string, unknown>;

beforeEach(() => {
	vi.resetAllMocks();
	try { sessionStorage.clear(); } catch { /* not available */ }
	status = fresh(); connection = disconnected;
	mocks.ownerStatus = dockerOwner;
	mocks.authStatus = { setup_required: true, auth_mode: 'local', claude_cli: { installed: true, signed_in: false, email: 'owner@example.com', login: { surface: 'docker' } } };
	vi.spyOn(window, 'open').mockReturnValue(null);
	mocks.getStatus.mockImplementation(async () => status);
	mocks.create.mockImplementation(async () => { status = enrolled(); return { network: status.network }; });
	mocks.workspace.mockResolvedValue({ workspace_path: '/workspace', default_path: '/workspace', is_local_mode: false, is_configured: false, exists: true });
	mocks.saveWorkspace.mockResolvedValue({}); mocks.checkAuth.mockResolvedValue(undefined); mocks.complete.mockResolvedValue(undefined);
	mocks.githubStatus.mockResolvedValue({ authenticated: false, user: null });
	mocks.getMode.mockResolvedValue({ supported: true, mode: null });
	mocks.setMode.mockImplementation(async (mode: string) => ({ supported: true, mode }));
	mocks.ready.mockImplementation(async (mode: string) => ({ ready: false, mode, reason: 'The Claude Code CLI is not signed in.', source: 'gate' }));
	mocks.keyState.mockResolvedValue({ set: false, masked: '' });
	mocks.license.mockResolvedValue(beta);
	mocks.agent.mockResolvedValue({ id: 'assistant', name: 'Assistant' });
	mocks.run.mockImplementation(({ onUpdate }: { onUpdate: (s: unknown) => void }) => {
		onUpdate({ phase: 'streaming', text: 'Hello! I can help you', sessionId: 's1', error: null });
		const final = { phase: 'done', text: 'Hello! I can help you write code.', sessionId: 's1', error: null };
		onUpdate(final);
		return { done: Promise.resolve(final), cancel: vi.fn() };
	});
	mocks.get.mockImplementation(async (path: string) => {
		if (path === '/mobile/hub/connection') return connection;
		if (path === '/mobile/hub/account') return { account: { id: 'a', username: 'owner', profile_complete: true }, servers: [] };
		throw new Error('Unexpected GET ' + path);
	});
});
afterEach(() => { cleanup(); vi.useRealTimers(); });

const click = (name: string | RegExp) => fireEvent.click(screen.getByRole('button', { name }));
async function throughWelcome() {
	render(Setup);
	await screen.findByTestId('setup-welcome');
	await click('Continue');
	await screen.findByText('Beta: no license needed');
}
async function throughLicense() {
	await throughWelcome();
	await click('Continue');
	await screen.findByTestId('setup-mode');
}
async function throughMode(mode: 'subscription' | 'api_key' = 'subscription') {
	await throughLicense();
	await fireEvent.click(screen.getByLabelText(mode === 'subscription' ? /Claude subscription/ : /API key/));
	await click('Continue');
	await screen.findByTestId('setup-credential');
}
async function throughCredential() {
	await throughMode();
	mocks.ready.mockResolvedValue({ ready: true, mode: 'subscription', reason: null, source: 'gate' });
	await click(/Check again/);
	await screen.findByTestId('setup-credential-ready');
	await click('Continue');
	await screen.findByTestId('setup-workspace');
}
async function throughFirstRun() {
	await throughCredential();
	await click('Continue');
	await screen.findByTestId('setup-first-run');
	await click(/Run it/);
	await screen.findByText(/It works/);
	await click('Continue');
	await screen.findByTestId('setup-extras');
}

describe('setup wizard v2', () => {
	it('shows the eight steps in order and the disclosures first', async () => {
		render(Setup);
		await screen.findByTestId('setup-welcome');
		const progress = screen.getByRole('navigation', { name: 'Setup progress' });
		const labels = [...progress.querySelectorAll('li')].map((li) => li.textContent?.replace('Completed', '').replace(/^\d+/, '').trim());
		expect(labels).toEqual(['Welcome', 'Password', 'License', 'Mode', 'Sign in', 'Workspace', 'First run', 'Account & GitHub']);
		expect(screen.getByText('Agents act with your access')).toBeTruthy();
		expect(screen.getByText(/never pays for, resells or shares model usage/)).toBeTruthy();
		expect(screen.getByRole('link', { name: /What is sent/ }).getAttribute('href')).toBe('https://hobbycoders.com/privacy#updates');
		// The password was set at the owner gate before this page could load.
		expect(progress.querySelectorAll('li')[1].className).toContain('complete');
	});

	it('leaves the password step out on the desktop app', async () => {
		mocks.ownerStatus = { ...dockerOwner, password_set: false, required: false, desktop_handoff: true };
		mocks.authStatus = { ...mocks.authStatus, claude_cli: { installed: true, signed_in: false, login: { surface: 'desktop' } } };
		mocks.workspace.mockResolvedValue({ workspace_path: 'C:\\Users\\me\\AI Shuffle', default_path: 'C:\\Users\\me\\AI Shuffle', is_local_mode: true, is_configured: false, exists: true });
		render(Setup);
		await screen.findByTestId('setup-welcome');
		expect(screen.getByRole('navigation', { name: 'Setup progress' }).textContent).not.toContain('Password');
		expect(screen.getByText(/Agents run as your user on this computer/)).toBeTruthy();
	});

	it('starts at the license once the owner gate showed the welcome', async () => {
		sessionStorage.setItem('ai-shuffle.setup.welcome-seen', '1');
		render(Setup);
		expect(await screen.findByText('Beta: no license needed')).toBeTruthy();
		expect(screen.queryByTestId('setup-welcome')).toBeNull();
	});

	it('stores the mode, with the API key advice for unattended runs', async () => {
		await throughLicense();
		expect(screen.getByTestId('setup-mode-guidance').textContent).toContain('Use an API key for scheduled and unattended runs');
		expect((screen.getByRole('button', { name: 'Continue' }) as HTMLButtonElement).disabled).toBe(true);
		await fireEvent.click(screen.getByLabelText(/API key/));
		await click('Continue');
		expect(mocks.setMode).toHaveBeenCalledWith('api_key');
		await screen.findByLabelText('Anthropic API key');
	});

	it('blocks the credential step until the credential exists, with no way to finish without one', async () => {
		await throughMode();
		const missing = await screen.findByTestId('setup-credential-missing');
		expect(missing.textContent).toContain('Agents cannot start yet');
		expect((screen.getByRole('button', { name: 'Continue' }) as HTMLButtonElement).disabled).toBe(true);
		expect(screen.queryByText(/without a provider/)).toBeNull();
		expect(screen.queryByRole('button', { name: /Skip/ })).toBeNull();
	});

	it('shows the host folder behind /workspace on Docker and Unraid', async () => {
		await throughCredential();
		const host = screen.getByTestId('setup-workspace-host');
		expect(host.textContent).toContain('/workspace');
		expect(host.textContent).toContain('WORKSPACE_DIR');
		expect(host.textContent).toContain('/mnt/user/appdata/ai-shuffle/workspace');
		await fireEvent.input(screen.getByLabelText('Or type a path manually'), { target: { value: '/tmp/projects' } });
		expect(screen.getByText(/lost when the container is re-created/)).toBeTruthy();
	});

	it('names the host folder when the worker reports it', async () => {
		mocks.workspace.mockResolvedValue({ workspace_path: '/workspace', default_path: '/workspace', is_local_mode: false, is_configured: false, exists: true, host_path: '/mnt/user/projects' });
		await throughCredential();
		expect(screen.getByTestId('setup-workspace-host').textContent).toContain('/mnt/user/projects');
	});

	it('runs a first agent, streams the reply, and finishes as a clean install', async () => {
		await throughCredential();
		expect(mocks.saveWorkspace).not.toHaveBeenCalled();
		await click('Continue');
		expect(mocks.saveWorkspace).toHaveBeenCalledWith('/workspace');
		expect(mocks.setup).toHaveBeenCalledOnce();
		await screen.findByTestId('setup-first-run');
		expect(screen.getByText(/paid for by your Claude plan \(owner@example.com\)/)).toBeTruthy();
		await click(/Run it/);
		expect(mocks.run).toHaveBeenCalledWith(expect.objectContaining({ profileId: 'assistant' }));
		expect((await screen.findByTestId('setup-first-run-reply')).textContent).toContain('Hello! I can help you write code.');
		await click('Continue');
		await screen.findByTestId('setup-extras');
		expect(await screen.findByRole('button', { name: 'Sign in with GitHub' })).toBeTruthy();
		await click('Finish');
		await waitFor(() => expect(mocks.goto).toHaveBeenCalledWith('/'));
		expect(mocks.create).toHaveBeenCalledOnce(); expect(mocks.complete).toHaveBeenCalledOnce();
		expect(mocks.enroll).not.toHaveBeenCalled();
	});

	it('explains a failed first run and lets the owner retry or continue', async () => {
		mocks.run.mockImplementation(({ onUpdate }: { onUpdate: (s: unknown) => void }) => {
			const final = { phase: 'failed', text: '', sessionId: null, error: 'New agent runs are paused until you add a license key.' };
			onUpdate(final);
			return { done: Promise.resolve(final), cancel: vi.fn() };
		});
		await throughCredential();
		await click('Continue');
		await screen.findByTestId('setup-first-run');
		await click(/Run it/);
		expect((await screen.findByRole('alert')).textContent).toContain('New agent runs are paused');
		expect(screen.getByRole('button', { name: /Try again/ })).toBeTruthy();
		await click('Continue anyway');
		await screen.findByTestId('setup-extras');
	});

	it('connects GitHub with a token in the last step', async () => {
		mocks.githubToken.mockResolvedValue({ success: true });
		mocks.githubStatus.mockResolvedValueOnce({ authenticated: false, user: null }).mockResolvedValue({ authenticated: true, user: 'octocat' });
		await throughFirstRun();
		await fireEvent.click(await screen.findByRole('button', { name: 'Personal access token' }));
		await fireEvent.input(screen.getByLabelText('GitHub personal access token'), { target: { value: 'ghp_secret' } });
		await click('Connect token');
		expect(mocks.githubToken).toHaveBeenCalledWith('ghp_secret');
		expect(await screen.findByText('GitHub is connected as @octocat.')).toBeTruthy();
	});

	it('signs in to an account in the last step and finishes without creating a network of its own', async () => {
		await throughFirstRun();
		await screen.findByRole('button', { name: 'Sign in' });
		await fireEvent.input(screen.getByLabelText('Name for this worker'), { target: { value: 'Studio laptop' } });
		mocks.post.mockResolvedValue(waiting);
		await click('Create account');
		expect(mocks.post).toHaveBeenCalledWith('/mobile/hub/connection', { name: 'Studio laptop' });
		connection = { ...disconnected, signed_in: true, status: 'connecting' };
		status = { ...fresh(), hub: { linked: true, registered: false, status: 'pending', account_network: { network_id: 'n', name: null, founder_device_id: 'x', devices: [{ device_id: 'x', name: 'Office PC', platform: 'windows', kind: 'worker', status: 'active' }], recovery_available: true } } };
		expect((await screen.findByText(/already syncs settings between/, {}, { timeout: 5000 })).textContent).toContain('Office PC');
		await click('Finish');
		await waitFor(() => expect(mocks.complete).toHaveBeenCalledOnce());
		expect(mocks.create).not.toHaveBeenCalled();
	});

	it('joins from an invite in the last step', async () => {
		await throughFirstRun();
		await click(/Use a worker invite instead/);
		const join = screen.getByRole('button', { name: 'Join' }) as HTMLButtonElement;
		expect(join.disabled).toBe(true);
		await fireEvent.input(screen.getByLabelText('Invite link or code'), { target: { value: link } });
		mocks.enroll.mockImplementation(async () => { status = { ...enrolled(), network: { ...enrolled().network!, founder_device_id: 'other-device', member_count: 2 } }; return { join_id: 'j', status: 'approved', inviter: {}, your_fingerprint: fingerprint }; });
		await fireEvent.click(join);
		expect(mocks.enroll).toHaveBeenCalledWith({ link, device_name: 'My worker' });
		await screen.findByText(/This worker now syncs settings with/);
		await click('Finish');
		await waitFor(() => expect(mocks.complete).toHaveBeenCalledOnce());
		expect(mocks.create).not.toHaveBeenCalled();
	});

	it('skips the account section for a worker that already belongs to a network', async () => {
		status = enrolled();
		await throughFirstRun();
		expect(screen.queryByRole('button', { name: 'Create account' })).toBeNull();
		await click('Finish');
		await waitFor(() => expect(mocks.complete).toHaveBeenCalledOnce());
		expect(mocks.create).not.toHaveBeenCalled();
	});
});
