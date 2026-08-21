/**
 * Behaviour tests for BankedResetsPanel — the inline banked-reset inventory that
 * replaced the old modal dialog. Every mechanic the dialog had is still under
 * test (listing, arming, consuming, idempotency-key reuse on retry, the 409
 * provider refresh, outcome copy, Grok vs Codex wording); only the dialog chrome
 * assertions are gone, because there is no dialog any more.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, waitFor } from '@testing-library/svelte';
import type { ModelProvider } from '$lib/api/model-providers';

const mocks = vi.hoisted(() => ({
	list: vi.fn(),
	consume: vi.fn(),
	listGrok: vi.fn(),
	consumeGrok: vi.fn(),
	refreshProviders: vi.fn(),
	randomUUID: vi.fn()
}));

vi.mock('$lib/api/model-providers', async (importOriginal) => {
	const actual = await importOriginal<typeof import('$lib/api/model-providers')>();
	return {
		...actual,
		listCodexRateLimitResetCredits: mocks.list,
		consumeCodexRateLimitResetCredit: mocks.consume,
		listGrokRateLimitResetCredits: mocks.listGrok,
		consumeGrokRateLimitResetCredit: mocks.consumeGrok
	};
});

vi.mock('$lib/stores/providers.svelte', () => ({
	providers: { refreshProviders: mocks.refreshProviders }
}));

import BankedResetsPanel from './BankedResetsPanel.svelte';

const provider: ModelProvider = {
	id: 'chatgpt-1',
	name: 'Work ChatGPT',
	provider_type: 'chatgpt',
	base_url: 'https://chatgpt.com/backend-api/codex',
	auth_token_set: false,
	auth_token_masked: '',
	models: [],
	default_model: null,
	small_fast_model: null,
	extra_env: {},
	context_window: null,
	max_output_tokens: null,
	enabled: true,
	auth_mode: 'chatgpt',
	needs_reauth: false,
	codex_access_token_set: true,
	created_at: null,
	updated_at: null
};

function grokProvider(partial: Partial<ModelProvider> = {}): ModelProvider {
	return {
		...provider,
		id: 'grok-1',
		name: 'Work Grok',
		provider_type: 'grok',
		base_url: 'https://cli-chat-proxy.grok.com',
		auth_mode: 'grok',
		...partial
	};
}

function credit(
	partial: Partial<{
		id: string;
		reset_type: 'codex_rate_limits' | 'grok_rate_limits' | 'unknown';
		status: 'available' | 'redeeming' | 'redeemed' | 'unknown';
		granted_at: number;
		expires_at: number | null;
		title: string | null;
		description: string | null;
	}> = {}
) {
	return {
		id: 'credit-1',
		reset_type: 'codex_rate_limits' as const,
		status: 'available' as const,
		granted_at: 1_750_000_000,
		expires_at: 1_760_000_000,
		title: 'Weekly reset',
		description: 'Restores eligible Codex windows.',
		...partial
	};
}

function deferred<T>() {
	let resolve!: (value: T) => void;
	let reject!: (reason?: unknown) => void;
	const promise = new Promise<T>((res, rej) => {
		resolve = res;
		reject = rej;
	});
	return { promise, resolve, reject };
}

beforeEach(() => {
	Object.values(mocks).forEach((mock) => mock.mockReset());
	mocks.refreshProviders.mockResolvedValue(undefined);
	mocks.randomUUID.mockReturnValue('uuid-1');
	vi.stubGlobal('crypto', { randomUUID: mocks.randomUUID });
});

afterEach(() => {
	cleanup();
	vi.unstubAllGlobals();
});

describe('BankedResetsPanel inventory', () => {
	it('shows loading, then every credit in order with metadata, statuses, fallback, and dates', async () => {
		const request = deferred<{
			available_count: number;
			credits: ReturnType<typeof credit>[];
		}>();
		mocks.list.mockReturnValue(request.promise);
		const { getByText, getAllByText, getAllByRole, queryByText } = render(BankedResetsPanel, {
			props: { provider }
		});

		expect(getByText('Loading…')).toBeTruthy();
		request.resolve({
			available_count: 1,
			credits: [
				credit(),
				credit({
					id: 'credit-2',
					status: 'redeeming',
					title: null,
					description: null,
					expires_at: null
				}),
				credit({ id: 'credit-3', status: 'redeemed', title: 'Already used' }),
				credit({ id: 'credit-4', reset_type: 'unknown', status: 'unknown', title: 'Other credit' })
			]
		});

		await waitFor(() => expect(getByText('Weekly reset')).toBeTruthy());
		expect(getByText('Codex rate-limit reset')).toBeTruthy();
		expect(getAllByText('Restores eligible Codex windows.')).toHaveLength(3);
		expect(getByText('Being used')).toBeTruthy();
		expect(getByText('Used')).toBeTruthy();
		expect(getByText('Status unavailable')).toBeTruthy();
		expect(getByText('No expiration shown')).toBeTruthy();
		expect(
			getAllByText(
				`Granted ${new Intl.DateTimeFormat(undefined, { dateStyle: 'medium', timeStyle: 'short' }).format(new Date(1_750_000_000 * 1000))}`
			)
		).toHaveLength(4);
		expect(getAllByRole('button', { name: 'Use' })).toHaveLength(1);
		expect(queryByText('Loading…')).toBeNull();
	});

	it('shows the header count and the empty inventory state', async () => {
		mocks.list.mockResolvedValue({ available_count: 0, credits: [] });
		const { getByText } = render(BankedResetsPanel, { props: { provider } });
		await waitFor(() => expect(getByText('No banked resets.')).toBeTruthy());
		expect(getByText(/Banked resets · 0 available/)).toBeTruthy();
	});

	it('states when the available inventory details are capped without inventing rows', async () => {
		mocks.list.mockResolvedValue({ available_count: 4, credits: [credit()] });
		const { getByText, getAllByRole } = render(BankedResetsPanel, { props: { provider } });
		await waitFor(() => expect(getByText('1 of 4 available reset details are shown.')).toBeTruthy());
		expect(getAllByRole('listitem')).toHaveLength(1);
	});

	it('shows a retryable initial error', async () => {
		mocks.list
			.mockRejectedValueOnce(new Error('Inventory unavailable'))
			.mockResolvedValueOnce({ available_count: 0, credits: [] });
		const { getByRole, getByText } = render(BankedResetsPanel, { props: { provider } });

		await waitFor(() => expect(getByRole('alert').textContent).toContain('Inventory unavailable'));
		await fireEvent.click(getByRole('button', { name: 'Try again' }));
		await waitFor(() => expect(getByText('No banked resets.')).toBeTruthy());
		expect(mocks.list).toHaveBeenCalledTimes(2);
	});

	it('refreshes and preserves prior rows when the refresh fails', async () => {
		mocks.list
			.mockResolvedValueOnce({ available_count: 1, credits: [credit()] })
			.mockRejectedValueOnce(new Error('Refresh failed'));
		const { getByRole, getByText } = render(BankedResetsPanel, { props: { provider } });

		await waitFor(() => expect(getByText('Weekly reset')).toBeTruthy());
		await fireEvent.click(getByRole('button', { name: 'Refresh' }));
		await waitFor(() => expect(getByRole('alert').textContent).toContain('Refresh failed'));
		expect(getByText('Weekly reset')).toBeTruthy();
	});
});

describe('BankedResetsPanel consume flow', () => {
	it('expands inline confirmation and Cancel disarms it', async () => {
		mocks.list.mockResolvedValue({ available_count: 1, credits: [credit()] });
		const { getByRole, getByText, queryByText } = render(BankedResetsPanel, { props: { provider } });
		await waitFor(() => expect(getByRole('button', { name: 'Use' })).toBeTruthy());

		await fireEvent.click(getByRole('button', { name: 'Use' }));
		expect(getByText('Use this reset?')).toBeTruthy();
		expect(getByText('Eligible Codex windows may reset. This action cannot be undone.')).toBeTruthy();
		await fireEvent.click(getByRole('button', { name: 'Cancel' }));
		expect(queryByText('Use this reset?')).toBeNull();
		expect(mocks.consume).not.toHaveBeenCalled();
	});

	it('sends the selected credit and UUID and disables actions while busy', async () => {
		mocks.list
			.mockResolvedValueOnce({
				available_count: 2,
				credits: [credit({ id: 'first' }), credit({ id: 'second', title: 'Second reset' })]
			})
			.mockResolvedValueOnce({ available_count: 1, credits: [credit({ id: 'first' })] });
		const consume = deferred<{
			outcome: 'reset';
			windows_reset: number;
		}>();
		mocks.consume.mockReturnValue(consume.promise);
		const { getAllByRole, getByRole } = render(BankedResetsPanel, { props: { provider } });
		await waitFor(() => expect(getAllByRole('button', { name: 'Use' })).toHaveLength(2));

		await fireEvent.click(getAllByRole('button', { name: 'Use' })[1]);
		await fireEvent.click(getByRole('button', { name: 'Confirm' }));

		expect(mocks.consume).toHaveBeenCalledWith('chatgpt-1', {
			credit_id: 'second',
			idempotency_key: 'uuid-1'
		});
		expect((getByRole('button', { name: 'Using…' }) as HTMLButtonElement).disabled).toBe(true);
		expect((getByRole('button', { name: 'Refresh' }) as HTMLButtonElement).disabled).toBe(true);
		expect((getByRole('button', { name: 'Use' }) as HTMLButtonElement).disabled).toBe(true);

		consume.resolve({ outcome: 'reset', windows_reset: 2 });
		await waitFor(() => expect(mocks.list).toHaveBeenCalledTimes(2));
	});

	it('reuses the UUID after an ambiguous failure', async () => {
		mocks.list
			.mockResolvedValueOnce({ available_count: 1, credits: [credit()] })
			.mockResolvedValueOnce({ available_count: 0, credits: [] });
		mocks.consume
			.mockRejectedValueOnce(new Error('Connection lost'))
			.mockResolvedValueOnce({ outcome: 'reset', windows_reset: 1 });
		const { getByRole } = render(BankedResetsPanel, { props: { provider } });
		await waitFor(() => expect(getByRole('button', { name: 'Use' })).toBeTruthy());
		await fireEvent.click(getByRole('button', { name: 'Use' }));
		await fireEvent.click(getByRole('button', { name: 'Confirm' }));
		await waitFor(() => expect(getByRole('alert').textContent).toContain('Connection lost'));
		await fireEvent.click(getByRole('button', { name: 'Try again' }));

		await waitFor(() => expect(mocks.consume).toHaveBeenCalledTimes(2));
		expect(mocks.consume.mock.calls[0][1].idempotency_key).toBe('uuid-1');
		expect(mocks.consume.mock.calls[1][1].idempotency_key).toBe('uuid-1');
		expect(mocks.randomUUID).toHaveBeenCalledTimes(1);
	});

	it.each([
		['reset', 2, '2 eligible Codex windows were reset.'],
		['nothing_to_reset', 0, 'No eligible Codex windows needed a reset.'],
		['no_credit', 0, 'This banked reset is no longer available.'],
		['already_redeemed', 0, 'This banked reset was already used.']
	] as const)(
		'handles %s and refreshes inventory while preserving the outcome',
		async (outcome, windowsReset, message) => {
			mocks.list
				.mockResolvedValueOnce({ available_count: 1, credits: [credit()] })
				.mockRejectedValueOnce(new Error('Post-use refresh failed'));
			mocks.consume.mockResolvedValue({ outcome, windows_reset: windowsReset });
			const { getByRole, getByText } = render(BankedResetsPanel, { props: { provider } });
			await waitFor(() => expect(getByRole('button', { name: 'Use' })).toBeTruthy());
			await fireEvent.click(getByRole('button', { name: 'Use' }));
			await fireEvent.click(getByRole('button', { name: 'Confirm' }));

			await waitFor(() => expect(getByText(message)).toBeTruthy());
			expect(mocks.list).toHaveBeenCalledTimes(2);
			expect(getByRole('alert').textContent).toContain('Post-use refresh failed');
			expect(getByText(message)).toBeTruthy();
		}
	);

	it('keeps a 409 error visible and refreshes providers', async () => {
		mocks.list.mockResolvedValue({ available_count: 1, credits: [credit()] });
		mocks.consume.mockRejectedValue({ status: 409, detail: 'Provider credentials changed' });
		const { getByRole } = render(BankedResetsPanel, { props: { provider } });
		await waitFor(() => expect(getByRole('button', { name: 'Use' })).toBeTruthy());
		await fireEvent.click(getByRole('button', { name: 'Use' }));
		await fireEvent.click(getByRole('button', { name: 'Confirm' }));

		await waitFor(() => expect(mocks.refreshProviders).toHaveBeenCalledOnce());
		expect(getByRole('alert').textContent).toContain('Provider credentials changed');
	});
});

describe('BankedResetsPanel Grok provider', () => {
	it('loads Grok inventory copy, default title, and grok APIs', async () => {
		mocks.listGrok.mockResolvedValue({
			available_count: 1,
			credits: [
				credit({
					reset_type: 'grok_rate_limits',
					title: null,
					description: null
				})
			]
		});
		const { getByText, getByRole } = render(BankedResetsPanel, {
			props: { provider: grokProvider() }
		});

		await waitFor(() => expect(getByText('Usage limit reset')).toBeTruthy());
		expect(getByText(/A reset clears your weekly usage once/)).toBeTruthy();
		expect(mocks.listGrok).toHaveBeenCalledWith('grok-1');
		expect(mocks.list).not.toHaveBeenCalled();

		await fireEvent.click(getByRole('button', { name: 'Use' }));
		expect(
			getByText('Eligible Grok weekly usage may reset. This action cannot be undone.')
		).toBeTruthy();
	});

	it('consumes via the Grok API and shows Grok outcome copy', async () => {
		mocks.listGrok
			.mockResolvedValueOnce({
				available_count: 1,
				credits: [credit({ reset_type: 'grok_rate_limits' })]
			})
			.mockResolvedValueOnce({ available_count: 0, credits: [] });
		mocks.consumeGrok.mockResolvedValue({ outcome: 'reset', windows_reset: 1 });
		const { getByRole, getByText } = render(BankedResetsPanel, {
			props: { provider: grokProvider() }
		});
		await waitFor(() => expect(getByRole('button', { name: 'Use' })).toBeTruthy());
		await fireEvent.click(getByRole('button', { name: 'Use' }));
		await fireEvent.click(getByRole('button', { name: 'Confirm' }));

		await waitFor(() =>
			expect(getByText('1 eligible Grok weekly usage window was reset.')).toBeTruthy()
		);
		expect(mocks.consumeGrok).toHaveBeenCalledWith('grok-1', {
			credit_id: 'credit-1',
			idempotency_key: 'uuid-1'
		});
		expect(mocks.consume).not.toHaveBeenCalled();
	});

	it('does not offer Use on a Codex-typed credit for a Grok account', async () => {
		mocks.listGrok.mockResolvedValue({
			available_count: 1,
			credits: [credit({ reset_type: 'codex_rate_limits' })]
		});
		const { queryByRole, getByText } = render(BankedResetsPanel, {
			props: { provider: grokProvider() }
		});
		await waitFor(() => expect(getByText('Weekly reset')).toBeTruthy());
		expect(queryByRole('button', { name: 'Use' })).toBeNull();
	});
});
