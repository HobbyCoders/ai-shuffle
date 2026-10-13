import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/svelte';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';

const mocks = vi.hoisted(() => ({ get: vi.fn(), post: vi.fn(), hydrate: vi.fn(), mobile: false }));
vi.mock('$lib/api/client', () => ({
	api: { get: mocks.get, post: mocks.post },
	getErrorMessage: (cause: { detail?: string; message?: string }, fallback: string) => cause?.detail ?? cause?.message ?? fallback
}));
vi.mock('$lib/stores/platform.svelte', () => ({ platform: { get isMobile() { return mocks.mobile; } } }));
vi.mock('$lib/constants/models.svelte', async (importOriginal) => ({
	...(await importOriginal<typeof import('$lib/constants/models.svelte')>()),
	hydrateModelRegistry: mocks.hydrate
}));
import ClaudeCliPanel from './ClaudeCliPanel.svelte';

const login = (overrides = {}) => ({
	surface: 'docker', terminal: true, command: 'claude auth login', logout_command: 'claude auth logout',
	status_command: 'claude auth status', docker_command: 'docker exec -it -u appuser <container> claude auth login',
	...overrides
});
const status = (overrides = {}) => ({
	installed: true, signed_in: false, auth_method: 'none', auth_method_label: 'Not signed in', email: null,
	org_name: null, plan: null, api_provider: 'firstParty', cli_version: '2.1.280',
	binary_path: '/home/appuser/.local/bin/claude', error: null, checked_at: 0, login: login(), ...overrides
});
const idle = { supported: true, active: false, running: false, exit_code: null, succeeded: null, output: '', offset: 0, reset: false, url: null, error: null };

beforeEach(() => { vi.resetAllMocks(); mocks.mobile = false; });
afterEach(cleanup);

it('shows the signed-in account, plan, version and binary', async () => {
	mocks.get.mockResolvedValue(status({ signed_in: true, auth_method: 'claude.ai', auth_method_label: 'Claude account', email: 'me@example.com', plan: 'max' }));
	render(ClaudeCliPanel, { isAdmin: true });
	expect((await screen.findByTestId('claude-cli-state')).textContent?.trim()).toBe('Signed in');
	expect(screen.getByTestId('claude-cli-email').textContent).toBe('me@example.com');
	expect(screen.getByText('Max')).toBeTruthy();
	expect(screen.getByText('2.1.280')).toBeTruthy();
	expect(screen.getByText('/home/appuser/.local/bin/claude')).toBeTruthy();
	expect(screen.queryByRole('button', { name: 'Sign in' })).toBeNull();
	expect(screen.getByText('claude auth logout')).toBeTruthy();
});

it('Check again asks the CLI again', async () => {
	mocks.get.mockResolvedValue(status());
	render(ClaudeCliPanel, { isAdmin: false });
	await fireEvent.click(await screen.findByRole('button', { name: /Check again/ }));
	await waitFor(() => expect(mocks.get).toHaveBeenCalledWith('/auth/claude/cli?refresh=true', { cache: 'no-store' }));
});

