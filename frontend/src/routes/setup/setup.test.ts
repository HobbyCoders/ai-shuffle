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
	setMode: vi.fn(), creds: vi.fn(), saveKey: vi.fn(),
	license: vi.fn(), agent: vi.fn(), run: vi.fn(),
	providerStore: { providers: [] as unknown[], claude: null, anthropicKeyMasked: null, falKeySet: false, falKeyMasked: '', falEnabled: true },
	registry: [] as unknown[], defaultModel: 'sonnet',
	cliConversations: [] as unknown[],
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
vi.mock('$lib/stores/providers.svelte', () => ({
	providers: { get current() { return mocks.providerStore; }, refreshAll: async () => {}, refreshProviders: async () => {} },
}));
vi.mock('$lib/constants/models.svelte', async (importOriginal) => ({
	...(await importOriginal<typeof import('$lib/constants/models.svelte')>()),
	hydrateModelRegistry: async () => true, getProviders: () => mocks.registry, getDefaultModel: () => mocks.defaultModel,
}));
vi.mock('$lib/stores/owner-access.svelte', () => ({ ownerAccess: { get status() { return mocks.ownerStatus; }, refresh: mocks.ownerRefresh } }));
vi.mock('$lib/api/agent-credentials', async (importOriginal) => ({
	...(await importOriginal<typeof import('$lib/api/agent-credentials')>()),
	setClaudeAuthMode: mocks.setMode, getAgentCredentials: mocks.creds, saveAnthropicApiKey: mocks.saveKey,
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
import type { AgentCredentials, ClaudeAuthMode } from '$lib/api/agent-credentials';
import type { AgentGateStatus } from '$lib/api/client';

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
const beta = { mode: 'cloud', state: 'not_linked', linked: false, remote_worker_limit: 0, lease_state: 'none', message: null,
	allowed: true, reason: null, banner: null, plan: null, seats: 0, seats_used: null,
	account_id: null, license_id: null, key_hint: null, install_id: null, issued_at: null, expires_at: null, grace: false,
	grace_until: null, last_refresh_at: null, next_refresh_at: null, last_error: null, server_status: null, server_configured: false };
let status: NetworkStatus;
let connection: Record<string, unknown>;
let workerMode: ClaudeAuthMode;

const NOT_SIGNED_IN = 'Agents cannot start: Claude Code is set to subscription mode and the Claude Code CLI is not signed in.';
/** The wizard's view of `GET /settings/claude-auth` (see agent-credentials.ts). */
function creds(over: Partial<AgentCredentials> = {}): AgentCredentials {
	return {
		mode: workerMode, modeLabel: workerMode === 'api_key' ? 'Anthropic API key' : 'Claude subscription', explicit: true,
		ready: false, credentialReady: false, licenseAllowsRuns: true, reason: NOT_SIGNED_IN,
		cli: { installed: true, signed_in: false, email: null, org_name: null, plan: null, auth_method_label: null, error: null },
		apiKey: { set: false, last4: null },
		...over,
	};
}
/** The worker's gate as `/auth/status` reports it, with the providers that are ready. */
const gate = (ready: string[]): AgentGateStatus => ({
	ready: ready.length > 0, mode: 'subscription', claude_credential_ready: ready.includes('claude'),
	any_provider_ready: ready.length > 0, ready_providers: ready,
});

beforeEach(() => {
	vi.resetAllMocks();
	try { sessionStorage.clear(); } catch { /* not available */ }
	status = fresh(); connection = disconnected; workerMode = 'subscription';
	mocks.ownerStatus = dockerOwner;
	mocks.authStatus = { setup_required: true, auth_mode: 'local', claude_cli: { installed: true, signed_in: false, login: { surface: 'docker' } }, agent_gate: gate([]) };
	mocks.providerStore = { providers: [], claude: null, anthropicKeyMasked: null, falKeySet: false, falKeyMasked: '', falEnabled: true };
	mocks.registry = []; mocks.defaultModel = 'sonnet'; mocks.cliConversations = [];
	vi.spyOn(window, 'open').mockReturnValue(null);
	mocks.getStatus.mockImplementation(async () => status);
	mocks.create.mockImplementation(async () => { status = enrolled(); return { network: status.network }; });
	mocks.workspace.mockResolvedValue({ workspace_path: '/workspace', default_path: '/workspace', is_local_mode: false, is_configured: false, exists: true });
	mocks.saveWorkspace.mockResolvedValue({}); mocks.checkAuth.mockImplementation(async () => mocks.authStatus); mocks.complete.mockResolvedValue(undefined);
	mocks.githubStatus.mockResolvedValue({ authenticated: false, user: null });
	mocks.setMode.mockImplementation(async (mode: ClaudeAuthMode) => { workerMode = mode; return creds(); });
	mocks.creds.mockImplementation(async () => creds());
	mocks.license.mockResolvedValue(beta);
	mocks.agent.mockResolvedValue({ id: 'assistant', name: 'Assistant' });
	mocks.run.mockImplementation(({ onUpdate }: { onUpdate: (s: unknown) => void }) => {
		onUpdate({ phase: 'streaming', text: 'Hello! I can help you', sessionId: 's1', error: null, code: null });
		const final = { phase: 'done', text: 'Hello! I can help you write code.', sessionId: 's1', error: null, code: null };
		onUpdate(final);
		return { done: Promise.resolve(final), cancel: vi.fn() };
	});
	mocks.get.mockImplementation(async (path: string) => {
		if (path === '/mobile/hub/connection') return connection;
		if (path === '/mobile/hub/account') return { account: { id: 'a', username: 'owner', profile_complete: true }, servers: [] };
		if (path.startsWith('/sessions/import/cli-history')) return { conversations: mocks.cliConversations };
		throw new Error('Unexpected GET ' + path);
	});
});
afterEach(() => { cleanup(); vi.useRealTimers(); });

const click = (name: string | RegExp) => fireEvent.click(screen.getByRole('button', { name }));
async function throughWelcome() {
	render(Setup);
	await screen.findByTestId('setup-welcome');
	await click('Continue');
	await screen.findByText('Local use is free');
}
async function throughLicense() {
	await throughWelcome();
	await click('Continue');
	await screen.findByTestId('setup-provider');
}
/** A ChatGPT subscription row with a picked model (the wizard needs one to call OpenAI connected). */
function openaiRow() {
	mocks.providerStore = { ...mocks.providerStore, providers: [{ id: 'codex', name: 'ChatGPT', provider_type: 'custom', auth_mode: 'chatgpt', enabled: true, codex_access_token_set: true, models: [{ id: 'gpt-5', label: 'GPT-5' }] }] };
	mocks.registry = [{ id: 'codex', name: 'ChatGPT', provider_type: 'custom', auth_kind: 'subscription', plan_type: null, models: [{ id: 'gpt-5', label: 'GPT-5' }], default_model: 'gpt-5' }];
}
/** OpenAI (a ChatGPT subscription row) is the ready provider unless the test says otherwise. */
function openaiReady() {
	mocks.authStatus = { ...mocks.authStatus, agent_gate: gate(['openai']) };
	openaiRow();
}
async function throughProvider() {
	// The row exists from the start; the worker reports it ready on Check again.
	openaiRow();
	await throughLicense();
	openaiReady();
	await click(/Check again/);
	await screen.findByTestId('setup-provider-ready');
	await click('Continue');
	await screen.findByTestId('setup-workspace');
}
async function throughFirstRun() {
	await throughProvider();
	await click('Continue');
	await screen.findByTestId('setup-first-run');
	await click(/Run it/);
	await screen.findByText(/It works/);
	await click('Continue');
	await screen.findByTestId('setup-extras');
}
/** Opens one of the last step's optional rows; they all start collapsed. */
async function openExtra(id: 'account' | 'github' | 'history') {
	const row = screen.getByTestId(`setup-extra-${id}`);
	const toggle = row.querySelector('button[aria-expanded]') as HTMLButtonElement;
	if (toggle.getAttribute('aria-expanded') !== 'true') await fireEvent.click(toggle);
	return row;
}
const summary = (id: string) => screen.getByTestId(`setup-extra-${id}-summary`).textContent ?? '';
const convo = (i: number) => ({ jsonl_path: `/home/u/.claude/projects/app/${i}.jsonl`, sdk_session_id: `s${i}`, title: `Conversation ${i}`,
	message_count: 4, modified_at: 1_790_000_000, cwd: '/work/app', project_id: null });

describe('setup wizard v2', () => {
	it('shows the seven steps in order and the disclosures first', async () => {
		render(Setup);
		await screen.findByTestId('setup-welcome');
		const progress = screen.getByRole('navigation', { name: 'Setup progress' });
		const labels = [...progress.querySelectorAll('li')].map((li) => li.textContent?.replace('Completed', '').replace(/^\d+/, '').trim());
		expect(labels).toEqual(['Welcome', 'Password', 'Cloud', 'AI provider', 'Workspace', 'First run', 'Account & GitHub']);
		expect(screen.getByText('Agents act with your access')).toBeTruthy();
		expect(screen.getByText(/never pays for, resells or shares model usage/)).toBeTruthy();
		expect(screen.getByTestId('setup-welcome').textContent).not.toMatch(/Claude|Anthropic/);
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
		expect(await screen.findByText('Local use is free')).toBeTruthy();
		expect(screen.queryByTestId('setup-welcome')).toBeNull();
	});

	it('marks each finished step done in the progress bar', async () => {
		await throughLicense();
		const items = screen.getByRole('navigation', { name: 'Setup progress' }).querySelectorAll('li');
		expect([...items].slice(0, 4).map((li) => li.className.includes('complete'))).toEqual([true, true, true, false]);
		expect(items[3].getAttribute('aria-current')).toBe('step');
	});

	it('offers every provider equally and names no engine in the subtitle', async () => {
		await throughLicense();
		expect(screen.getByText('Connect at least one AI provider. You can add more later in Settings > Providers.')).toBeTruthy();
		const list = screen.getByRole('list', { name: 'AI providers' });
		for (const name of ['Claude', 'OpenAI', 'xAI Grok', 'OpenRouter', 'Your own gateway']) expect(list.textContent).toContain(name);
		expect(screen.queryByText(/Claude Code/)).toBeNull();
	});

	it('blocks the provider step until a provider is ready, with no way to skip', async () => {
		await throughLicense();
		expect((await screen.findByTestId('setup-provider-missing')).textContent).toContain('Connect at least one provider');
		expect((screen.getByRole('button', { name: 'Continue' }) as HTMLButtonElement).disabled).toBe(true);
		expect(screen.queryByRole('button', { name: /Skip/ })).toBeNull();
	});

	it('keeps the Claude subscription or key choice inside the Claude provider', async () => {
		await throughLicense();
		await fireEvent.click(screen.getByTestId('setup-provider-claude'));
		await fireEvent.click(await screen.findByLabelText(/API key/));
		expect(mocks.setMode).toHaveBeenCalledWith('api_key');
		mocks.saveKey.mockImplementation(async () => {
			workerMode = 'api_key';
			mocks.creds.mockImplementation(async () => creds({ ready: true, credentialReady: true, reason: null, apiKey: { set: true, last4: 'abcd' } }));
			mocks.authStatus = { ...mocks.authStatus, agent_gate: gate(['claude']) };
			return { masked: 'sk-ant-...abcd' };
		});
		await fireEvent.input(await screen.findByLabelText('Anthropic API key'), { target: { value: 'sk-ant-secret' } });
		await click('Save key');
		expect(mocks.saveKey).toHaveBeenCalledWith('sk-ant-secret', { useForClaudeCode: true });
		expect((await screen.findByTestId('setup-provider-ready')).textContent).toContain('Connected: Claude.');
		expect((screen.getByRole('button', { name: 'Continue' }) as HTMLButtonElement).disabled).toBe(false);
	});

	it('lets a returning owner with a provider already connected continue', async () => {
		openaiReady();
		await throughLicense();
		expect((await screen.findByTestId('setup-provider-ready')).textContent).toContain('Connected: OpenAI.');
		await click('Continue');
		await screen.findByTestId('setup-workspace');
	});

	it('shows the host folder behind /workspace on Docker Compose', async () => {
		await throughProvider();
		const host = screen.getByTestId('setup-workspace-host');
		expect(host.textContent).toContain('/workspace');
		expect(host.textContent).toContain('WORKSPACE_DIR');
		expect(host.textContent).toContain('./workspace');
		expect(host.textContent).not.toMatch(/Unraid|template's Workspace path/);
		await fireEvent.input(screen.getByLabelText('Or type a path manually'), { target: { value: '/tmp/projects' } });
		expect(screen.getByText(/lost when the container is re-created/)).toBeTruthy();
	});

	it('names the host folder when the worker reports it', async () => {
		mocks.workspace.mockResolvedValue({ workspace_path: '/workspace', default_path: '/workspace', is_local_mode: false, is_configured: false, exists: true, host_path: '/mnt/user/projects' });
		await throughProvider();
		expect(screen.getByTestId('setup-workspace-host').textContent).toContain('/mnt/user/projects');
	});

	it('runs a first agent, streams the reply, and finishes as a clean install', async () => {
		await throughProvider();
		expect(mocks.saveWorkspace).not.toHaveBeenCalled();
		await click('Continue');
		expect(mocks.saveWorkspace).toHaveBeenCalledWith('/workspace');
		expect(mocks.setup).toHaveBeenCalledOnce();
		await screen.findByTestId('setup-first-run');
		expect((await screen.findByText(/GPT-5 on OpenAI, billed to your own account/))).toBeTruthy();
		expect(screen.getByTestId('setup-first-run').textContent).not.toMatch(/Claude|Anthropic/);
		await click(/Run it/);
		expect(mocks.run).toHaveBeenCalledWith(expect.objectContaining({ profileId: 'assistant', model: 'prov:codex:gpt-5' }));
		expect((await screen.findByTestId('setup-first-run-reply')).textContent).toContain('Hello! I can help you write code.');
		await click('Continue');
		await screen.findByTestId('setup-extras');
		await openExtra('github');
		expect(await screen.findByRole('button', { name: 'Sign in with GitHub' })).toBeTruthy();
		await click('Finish setup');
		await waitFor(() => expect(mocks.goto).toHaveBeenCalledWith('/'));
		expect(mocks.create).toHaveBeenCalledOnce(); expect(mocks.complete).toHaveBeenCalledOnce();
		expect(mocks.enroll).not.toHaveBeenCalled();
	});

	it('explains a failed first run and lets the owner retry or continue', async () => {
		mocks.run.mockImplementation(({ onUpdate }: { onUpdate: (s: unknown) => void }) => {
			const final = { phase: 'failed', text: '', sessionId: null, error: 'The connection to the worker closed before the run started.', code: null };
			onUpdate(final);
			return { done: Promise.resolve(final), cancel: vi.fn() };
		});
		await throughProvider();
		await click('Continue');
		await screen.findByTestId('setup-first-run');
		await click(/Run it/);
		const alert = await screen.findByTestId('setup-first-run-error');
		expect(alert.textContent).toContain('The run did not finish');
		expect(alert.textContent).toContain('closed before the run started');
		expect(screen.getByRole('button', { name: /Try again/ })).toBeTruthy();
		await click('Continue anyway');
		await screen.findByTestId('setup-extras');
	});

	it('shows the credential gate refusal of the first run and leads back to the provider step', async () => {
		mocks.run.mockImplementation(({ onUpdate }: { onUpdate: (s: unknown) => void }) => {
			const final = { phase: 'failed', text: '', sessionId: 's1', error: NOT_SIGNED_IN, code: 'agent_setup_required' };
			onUpdate(final);
			return { done: Promise.resolve(final), cancel: vi.fn() };
		});
		await throughProvider();
		await click('Continue');
		await screen.findByTestId('setup-first-run');
		await click(/Run it/);
		const alert = await screen.findByTestId('setup-first-run-error');
		expect(alert.textContent).toContain('Agents cannot start yet');
		expect(alert.textContent).toContain(NOT_SIGNED_IN);
		await click('Go back to AI provider');
		await screen.findByTestId('setup-provider');
	});

	it('shows a loop guard hold of the first run', async () => {
		const held = 'Loop guard: this automation turn is waiting for your confirmation. Confirm or dismiss it in Settings > Providers > Loop guard.';
		mocks.run.mockImplementation(({ onUpdate }: { onUpdate: (s: unknown) => void }) => {
			const final = { phase: 'failed', text: '', sessionId: 's1', error: held, code: 'loop_guard_confirm' };
			onUpdate(final);
			return { done: Promise.resolve(final), cancel: vi.fn() };
		});
		await throughProvider();
		await click('Continue');
		await screen.findByTestId('setup-first-run');
		await click(/Run it/);
		const alert = await screen.findByTestId('setup-first-run-error');
		expect(alert.textContent).toContain('The run is waiting for your confirmation');
		expect(alert.textContent).toContain(held);
		expect(screen.queryByRole('button', { name: 'Go back to AI provider' })).toBeNull();
	});

	it('connects GitHub with a token in the last step', async () => {
		mocks.githubToken.mockResolvedValue({ success: true });
		mocks.githubStatus.mockResolvedValueOnce({ authenticated: false, user: null }).mockResolvedValue({ authenticated: true, user: 'octocat' });
		await throughFirstRun();
		await openExtra('github');
		await fireEvent.click(await screen.findByRole('button', { name: 'Personal access token' }));
		await fireEvent.input(screen.getByLabelText('GitHub personal access token'), { target: { value: 'ghp_secret' } });
		await click('Connect token');
		expect(mocks.githubToken).toHaveBeenCalledWith('ghp_secret');
		expect(await screen.findByText('GitHub is connected as @octocat.')).toBeTruthy();
		await waitFor(() => expect(summary('github')).toContain('Connected as @octocat'));
	});

	it('signs in to an account in the last step and finishes without creating a network of its own', async () => {
		await throughFirstRun();
		await openExtra('account');
		await screen.findByRole('button', { name: 'Sign in' });
		await fireEvent.input(screen.getByLabelText('Name for this worker'), { target: { value: 'Studio laptop' } });
		mocks.post.mockResolvedValue(waiting);
		await click('Create account');
		expect(mocks.post).toHaveBeenCalledWith('/mobile/hub/connection', { name: 'Studio laptop' });
		connection = { ...disconnected, signed_in: true, status: 'connecting' };
		status = { ...fresh(), hub: { linked: true, registered: false, status: 'pending', account_network: { network_id: 'n', name: null, founder_device_id: 'x', devices: [{ device_id: 'x', name: 'Office PC', platform: 'windows', kind: 'worker', status: 'active' }], recovery_available: true } } };
		expect((await screen.findByText(/already syncs settings between/, {}, { timeout: 5000 })).textContent).toContain('Office PC');
		await click('Finish setup');
		await waitFor(() => expect(mocks.complete).toHaveBeenCalledOnce());
		expect(mocks.create).not.toHaveBeenCalled();
	});

	it('joins from an invite in the last step', async () => {
		await throughFirstRun();
		await openExtra('account');
		await click(/Use a worker invite instead/);
		const join = screen.getByRole('button', { name: 'Join' }) as HTMLButtonElement;
		expect(join.disabled).toBe(true);
		await fireEvent.input(screen.getByLabelText('Invite link or code'), { target: { value: link } });
		mocks.enroll.mockImplementation(async () => { status = { ...enrolled(), network: { ...enrolled().network!, founder_device_id: 'other-device', member_count: 2 } }; return { join_id: 'j', status: 'approved', inviter: {}, your_fingerprint: fingerprint }; });
		await fireEvent.click(join);
		expect(mocks.enroll).toHaveBeenCalledWith({ link, device_name: 'My worker' });
		await screen.findByText(/This worker now syncs settings with/);
		expect(summary('account')).toContain('Syncing settings with Home');
		await click('Finish setup');
		await waitFor(() => expect(mocks.complete).toHaveBeenCalledOnce());
		expect(mocks.create).not.toHaveBeenCalled();
	});

	it('flags an invite that fails while its row is closed and opens the row again', async () => {
		const enrollment = (state: string, error: string | null = null) => ({ id: 'e', status: state, endpoint_url: 'http://10.0.0.5:8000', inviter_fingerprint: null, fingerprint, error });
		await throughFirstRun();
		const row = await openExtra('account');
		await click(/Use a worker invite instead/);
		await fireEvent.input(screen.getByLabelText('Invite link or code'), { target: { value: link } });
		mocks.enroll.mockImplementation(async () => { status = { ...fresh(), enrollment: enrollment('pending') }; return { join_id: 'j', status: 'pending', inviter: {}, your_fingerprint: fingerprint }; });
		await click('Join');
		const toggle = row.querySelector('button[aria-expanded]') as HTMLButtonElement;
		await waitFor(() => expect(summary('account')).toContain('Joining with a worker invite…'), { timeout: 5000 });
		await fireEvent.click(toggle);
		expect(toggle.getAttribute('aria-expanded')).toBe('false');
		status = { ...fresh(), enrollment: enrollment('expired', 'The invite expired.') };
		await waitFor(() => expect(summary('account')).toContain("Invite didn't go through"), { timeout: 5000 });
		expect(screen.getByTestId('setup-extra-account').dataset.tone).toBe('error');
		expect(toggle.getAttribute('aria-expanded')).toBe('true');
		expect(screen.getByRole('button', { name: 'Dismiss' })).toBeTruthy();
	}, 15000);

	it('skips the account section for a worker that already belongs to a network', async () => {
		status = enrolled();
		await throughFirstRun();
		expect(screen.queryByRole('button', { name: 'Create account' })).toBeNull();
		expect(screen.queryByTestId('setup-extra-account')).toBeNull();
		await click('Finish setup');
		await waitFor(() => expect(mocks.complete).toHaveBeenCalledOnce());
		expect(mocks.create).not.toHaveBeenCalled();
	});

	it('shows each optional part as a collapsed row that says where it stands', async () => {
		mocks.githubStatus.mockResolvedValue({ authenticated: true, user: 'octocat' });
		mocks.cliConversations = [convo(1), convo(2), convo(3)];
		await throughFirstRun();
		expect(screen.getByTestId('setup-extras').textContent).toContain('Each of these is optional');
		for (const id of ['account', 'github', 'history']) {
			const toggle = screen.getByTestId(`setup-extra-${id}`).querySelector('button[aria-expanded]')!;
			expect(toggle.getAttribute('aria-expanded')).toBe('false');
		}
		expect(summary('account')).toContain('Not signed in');
		await waitFor(() => expect(summary('github')).toContain('Connected as @octocat'));
		await waitFor(() => expect(summary('history')).toContain('3 conversations found'));
		// Collapsed content stays mounted but out of reach until its row opens.
		expect(screen.queryByRole('button', { name: 'Disconnect GitHub' })).toBeNull();
		expect(screen.queryByRole('button', { name: 'Create account' })).toBeNull();
		await openExtra('history');
		expect(screen.getByText('Conversation 2')).toBeTruthy();
		await openExtra('github');
		expect(screen.getByRole('button', { name: 'Disconnect GitHub' })).toBeTruthy();
		expect(screen.getByRole('button', { name: 'Finish setup' })).toBeTruthy();
	});

	it('leaves the history row out when there is nothing to import', async () => {
		await throughFirstRun();
		await waitFor(() => expect(summary('github')).toContain('Not connected'));
		expect((screen.getByTestId('setup-extra-history') as HTMLElement).hidden).toBe(true);
	});
});
