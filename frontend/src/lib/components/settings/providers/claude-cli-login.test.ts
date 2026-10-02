import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const mocks = vi.hoisted(() => ({ get: vi.fn(), post: vi.fn(), hydrate: vi.fn(), checkAuth: vi.fn() }));
vi.mock('$lib/api/client', () => ({
	api: { get: mocks.get, post: mocks.post },
	getErrorMessage: (cause: { detail?: string; message?: string }, fallback: string) => cause?.detail ?? cause?.message ?? fallback
}));
vi.mock('$lib/constants/models.svelte', () => ({ hydrateModelRegistry: mocks.hydrate }));
vi.mock('$lib/stores/auth.svelte', () => ({ auth: { checkAuth: mocks.checkAuth } }));

import { ClaudeCliLogin } from './claude-cli-login.svelte';

const login = { surface: 'docker', terminal: true, command: 'claude auth login', logout_command: 'claude auth logout', status_command: 'claude auth status', docker_command: 'docker exec -it -u appuser <container> claude auth login' };
const status = (over = {}) => ({
	installed: true, signed_in: false, auth_method: 'none', auth_method_label: null, email: null, org_name: null, plan: null,
	api_provider: null, cli_version: '2.1.285', binary_path: '/home/appuser/.local/bin/claude', error: null, checked_at: 0, login, ...over
});
const idle = { supported: true, active: false, running: false, exit_code: null, succeeded: null, output: '', offset: 0, reset: false, url: null, error: null };

beforeEach(() => {
	vi.resetAllMocks();
	vi.useFakeTimers();
	mocks.checkAuth.mockResolvedValue(undefined);
	mocks.hydrate.mockResolvedValue(true);
});
afterEach(() => vi.useRealTimers());

describe('ClaudeCliLogin', () => {
	it('offers the terminal only to the owner, off the phone, when the worker has one', async () => {
		mocks.get.mockResolvedValue(status());
		const cli = new ClaudeCliLogin();
		await cli.load(false);
		expect(cli.terminalAvailable(true, false)).toBe(true);
		expect(cli.terminalAvailable(false, false)).toBe(false);
		expect(cli.terminalAvailable(true, true)).toBe(false);
		expect(cli.hostCommand).toBe(login.docker_command);
	});

	it('runs the login, appends output, passes the code and reloads when the CLI exits', async () => {
		const onStatus = vi.fn();
		mocks.get.mockImplementation(async (path: string) => {
			if (path.startsWith('/auth/claude/cli-login')) return { ...idle, active: true, running: false, succeeded: true, output: 'Login successful\n', offset: 90 };
			return status({ signed_in: path.includes('refresh') });
		});
		mocks.post.mockImplementation(async (path: string) => {
			if (path === '/auth/claude/cli-login/start') return { ...idle, active: true, running: true, output: 'Open this link\n', offset: 14, url: 'https://claude.ai/oauth' };
			if (path === '/auth/claude/cli-login/input') return { ...idle, active: true, running: true, output: 'code ok\n', offset: 22 };
			return idle;
		});
		const cli = new ClaudeCliLogin({ onStatus });
		await cli.load(false);
		await cli.startLogin();
		expect(cli.terminalOpen).toBe(true);
		expect(cli.url).toBe('https://claude.ai/oauth');
		cli.code = ' abc#def ';
		await cli.sendCode();
		expect(mocks.post).toHaveBeenCalledWith('/auth/claude/cli-login/input', { text: 'abc#def', offset: 14 });
		expect(cli.output).toBe('Open this link\ncode ok\n');
		expect(cli.code).toBe('');
		await vi.advanceTimersByTimeAsync(300);
		expect(mocks.get).toHaveBeenCalledWith('/auth/claude/cli-login?offset=22', { cache: 'no-store' });
		expect(cli.loginResult).toBe('ok');
		expect(cli.status?.signed_in).toBe(true);
		// A sign-in changes the account: the app gates and model registry re-read.
		expect(mocks.checkAuth).toHaveBeenCalled();
		expect(mocks.hydrate).toHaveBeenCalledOnce();
		expect(onStatus).toHaveBeenCalledTimes(2);
	});

	it('Stop cancels a running login and hides the terminal', async () => {
		mocks.get.mockResolvedValue(status());
		mocks.post.mockImplementation(async (path: string) =>
			path === '/auth/claude/cli-login/start' ? { ...idle, active: true, running: true, output: 'x', offset: 1 } : idle
		);
		const cli = new ClaudeCliLogin();
		await cli.startLogin();
		await cli.closeTerminal();
		expect(mocks.post).toHaveBeenCalledWith('/auth/claude/cli-login/cancel', {});
		expect(cli.terminalOpen).toBe(false);
		expect(cli.output).toBe('');
	});

	it('resumes a login still running from before a reload', async () => {
		mocks.get.mockImplementation(async (path: string) =>
			path.startsWith('/auth/claude/cli-login') ? { ...idle, active: true, running: true, output: 'waiting', offset: 7 } : status()
		);
		const cli = new ClaudeCliLogin();
		await cli.init(() => true);
		expect(cli.terminalOpen).toBe(true);
		expect(cli.output).toBe('waiting');
		cli.dispose();
	});

	it('reports a failed sign-out and reads the status again', async () => {
		mocks.get.mockResolvedValue(status({ signed_in: true }));
		mocks.post.mockResolvedValue({ success: false, error: 'not signed in' });
		const cli = new ClaudeCliLogin();
		await cli.signOut();
		expect(mocks.post).toHaveBeenCalledWith('/auth/claude/logout', {});
		expect(cli.loginError).toBe('not signed in');
		expect(mocks.get).toHaveBeenCalledWith('/auth/claude/cli?refresh=true', { cache: 'no-store' });
	});
});