it('runs claude auth login in the terminal and passes the code', async () => {
	let started = false;
	mocks.get.mockImplementation(async (path: string) => {
		if (path.startsWith('/auth/claude/cli-login')) {
			if (!started) return idle;
			return { ...idle, active: true, running: true, output: 'more\n', offset: 70, url: 'https://claude.com/cai/oauth/authorize?x=1' };
		}
		return status();
	});
	mocks.post.mockImplementation(async (path: string) => {
		if (path === '/auth/claude/cli-login/start') started = true;
		if (path === '/auth/claude/cli-login/start') return { ...idle, active: true, running: true, output: 'Opening browser to sign in\n', offset: 26 };
		if (path === '/auth/claude/cli-login/input') return { ...idle, active: true, running: true, offset: 70 };
		return idle;
	});
	render(ClaudeCliPanel, { isAdmin: true });
	await fireEvent.click(await screen.findByRole('button', { name: /Sign in/ }));
	await waitFor(() => expect(mocks.get).toHaveBeenCalledWith('/auth/claude/cli-login?offset=0', { cache: 'no-store' }));
	expect(mocks.post).toHaveBeenCalledWith('/auth/claude/cli-login/start', {});
	const link = await screen.findByRole('link', { name: /Open Anthropic's sign-in page/ }, { timeout: 3000 });
	expect(link.getAttribute('href')).toBe('https://claude.com/cai/oauth/authorize?x=1');
	await fireEvent.input(screen.getByLabelText("Code from Anthropic's page"), { target: { value: ' abc#def ' } });
	await fireEvent.click(screen.getByRole('button', { name: 'Send' }));
	expect(mocks.post).toHaveBeenCalledWith('/auth/claude/cli-login/input', expect.objectContaining({ text: 'abc#def' }));
	await fireEvent.click(screen.getByRole('button', { name: 'Stop' }));
	await waitFor(() => expect(mocks.post).toHaveBeenCalledWith('/auth/claude/cli-login/cancel', {}));
});

it('shows host instructions and no terminal on the phone', async () => {
	mocks.mobile = true;
	mocks.get.mockResolvedValue(status());
	render(ClaudeCliPanel, { isAdmin: true });
	const howto = await screen.findByTestId('claude-cli-instructions');
	expect(howto.textContent).toContain('docker exec -it -u appuser <container> claude auth login');
	expect(howto.textContent).toContain('ai-shuffle');
	expect(howto.textContent).not.toMatch(/ServerWorker|AI-Shuffle/);
	expect(screen.queryByRole('button', { name: /Sign in/ })).toBeNull();
});

it('tells desktop users to run the command in their own terminal', async () => {
	mocks.get.mockResolvedValue(status({ login: login({ surface: 'desktop', terminal: false, docker_command: null }) }));
	render(ClaudeCliPanel, { isAdmin: true });
	const howto = await screen.findByTestId('claude-cli-instructions');
	expect(howto.textContent).toContain('claude auth login');
	expect(howto.textContent).not.toContain('docker');
	expect(screen.queryByRole('button', { name: /Sign in/ })).toBeNull();
});

it('Sign out runs the CLI logout and asks again', async () => {
	mocks.get.mockResolvedValueOnce(status({ signed_in: true, email: 'me@example.com' })).mockResolvedValue(status());
	mocks.post.mockResolvedValue({ success: true });
	render(ClaudeCliPanel, { isAdmin: true });
	await fireEvent.click(await screen.findByRole('button', { name: 'Sign out' }));
	expect(mocks.post).toHaveBeenCalledWith('/auth/claude/logout', {});
	await waitFor(() => expect(screen.getByTestId('claude-cli-state').textContent?.trim()).toBe('Not signed in'));
});

it('reports a missing CLI and offers the API key', async () => {
	const onUseApiKey = vi.fn();
	mocks.get.mockImplementation(async (path: string) => path === '/settings/claude-cli'
		? { installed: false, version: null, path: null, keep_installed: false, installed_by_app: false, can_install: true, can_remove: false, platform: 'docker', job: null }
		: status({ installed: false, signed_in: null, cli_version: null, binary_path: null, error: 'Claude Code CLI not found.' }));
	render(ClaudeCliPanel, { isAdmin: true, onUseApiKey });
	expect(await screen.findByRole('button', { name: 'Install Claude Code' })).toBeTruthy();
	expect(screen.getByText(/It is not installed on this worker/)).toBeTruthy();
	await fireEvent.click(screen.getByRole('button', { name: 'Use an API key' }));
	expect(onUseApiKey).toHaveBeenCalled();
});

it('re-reads the model registry when Check again finds a new sign-in', async () => {
	// The composer lists Claude models only once the registry reports a Claude
	// sign-in, so a sign-in must refresh it without a page reload.
	mocks.get.mockResolvedValueOnce(status())
		.mockResolvedValue(status({ signed_in: true, auth_method: 'claude.ai', email: 'me@example.com', plan: 'max' }));
	mocks.hydrate.mockResolvedValue(true);
	render(ClaudeCliPanel, { isAdmin: false });
	expect((await screen.findByTestId('claude-cli-state')).textContent?.trim()).toBe('Not signed in');
	expect(mocks.hydrate).not.toHaveBeenCalled();
	await fireEvent.click(screen.getByRole('button', { name: /Check again/ }));
	await waitFor(() => expect(screen.getByTestId('claude-cli-state').textContent?.trim()).toBe('Signed in'));
	expect(mocks.hydrate).toHaveBeenCalledTimes(1);
});

it('leaves the model registry alone when nothing about the account changed', async () => {
	mocks.get.mockResolvedValue(status({ signed_in: true, auth_method: 'claude.ai', email: 'me@example.com', plan: 'max' }));
	render(ClaudeCliPanel, { isAdmin: false });
	await fireEvent.click(await screen.findByRole('button', { name: /Check again/ }));
	await waitFor(() => expect(mocks.get).toHaveBeenCalledWith('/auth/claude/cli?refresh=true', { cache: 'no-store' }));
	expect(mocks.hydrate).not.toHaveBeenCalled();
});
