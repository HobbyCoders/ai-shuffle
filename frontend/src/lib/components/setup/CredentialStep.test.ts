import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/svelte';

const mocks = vi.hoisted(() => ({ ready: vi.fn(), keyState: vi.fn(), save: vi.fn() }));
vi.mock('$lib/api/agent-credentials', async (importOriginal) => ({
	...(await importOriginal<typeof import('$lib/api/agent-credentials')>()),
	getAgentCredentials: mocks.ready,
	getAnthropicKeyState: mocks.keyState,
	saveAnthropicApiKey: mocks.save,
}));
vi.mock('$lib/components/settings/ClaudeCliPanel.svelte', () => import('$lib/test-stubs/EmptyRouteDependency.svelte'));
import CredentialStep from './CredentialStep.svelte';

const supported = { supported: true, mode: null };
const notReady = (mode: string, reason: string) => ({ ready: false, mode, reason, source: 'gate' });

beforeEach(() => {
	vi.resetAllMocks();
	mocks.keyState.mockResolvedValue({ set: false, masked: '' });
});
afterEach(() => cleanup());

describe('setup credential step', () => {
	it('blocks Continue with an empty state until the CLI is signed in', async () => {
		mocks.ready.mockResolvedValue(notReady('subscription', 'The Claude Code CLI is not signed in.'));
		const onContinue = vi.fn();
		render(CredentialStep, { mode: 'subscription', modeState: supported, isAdmin: true, onContinue, pollMs: 60_000 });
		const missing = await screen.findByTestId('setup-credential-missing');
		expect(missing.textContent).toContain('Agents cannot start yet');
		expect(missing.textContent).toContain('The Claude Code CLI is not signed in.');
		expect((screen.getByRole('button', { name: 'Continue' }) as HTMLButtonElement).disabled).toBe(true);
		expect(screen.queryByText(/Finish without/)).toBeNull();

		mocks.ready.mockResolvedValue({ ready: true, mode: 'subscription', reason: null, source: 'gate' });
		await fireEvent.click(screen.getByRole('button', { name: /Check again/ }));
		await screen.findByTestId('setup-credential-ready');
		const next = screen.getByRole('button', { name: 'Continue' }) as HTMLButtonElement;
		expect(next.disabled).toBe(false);
		await fireEvent.click(next);
		expect(onContinue).toHaveBeenCalledOnce();
	});

	it('saves an API key and then allows Continue', async () => {
		mocks.ready.mockResolvedValueOnce(notReady('api_key', 'No Anthropic API key is saved on this worker.'))
			.mockResolvedValue({ ready: true, mode: 'api_key', reason: null, source: 'local' });
		mocks.save.mockImplementation(async () => {
			mocks.keyState.mockResolvedValue({ set: true, masked: 'sk-ant-...abcd' });
			return { set: true, masked: 'sk-ant-...abcd' };
		});
		render(CredentialStep, { mode: 'api_key', modeState: supported, isAdmin: true, onContinue: vi.fn(), pollMs: 60_000 });
		await screen.findByTestId('setup-credential-missing');
		await fireEvent.input(screen.getByLabelText('Anthropic API key'), { target: { value: ' sk-ant-secret ' } });
		await fireEvent.click(screen.getByRole('button', { name: 'Save' }));
		expect(mocks.save).toHaveBeenCalledWith('sk-ant-secret');
		await screen.findByTestId('setup-credential-ready');
		expect(screen.getByText('Anthropic API key saved')).toBeTruthy();
		expect((screen.getByRole('button', { name: 'Continue' }) as HTMLButtonElement).disabled).toBe(false);
	});

	it('shows why a key was refused', async () => {
		mocks.ready.mockResolvedValue(notReady('api_key', 'No key'));
		mocks.save.mockRejectedValue({ status: 400, detail: 'Invalid API key. Anthropic rejected authentication.' });
		render(CredentialStep, { mode: 'api_key', modeState: supported, isAdmin: true, onContinue: vi.fn(), pollMs: 60_000 });
		await fireEvent.input(await screen.findByLabelText('Anthropic API key'), { target: { value: 'sk-ant-bad' } });
		await fireEvent.click(screen.getByRole('button', { name: 'Save' }));
		await waitFor(() => expect(screen.getByText('Invalid API key. Anthropic rejected authentication.')).toBeTruthy());
	});

	it('warns that a saved key wins on a worker without the mode setting', async () => {
		mocks.ready.mockResolvedValue({ ready: true, mode: 'subscription', reason: null, source: 'local' });
		mocks.keyState.mockResolvedValue({ set: true, masked: 'sk-ant-...abcd' });
		render(CredentialStep, { mode: 'subscription', modeState: { supported: false, mode: 'subscription' }, isAdmin: true, onContinue: vi.fn(), pollMs: 60_000 });
		expect(await screen.findByText(/uses a saved key instead of the CLI's sign-in/)).toBeTruthy();
	});
});
