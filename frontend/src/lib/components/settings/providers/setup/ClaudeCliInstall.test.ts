import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/svelte';
import { tick } from 'svelte';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import type { ClaudeCliInstallation, ClaudeCliJob } from '$lib/api/claude-cli';

const mocks = vi.hoisted(() => ({ get: vi.fn(), post: vi.fn(), put: vi.fn(), refresh: vi.fn(), auth: vi.fn() }));
vi.mock('$lib/api/client', () => ({ api: { get: mocks.get, post: mocks.post, put: mocks.put },
	getErrorMessage: (e: Error, fallback: string) => e?.message ?? fallback }));
vi.mock('$lib/stores/providers.svelte', async (original) => ({
	...(await original<typeof import('$lib/stores/providers.svelte')>()),
	providers: { loadClaude: mocks.refresh }
}));
vi.mock('$lib/stores/auth.svelte', async (original) => ({
	...(await original<typeof import('$lib/stores/auth.svelte')>()),
	auth: { checkAuth: mocks.auth }
}));
import ClaudeCliInstall from './ClaudeCliInstall.svelte';
import ClaudeCliPanel from '../../ClaudeCliPanel.svelte';

const base: ClaudeCliInstallation = { installed: false, version: null, path: null, keep_installed: false,
	installed_by_app: false, can_install: true, can_remove: false, platform: 'docker', job: null };
const job = (state: ClaudeCliJob['state'], kind: ClaudeCliJob['kind'] = 'install'): ClaudeCliJob => ({
	kind, state, started_at: new Date().toISOString(), finished_at: state === 'running' ? null : new Date().toISOString(),
	message: state === 'failed' ? 'Network connection failed.' : state === 'succeeded' ? 'Claude Code updated from 2.1.280 to 2.1.285.' : null,
	output_tail: 'Downloading installer\nConnecting to Anthropic'
});
let current: ClaudeCliInstallation;
beforeEach(() => {
	vi.clearAllMocks(); current = { ...base };
	mocks.refresh.mockResolvedValue(undefined); mocks.auth.mockResolvedValue(undefined);
	mocks.get.mockImplementation(async (path: string) => {
		if (path === '/settings/claude-cli') return current;
		if (path.startsWith('/auth/claude/cli-login')) return { supported: true, active: false };
		return { installed: current.installed, signed_in: false, cli_version: current.version, binary_path: current.path,
			login: { surface: 'docker', terminal: true, command: 'claude auth login' } };
	});
	mocks.post.mockImplementation(async () => current);
	mocks.put.mockImplementation(async (_: string, body: { keep_installed: boolean }) => current = { ...current, ...body });
});
afterEach(() => { cleanup(); vi.useRealTimers(); vi.restoreAllMocks(); });

