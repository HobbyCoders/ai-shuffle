import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/svelte';
import type { AgentCredentials, ClaudeAuthMode } from '$lib/api/agent-credentials';

const mocks = vi.hoisted(() => ({ creds: vi.fn(), save: vi.fn(), setMode: vi.fn() }));
vi.mock('$lib/api/agent-credentials', async (importOriginal) => ({
	...(await importOriginal<typeof import('$lib/api/agent-credentials')>()),
	getAgentCredentials: mocks.creds,
	saveAnthropicApiKey: mocks.save,
	setClaudeAuthMode: mocks.setMode,
}));
vi.mock('$lib/components/settings/ClaudeCliPanel.svelte', () => import('$lib/test-stubs/EmptyRouteDependency.svelte'));
import ClaudeConnect from './ClaudeConnect.svelte';

function creds(over: Partial<AgentCredentials> = {}): AgentCredentials {
	return {
		mode: 'subscription', modeLabel: 'Claude subscription', explicit: true,
		ready: false, credentialReady: false, licenseAllowsRuns: true, reason: 'Not signed in.',
		cli: { installed: true, signed_in: false, email: null, org_name: null, plan: null, auth_method_label: null, error: null },
		apiKey: { set: false, last4: null },
		...over,
	};
}
const signedIn = () => creds({
	ready: true, credentialReady: true, reason: null,
	cli: { installed: true, signed_in: true, email: 'owner@example.com', org_name: null, plan: 'max', auth_method_label: null, error: null },
});

beforeEach(() => {
	vi.resetAllMocks();
	mocks.setMode.mockImplementation(async (mode: ClaudeAuthMode) => creds({ mode }));
});
afterEach(() => cleanup());

describe('setup Claude connect flow', () => {
	it('asks how to pay with nothing picked, and stores the pick as the Claude mode', async () => {
		mocks.creds.mockResolvedValue(creds({ mode: 'api_key' }));
		render(ClaudeConnect, { isAdmin: true, pollMs: 60_000 });
		const group = await screen.findByRole('radiogroup', { name: 'How to pay for Claude' });
		await waitFor(() => expect(mocks.creds).toHaveBeenCalled());
		expect([...group.querySelectorAll('input')].some((i) => (i as HTMLInputElement).checked)).toBe(false);
		await fireEvent.click(screen.getByLabelText(/Subscription/));
		expect(mocks.setMode).toHaveBeenCalledWith('subscription');
		expect((await screen.findByTestId('setup-claude-unattended')).textContent).toContain('Use an API key for unattended runs');
	});

	it('saves an Anthropic key without the CLI and reports ready', async () => {
		const missingCli = { installed: false, signed_in: false, email: null, org_name: null, plan: null, auth_method_label: null, error: null };
		mocks.creds.mockResolvedValueOnce(creds({ cli: missingCli }))
			.mockResolvedValue(creds({ mode: 'api_key', ready: true, credentialReady: true, reason: null, apiKey: { set: true, last4: 'abcd' }, cli: missingCli }));
		mocks.save.mockResolvedValue({ masked: 'sk-ant-...abcd' });
		const onReady = vi.fn();
		render(ClaudeConnect, { isAdmin: true, onReady, pollMs: 60_000 });
		await waitFor(() => expect(mocks.creds).toHaveBeenCalled());
		await fireEvent.click(screen.getByLabelText(/API key/));
		expect(mocks.setMode).toHaveBeenCalledWith('api_key');
		await fireEvent.input(await screen.findByLabelText('Anthropic API key'), { target: { value: ' sk-ant-secret ' } });
		await fireEvent.click(screen.getByRole('button', { name: 'Save key' }));
		expect(mocks.save).toHaveBeenCalledWith('sk-ant-secret', { useForClaudeCode: true });
		expect((await screen.findByTestId('setup-claude-ready')).textContent).toContain('(ending in abcd)');
		expect(onReady).toHaveBeenCalledOnce();
	});

	it('shows why a key was refused', async () => {
		mocks.creds.mockResolvedValue(creds());
		mocks.save.mockRejectedValue({ status: 400, detail: 'Invalid API key. Anthropic rejected authentication.' });
		render(ClaudeConnect, { isAdmin: true, pollMs: 60_000 });
		await waitFor(() => expect(mocks.creds).toHaveBeenCalled());
		await fireEvent.click(screen.getByLabelText(/API key/));
		await fireEvent.input(await screen.findByLabelText('Anthropic API key'), { target: { value: 'sk-ant-bad' } });
		await fireEvent.click(screen.getByRole('button', { name: 'Save key' }));
		await waitFor(() => expect(screen.getByText('Invalid API key. Anthropic rejected authentication.')).toBeTruthy());
	});

	it('opens on the working way for a returning owner', async () => {
		mocks.creds.mockResolvedValue(signedIn());
		const onReady = vi.fn();
		render(ClaudeConnect, { isAdmin: true, onReady, pollMs: 60_000 });
		expect((await screen.findByTestId('setup-claude-ready')).textContent).toContain('owner@example.com (Max)');
		expect(mocks.setMode).not.toHaveBeenCalled();
		expect(onReady).toHaveBeenCalledOnce();
	});
});
