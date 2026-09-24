import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/svelte';
import type { AgentCredentials } from '$lib/api/agent-credentials';

const mocks = vi.hoisted(() => ({ creds: vi.fn(), save: vi.fn() }));
vi.mock('$lib/api/agent-credentials', async (importOriginal) => ({
	...(await importOriginal<typeof import('$lib/api/agent-credentials')>()),
	getAgentCredentials: mocks.creds,
	saveAnthropicApiKey: mocks.save,
}));
vi.mock('$lib/components/settings/ClaudeCliPanel.svelte', () => import('$lib/test-stubs/EmptyRouteDependency.svelte'));
import CredentialStep from './CredentialStep.svelte';

const NOT_SIGNED_IN = 'Agents cannot start: Claude Code is set to subscription mode and the Claude Code CLI is not signed in.';
const NO_KEY = 'Agents cannot start: Claude Code is set to API key mode and no Anthropic API key is stored on this worker.';

function creds(over: Partial<AgentCredentials> = {}): AgentCredentials {
	return {
		mode: 'subscription', modeLabel: 'Claude subscription', explicit: true,
		ready: false, credentialReady: false, licenseAllowsRuns: true, reason: NOT_SIGNED_IN,
		cli: { installed: true, signed_in: false, email: null, org_name: null, plan: null, auth_method_label: null, error: null },
		apiKey: { set: false, last4: null },
		...over,
	};
}
const signedIn = () => creds({
	ready: true, credentialReady: true, reason: null,
	cli: { installed: true, signed_in: true, email: 'owner@example.com', org_name: null, plan: 'max', auth_method_label: null, error: null },
});
const keyMode = (over: Partial<AgentCredentials> = {}) => creds({ mode: 'api_key', modeLabel: 'Anthropic API key', reason: NO_KEY, ...over });

beforeEach(() => vi.resetAllMocks());
afterEach(() => cleanup());

describe('setup credential step', () => {
	it('blocks Continue with the gate reason until the CLI is signed in', async () => {
		mocks.creds.mockResolvedValue(creds());
		const onContinue = vi.fn();
		render(CredentialStep, { mode: 'subscription', isAdmin: true, onContinue, pollMs: 60_000 });
		const missing = await screen.findByTestId('setup-credential-missing');
		expect(missing.textContent).toContain('Agents cannot start yet');
		expect(screen.getByTestId('setup-credential-reason').textContent).toBe(NOT_SIGNED_IN);
		expect((screen.getByRole('button', { name: 'Continue' }) as HTMLButtonElement).disabled).toBe(true);
		expect(screen.queryByText(/Finish without/)).toBeNull();

		mocks.creds.mockResolvedValue(signedIn());
		await fireEvent.click(screen.getByRole('button', { name: /Check again/ }));
		expect(mocks.creds).toHaveBeenLastCalledWith(true);
		const readyNote = await screen.findByTestId('setup-credential-ready');
		expect(readyNote.textContent).toContain('signed in as owner@example.com (Max). Agents can start.');
		const next = screen.getByRole('button', { name: 'Continue' }) as HTMLButtonElement;
		expect(next.disabled).toBe(false);
		await fireEvent.click(next);
		expect(onContinue).toHaveBeenCalledOnce();
	});

	it('saves an API key for Claude Code and then allows Continue', async () => {
		mocks.creds.mockResolvedValueOnce(keyMode())
			.mockResolvedValue(keyMode({ ready: true, credentialReady: true, reason: null, apiKey: { set: true, last4: 'abcd' } }));
		mocks.save.mockResolvedValue({ masked: 'sk-ant-...abcd' });
		render(CredentialStep, { mode: 'api_key', isAdmin: true, onContinue: vi.fn(), pollMs: 60_000 });
		await screen.findByTestId('setup-credential-missing');
		expect(screen.getByTestId('setup-credential-reason').textContent).toBe(NO_KEY);
		await fireEvent.input(screen.getByLabelText('Anthropic API key'), { target: { value: ' sk-ant-secret ' } });
		await fireEvent.click(screen.getByRole('button', { name: 'Save' }));
		expect(mocks.save).toHaveBeenCalledWith('sk-ant-secret', { useForClaudeCode: true });
		expect((await screen.findByTestId('setup-credential-ready')).textContent).toContain('(ending in abcd)');
		expect(screen.getByText('Anthropic API key saved')).toBeTruthy();
		expect(screen.getByText('Key ending in abcd')).toBeTruthy();
		expect((screen.getByRole('button', { name: 'Continue' }) as HTMLButtonElement).disabled).toBe(false);
	});

	it('shows why a key was refused', async () => {
		mocks.creds.mockResolvedValue(keyMode());
		mocks.save.mockRejectedValue({ status: 400, detail: 'Invalid API key. Anthropic rejected authentication.' });
		render(CredentialStep, { mode: 'api_key', isAdmin: true, onContinue: vi.fn(), pollMs: 60_000 });
		await fireEvent.input(await screen.findByLabelText('Anthropic API key'), { target: { value: 'sk-ant-bad' } });
		await fireEvent.click(screen.getByRole('button', { name: 'Save' }));
		await waitFor(() => expect(screen.getByText('Invalid API key. Anthropic rejected authentication.')).toBeTruthy());
	});

	it('says a stored key is not used in subscription mode', async () => {
		mocks.creds.mockResolvedValue({ ...signedIn(), apiKey: { set: true, last4: 'abcd' } });
		render(CredentialStep, { mode: 'subscription', isAdmin: true, onContinue: vi.fn(), pollMs: 60_000 });
		expect((await screen.findByTestId('setup-credential-key-unused')).textContent).toContain('Key ending in abcd');
	});

	it('lets the credential through but shows a license block', async () => {
		mocks.creds.mockResolvedValue({ ...signedIn(), ready: false, licenseAllowsRuns: false, reason: 'New agent runs are paused until you add a license key.' });
		render(CredentialStep, { mode: 'subscription', isAdmin: true, onContinue: vi.fn(), pollMs: 60_000 });
		const note = await screen.findByTestId('setup-credential-ready');
		expect(note.textContent).not.toContain('Agents can start');
		expect(screen.getByTestId('setup-credential-license').textContent).toContain('paused until you add a license key');
		expect((screen.getByRole('button', { name: 'Continue' }) as HTMLButtonElement).disabled).toBe(false);
	});

	it('does not accept a credential for the other mode', async () => {
		mocks.creds.mockResolvedValue(keyMode({ ready: true, credentialReady: true, reason: null, apiKey: { set: true, last4: 'abcd' } }));
		render(CredentialStep, { mode: 'subscription', isAdmin: true, onContinue: vi.fn(), pollMs: 60_000 });
		expect((await screen.findByTestId('setup-credential-reason')).textContent).toContain('set to Anthropic API key mode');
		expect((screen.getByRole('button', { name: 'Continue' }) as HTMLButtonElement).disabled).toBe(true);
	});
});
