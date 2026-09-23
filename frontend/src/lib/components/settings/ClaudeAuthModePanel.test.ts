import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/svelte';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';

const mocks = vi.hoisted(() => ({ get: vi.fn(), put: vi.fn(), checkAuth: vi.fn() }));
vi.mock('$lib/api/client', () => ({
	api: { get: mocks.get, put: mocks.put },
	getErrorMessage: (cause: { detail?: string; message?: string }, fallback: string) => cause?.detail ?? cause?.message ?? fallback
}));
vi.mock('$lib/stores/auth.svelte', () => ({ auth: { checkAuth: mocks.checkAuth } }));
import ClaudeAuthModePanel from './ClaudeAuthModePanel.svelte';

const summary = (overrides: Record<string, unknown> = {}) => ({
	mode: 'subscription',
	mode_label: 'Claude subscription',
	explicit: true,
	modes: [
		{ value: 'subscription', label: 'Claude subscription' },
		{ value: 'api_key', label: 'Anthropic API key' }
	],
	cli: { installed: true, signed_in: true, email: 'me@example.com', org_name: null, plan: 'max', auth_method_label: 'Claude account', error: null },
	api_key: { set: true, last4: 'WXYZ' },
	...overrides
});

beforeEach(() => {
	vi.resetAllMocks();
	mocks.checkAuth.mockResolvedValue(undefined);
});
afterEach(cleanup);

it('shows the active mode, the CLI account and the key last four', async () => {
	mocks.get.mockResolvedValue(summary());
	render(ClaudeAuthModePanel, { isAdmin: true });
	expect((await screen.findByTestId('claude-auth-subscription-detail')).textContent).toBe('me@example.com (Max)');
	expect(screen.getByTestId('claude-auth-key-detail').textContent).toBe('Key ending in WXYZ');
	expect(screen.getByTestId('claude-auth-active').textContent).toContain('Claude subscription');
	expect(screen.queryByTestId('claude-auth-missing')).toBeNull();
});

it('warns when the active mode has no credential', async () => {
	mocks.get.mockResolvedValue(summary({ mode: 'api_key', mode_label: 'Anthropic API key', api_key: { set: false, last4: null } }));
	render(ClaudeAuthModePanel, { isAdmin: true });
	expect((await screen.findByTestId('claude-auth-missing')).textContent).toContain('No API key is stored');
});

it('changes the mode only when the owner picks one', async () => {
	mocks.get.mockResolvedValue(summary());
	mocks.put.mockResolvedValue(summary({ mode: 'api_key', mode_label: 'Anthropic API key' }));
	render(ClaudeAuthModePanel, { isAdmin: true });
	await screen.findByTestId('claude-auth-active');
	expect(mocks.put).not.toHaveBeenCalled();
	await fireEvent.click(screen.getByRole('radio', { name: /Anthropic API key/ }));
	await waitFor(() => expect(mocks.put).toHaveBeenCalledWith('/settings/claude-auth', { mode: 'api_key' }));
	await waitFor(() => expect(screen.getByTestId('claude-auth-active').textContent).toContain('Anthropic API key'));
});

it('is read-only for other devices', async () => {
	mocks.get.mockResolvedValue(summary());
	render(ClaudeAuthModePanel, { isAdmin: false });
	await screen.findByTestId('claude-auth-active');
	expect((screen.getByRole('radio', { name: /Anthropic API key/ }) as HTMLInputElement).disabled).toBe(true);
});
