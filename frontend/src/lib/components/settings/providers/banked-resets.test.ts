/**
 * Banked resets (ChatGPT / SuperGrok > Usage), ported from BankedResetsPanel.
 * Every mechanic is still under test: listing, arming, consuming, the
 * idempotency key reused on retry, the 409 provider refresh, outcome copy,
 * and Grok vs Codex wording.
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

vi.mock('$lib/api/model-providers', async (importOriginal) => ({
	...(await importOriginal<typeof import('$lib/api/model-providers')>()),
	listCodexRateLimitResetCredits: mocks.list,
	consumeCodexRateLimitResetCredit: mocks.consume,
	listGrokRateLimitResetCredits: mocks.listGrok,
	consumeGrokRateLimitResetCredit: mocks.consumeGrok
}));
vi.mock('$lib/stores/providers.svelte', () => ({ providers: { refreshProviders: mocks.refreshProviders } }));

import SubscriptionUsage from './tabs/SubscriptionUsage.svelte';
import { BankedResets } from './banked-resets.svelte';
import { chatgptRow, grokRow } from './fixtures';

function credit(partial: Partial<{ id: string; reset_type: 'codex_rate_limits' | 'grok_rate_limits' | 'unknown'; status: 'available' | 'redeeming' | 'redeemed' | 'unknown'; expires_at: number | null; title: string | null; description: string | null }> = {}) {
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
	const promise = new Promise<T>((res) => (resolve = res));
	return { promise, resolve };
}

function mount(provider: ModelProvider = chatgptRow()) {
	const banked = new BankedResets(() => provider);
	const view = render(SubscriptionUsage, { banked, usageEntry: null, signedIn: true });
	void banked.refresh();
	return { ...view, banked };
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

describe('inventory', () => {
	it('shows loading, then every credit with its status, fallback title and Use only where usable', async () => {
		const request = deferred<{ available_count: number; credits: ReturnType<typeof credit>[] }>();
		mocks.list.mockReturnValue(request.promise);
		const { getByText, findByText, getAllByText, getAllByRole, queryByText } = mount();
		expect(await findByText('Loading banked resets')).toBeTruthy();
		request.resolve({
			available_count: 1,
			credits: [
				credit(),
				credit({ id: 'credit-2', status: 'redeeming', title: null, description: null, expires_at: null }),
				credit({ id: 'credit-3', status: 'redeemed', title: 'Already used' }),
				credit({ id: 'credit-4', reset_type: 'unknown', status: 'unknown', title: 'Other credit' })
			]
		});
		await waitFor(() => expect(getByText('Weekly reset')).toBeTruthy());
		expect(getByText('Codex rate-limit reset')).toBeTruthy();
		expect(getAllByText('Being used').length).toBeGreaterThan(0);
		expect(getAllByText('Used').length).toBeGreaterThan(0);
		expect(getAllByText('Status unavailable').length).toBeGreaterThan(0);
		const date = new Intl.DateTimeFormat(undefined, { dateStyle: 'medium' }).format(new Date(1_760_000_000 * 1000));
		expect(getAllByText(`Expires ${date}`)).toHaveLength(3);
		expect(getAllByRole('button', { name: 'Use' })).toHaveLength(1);
		expect(queryByText('Loading banked resets')).toBeNull();
		expect(getByText('1 available')).toBeTruthy();
	});

	it('shows the empty state', async () => {
		mocks.list.mockResolvedValue({ available_count: 0, credits: [] });
		const { getByText } = mount();
		await waitFor(() => expect(getByText('No banked resets')).toBeTruthy());
		expect(getByText('0 available')).toBeTruthy();
	});

	it('states when the available details are capped without inventing rows', async () => {
		mocks.list.mockResolvedValue({ available_count: 4, credits: [credit()] });
		const { getByText } = mount();
		await waitFor(() => expect(getByText('1 of 4 available reset details are shown.')).toBeTruthy());
	});

	it('retries an initial load failure', async () => {
		mocks.list.mockRejectedValueOnce(new Error('Inventory unavailable')).mockResolvedValueOnce({ available_count: 0, credits: [] });
		const { getByRole, getByText } = mount();
		await waitFor(() => expect(getByText('Could not load resets')).toBeTruthy());
		await fireEvent.click(getByRole('button', { name: 'Try again' }));
		await waitFor(() => expect(getByText('No banked resets')).toBeTruthy());
		expect(mocks.list).toHaveBeenCalledTimes(2);
	});

	it('keeps prior rows when a refresh fails', async () => {
		mocks.list.mockResolvedValueOnce({ available_count: 1, credits: [credit()] }).mockRejectedValueOnce(new Error('Refresh failed'));
		const { getByRole, getByText } = mount();
		await waitFor(() => expect(getByText('Weekly reset')).toBeTruthy());
		await fireEvent.click(getByRole('button', { name: 'Refresh resets' }));
		await waitFor(() => expect(getByRole('alert').textContent).toContain('Refresh failed'));
		expect(getByText('Weekly reset')).toBeTruthy();
	});
});

describe('Use', () => {
	it('asks first, and Cancel disarms without spending', async () => {
		mocks.list.mockResolvedValue({ available_count: 1, credits: [credit()] });
		const { getByRole, getByText, queryByText } = mount();
		await waitFor(() => expect(getByRole('button', { name: 'Use' })).toBeTruthy());
		await fireEvent.click(getByRole('button', { name: 'Use' }));
		expect(getByText('Can’t be undone')).toBeTruthy();
		await fireEvent.click(getByRole('button', { name: 'Cancel' }));
		expect(queryByText('Can’t be undone')).toBeNull();
		expect(mocks.consume).not.toHaveBeenCalled();
	});

	it('sends the chosen credit with a UUID and blocks other actions while busy', async () => {
		mocks.list
			.mockResolvedValueOnce({ available_count: 2, credits: [credit({ id: 'first' }), credit({ id: 'second', title: 'Second reset' })] })
			.mockResolvedValueOnce({ available_count: 1, credits: [credit({ id: 'first' })] });
		const consume = deferred<{ outcome: 'reset'; windows_reset: number }>();
		mocks.consume.mockReturnValue(consume.promise);
		const { getAllByRole, getByRole } = mount();
		await waitFor(() => expect(getAllByRole('button', { name: 'Use' })).toHaveLength(2));
		await fireEvent.click(getAllByRole('button', { name: 'Use' })[1]);
		await fireEvent.click(getAllByRole('button', { name: 'Use' })[1]);
		expect(mocks.consume).toHaveBeenCalledWith('chatgpt-1', { credit_id: 'second', idempotency_key: 'uuid-1' });
		expect((getByRole('button', { name: 'Refresh resets' }) as HTMLButtonElement).disabled).toBe(true);
		expect((getAllByRole('button', { name: 'Use' })[0] as HTMLButtonElement).disabled).toBe(true);
		consume.resolve({ outcome: 'reset', windows_reset: 2 });
		await waitFor(() => expect(mocks.list).toHaveBeenCalledTimes(2));
	});

	it('reuses the UUID after an ambiguous failure', async () => {
		mocks.list.mockResolvedValueOnce({ available_count: 1, credits: [credit()] }).mockResolvedValueOnce({ available_count: 0, credits: [] });
		mocks.consume.mockRejectedValueOnce(new Error('Connection lost')).mockResolvedValueOnce({ outcome: 'reset', windows_reset: 1 });
		const { getByRole } = mount();
		await waitFor(() => expect(getByRole('button', { name: 'Use' })).toBeTruthy());
		await fireEvent.click(getByRole('button', { name: 'Use' }));
		await fireEvent.click(getByRole('button', { name: 'Use' }));
		await waitFor(() => expect(getByRole('alert').textContent).toContain('Connection lost'));
		await fireEvent.click(getByRole('button', { name: 'Use' }));
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
	] as const)('reports %s and keeps the outcome when the refresh fails', async (outcome, windowsReset, message) => {
		mocks.list.mockResolvedValueOnce({ available_count: 1, credits: [credit()] }).mockRejectedValueOnce(new Error('Post-use refresh failed'));
		mocks.consume.mockResolvedValue({ outcome, windows_reset: windowsReset });
		const { getByRole, getByText } = mount();
		await waitFor(() => expect(getByRole('button', { name: 'Use' })).toBeTruthy());
		await fireEvent.click(getByRole('button', { name: 'Use' }));
		await fireEvent.click(getByRole('button', { name: 'Use' }));
		await waitFor(() => expect(getByText(message)).toBeTruthy());
		expect(mocks.list).toHaveBeenCalledTimes(2);
		expect(getByRole('alert').textContent).toContain('Post-use refresh failed');
	});

	it('keeps a 409 error visible and refreshes providers', async () => {
		mocks.list.mockResolvedValue({ available_count: 1, credits: [credit()] });
		mocks.consume.mockRejectedValue({ status: 409, detail: 'Provider credentials changed' });
		const { getByRole } = mount();
		await waitFor(() => expect(getByRole('button', { name: 'Use' })).toBeTruthy());
		await fireEvent.click(getByRole('button', { name: 'Use' }));
		await fireEvent.click(getByRole('button', { name: 'Use' }));
		await waitFor(() => expect(mocks.refreshProviders).toHaveBeenCalledOnce());
		expect(getByRole('alert').textContent).toContain('Provider credentials changed');
	});
});

describe('Grok', () => {
	it('uses the Grok inventory, default title and outcome copy', async () => {
		mocks.listGrok
			.mockResolvedValueOnce({ available_count: 1, credits: [credit({ reset_type: 'grok_rate_limits', title: null, description: null })] })
			.mockResolvedValueOnce({ available_count: 0, credits: [] });
		mocks.consumeGrok.mockResolvedValue({ outcome: 'reset', windows_reset: 1 });
		const { getByRole, getByText, banked } = mount(grokRow({ needs_reauth: false }));
		await waitFor(() => expect(getByText('Usage limit reset')).toBeTruthy());
		expect(banked.description(banked.credits[0])).toMatch(/A reset clears your weekly usage once/);
		expect(mocks.listGrok).toHaveBeenCalledWith('grok-1');
		expect(mocks.list).not.toHaveBeenCalled();
		await fireEvent.click(getByRole('button', { name: 'Use' }));
		await fireEvent.click(getByRole('button', { name: 'Use' }));
		await waitFor(() => expect(getByText('1 eligible Grok weekly usage window was reset.')).toBeTruthy());
		expect(mocks.consumeGrok).toHaveBeenCalledWith('grok-1', { credit_id: 'credit-1', idempotency_key: 'uuid-1' });
		expect(mocks.consume).not.toHaveBeenCalled();
	});

	it('does not offer Use on a Codex-typed credit for a Grok account', async () => {
		mocks.listGrok.mockResolvedValue({ available_count: 1, credits: [credit({ reset_type: 'codex_rate_limits' })] });
		const { queryByRole, getByText } = mount(grokRow({ needs_reauth: false }));
		await waitFor(() => expect(getByText('Weekly reset')).toBeTruthy());
		expect(queryByRole('button', { name: 'Use' })).toBeNull();
	});
});