it('installs, polls, refreshes auth, and focuses the sign-in heading without a reload', async () => {
	render(ClaudeCliPanel, { isAdmin: true, variant: 'setup' });
	const button = await screen.findByRole('button', { name: 'Install Claude Code' });
	vi.useFakeTimers();
	current = { ...base, job: job('running') };
	await fireEvent.click(button); await tick();
	expect(mocks.post).toHaveBeenCalledWith('/settings/claude-cli/install', {});
	expect(screen.getByRole('button', { name: 'Working' }).hasAttribute('disabled')).toBe(true);
	expect(screen.getByText(/Connecting to Anthropic/)).toBeTruthy();
	current = { ...current, installed: true, version: '2.1.285', job: job('succeeded') };
	await vi.advanceTimersByTimeAsync(2000); await tick();
	expect(screen.getByRole('button', { name: 'Sign in' })).toBeTruthy();
	expect(document.activeElement).toBe(screen.getByRole('heading', { name: 'Sign in to Claude Code' }));
	expect(mocks.refresh).toHaveBeenCalledWith(true); expect(mocks.auth).toHaveBeenCalled();
});
it('shows failure output, retry and a copyable manual command', async () => {
	current = { ...base, job: job('failed') }; render(ClaudeCliInstall);
	expect(await screen.findByText('Network connection failed.')).toBeTruthy();
	expect(screen.getByText(/Connecting to Anthropic/)).toBeTruthy();
	expect(screen.getByText('curl -fsSL https://claude.ai/install.sh | bash')).toBeTruthy();
	expect(screen.getByRole('button', { name: 'Copy install command' })).toBeTruthy();
	await fireEvent.click(screen.getByRole('button', { name: 'Try again' }));
	expect(mocks.post).toHaveBeenCalledWith('/settings/claude-cli/install', {});
});
it('uses PowerShell for Windows desktop, but the shell installer for Docker', async () => {
	vi.spyOn(navigator, 'platform', 'get').mockReturnValue('Win32');
	current = { ...base, platform: 'desktop', can_install: false };
	const view = render(ClaudeCliInstall);
	expect(await screen.findByText('irm https://claude.ai/install.ps1 | iex')).toBeTruthy();
	view.unmount(); current = { ...base, can_install: false }; render(ClaudeCliInstall);
	expect(await screen.findByText('curl -fsSL https://claude.ai/install.sh | bash')).toBeTruthy();
});
it('offers only the manual command when the installer is unavailable', async () => {
	current = { ...base, can_install: false }; render(ClaudeCliInstall);
	expect(await screen.findByText(/cannot run the installer/)).toBeTruthy();
	expect(screen.queryByRole('button', { name: 'Install Claude Code' })).toBeNull();
	expect(screen.queryByRole('button', { name: 'Try again' })).toBeNull();
	expect(screen.getByRole('button', { name: 'Copy install command' })).toBeTruthy();
});
it('resumes a job on mount and stops polling on destroy', async () => {
	current = { ...base, job: job('running') }; const view = render(ClaudeCliInstall);
	await screen.findByRole('button', { name: 'Working' }); vi.useFakeTimers();
	// The initial timer was scheduled before fake timers. Remount under the fake clock.
	view.unmount(); const again = render(ClaudeCliInstall); await tick(); await Promise.resolve(); await tick();
	await vi.advanceTimersByTimeAsync(2000);
	expect(mocks.get.mock.calls.filter(([path]) => path === '/settings/claude-cli').length).toBeGreaterThan(2);
	again.unmount(); const calls = mocks.get.mock.calls.length;
	await vi.advanceTimersByTimeAsync(6000); expect(mocks.get.mock.calls.length).toBe(calls);
});
it('sends the keep-installed PUT and reflects the response', async () => {
	current = { ...base, installed: true, version: '2.1.285', path: '/home/appuser/.local/bin/claude' };
	render(ClaudeCliInstall, { manage: true });
	const control = await screen.findByRole('switch', { name: 'Keep Claude Code installed' });
	await fireEvent.click(control);
	expect(mocks.put).toHaveBeenCalledWith('/settings/claude-cli', { keep_installed: true });
	await waitFor(() => expect(control.getAttribute('aria-checked')).toBe('true'));
});
it('hides Remove for an unowned installation', async () => {
	current = { ...base, installed: true, can_remove: false }; render(ClaudeCliInstall, { manage: true });
	await screen.findByRole('button', { name: 'Check for update' });
	expect(screen.queryByRole('button', { name: 'Remove' })).toBeNull();
});
it('requires confirmation before removing and preserves the sign-in warning', async () => {
	current = { ...base, installed: true, can_remove: true }; render(ClaudeCliInstall, { manage: true });
	await fireEvent.click(await screen.findByRole('button', { name: 'Remove' }));
	expect(mocks.post).not.toHaveBeenCalled();
	const dialog = screen.getByRole('dialog');
	expect(within(dialog).getByText(/sign-in and settings in the Claude folder are kept/)).toBeTruthy();
	await fireEvent.click(within(dialog).getByRole('button', { name: 'Remove' }));
	expect(mocks.post).toHaveBeenCalledWith('/settings/claude-cli/remove', {});
});
it('shows the update result from the worker', async () => {
	current = { ...base, installed: true }; render(ClaudeCliInstall, { manage: true });
	const button = await screen.findByRole('button', { name: 'Check for update' });
	current = { ...current, job: job('succeeded', 'update') }; await fireEvent.click(button);
	expect(mocks.post).toHaveBeenCalledWith('/settings/claude-cli/update', {});
	expect(await screen.findByText('Claude Code updated from 2.1.280 to 2.1.285.')).toBeTruthy();
});
it('replaces an earlier success with a later failure', async () => {
	current = { ...base, installed: true, job: job('succeeded') };
	render(ClaudeCliInstall, { manage: true });
	expect(await screen.findByText('Claude Code updated from 2.1.280 to 2.1.285.')).toBeTruthy();
	mocks.post.mockRejectedValueOnce(new Error('A Claude Code install or update started before the worker restarted is still running.'));
	await fireEvent.click(screen.getByRole('button', { name: 'Check for update' }));
	expect(await screen.findByText(/started before the worker restarted/)).toBeTruthy();
	expect(screen.queryByText('Claude Code updated from 2.1.280 to 2.1.285.')).toBeNull();
	current = { ...current, job: job('failed', 'update') };
	await fireEvent.click(screen.getByRole('button', { name: 'Check again' }));
	expect(await screen.findByText('Network connection failed.')).toBeTruthy();
	expect(screen.queryByText('Claude Code updated from 2.1.280 to 2.1.285.')).toBeNull();
	await waitFor(() => expect(screen.getAllByRole('alert')).toHaveLength(1));
});
it('keeps the removed confirmation after the CLI is gone until it is dismissed', async () => {
	const removed = 'Claude Code removed. Your sign-in and settings were kept.';
	const onDismiss = vi.fn();
	current = { ...base, installed: true, can_remove: true }; render(ClaudeCliInstall, { manage: true, onDismiss });
	await fireEvent.click(await screen.findByRole('button', { name: 'Remove' }));
	current = { ...base, installed: false, job: { ...job('succeeded', 'remove'), message: removed } };
	await fireEvent.click(within(screen.getByRole('dialog')).getByRole('button', { name: 'Remove' }));
	expect(await screen.findByText(removed)).toBeTruthy();
	expect(screen.queryByRole('button', { name: 'Install Claude Code' })).toBeNull();
	await fireEvent.click(screen.getByRole('button', { name: 'Dismiss' }));
	expect(onDismiss).toHaveBeenCalledOnce();
	expect(screen.queryByText(removed)).toBeNull();
	expect(await screen.findByRole('button', { name: 'Install Claude Code' })).toBeTruthy();
});
it('does not replay an earlier removal on a later visit', async () => {
	current = { ...base, installed: false, job: { ...job('succeeded', 'remove'), message: 'Claude Code removed.' } };
	render(ClaudeCliInstall, { manage: true });
	expect(await screen.findByRole('button', { name: 'Install Claude Code' })).toBeTruthy();
	expect(screen.queryByRole('button', { name: 'Dismiss' })).toBeNull();
});
