/**
 * Behavior tests for ProviderList — the static provider catalog.
 *
 * Every supported provider holds a row whether or not it is configured, and the
 * per-provider actions (reconnect, banked resets, manage models) live inside the
 * row's panel — so each test expands the row first, via its "Show {title}
 * settings" control, then clicks the action.
 *
 * We hydrate the real `providers` store through a mocked list API.
 * `ChatGptDeviceFlow`'s device API is mocked so its mount does no real network I/O.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, fireEvent, cleanup, waitFor } from '@testing-library/svelte';
import type { ModelProvider } from '$lib/api/model-providers';

// jsdom lacks ResizeObserver; some lucide/layout paths probe for it.
if (!('ResizeObserver' in globalThis)) {
	(globalThis as unknown as { ResizeObserver: unknown }).ResizeObserver = class {
		observe() {}
		unobserve() {}
		disconnect() {}
	};
}

const mocks = vi.hoisted(() => ({
	listModelProviders: vi.fn(),
	createModelProvider: vi.fn(),
	deleteModelProvider: vi.fn(),
	listCodexRateLimitResetCredits: vi.fn(),
	consumeCodexRateLimitResetCredit: vi.fn(),
	listGrokRateLimitResetCredits: vi.fn(),
	consumeGrokRateLimitResetCredit: vi.fn(),
	codexDeviceStart: vi.fn(),
	codexDevicePoll: vi.fn(),
	xaiDeviceStart: vi.fn(),
	xaiDevicePoll: vi.fn(),
	hydrateModelRegistry: vi.fn()
}));

vi.mock('$lib/api/model-providers', async (importOriginal) => {
	const actual = await importOriginal<typeof import('$lib/api/model-providers')>();
	return {
		...actual,
		listModelProviders: mocks.listModelProviders,
		createModelProvider: mocks.createModelProvider,
		deleteModelProvider: mocks.deleteModelProvider,
		listCodexRateLimitResetCredits: mocks.listCodexRateLimitResetCredits,
		consumeCodexRateLimitResetCredit: mocks.consumeCodexRateLimitResetCredit,
		listGrokRateLimitResetCredits: mocks.listGrokRateLimitResetCredits,
		consumeGrokRateLimitResetCredit: mocks.consumeGrokRateLimitResetCredit,
		codexDeviceStart: mocks.codexDeviceStart,
		codexDevicePoll: mocks.codexDevicePoll,
		xaiDeviceStart: mocks.xaiDeviceStart,
		xaiDevicePoll: mocks.xaiDevicePoll
	};
});
vi.mock('$lib/constants/models.svelte', async (importOriginal) => {
	const actual = await importOriginal<typeof import('$lib/constants/models.svelte')>();
	return { ...actual, hydrateModelRegistry: mocks.hydrateModelRegistry };
});

import ProviderList from './ProviderList.svelte';
import { providers } from '$lib/stores/providers.svelte';

/** Open a row's panel by clicking its expander. */
async function expandRow(
	getByRole: (role: string, options: { name: string | RegExp }) => HTMLElement,
	name: string | RegExp
) {
	await fireEvent.click(getByRole('button', { name }));
}

function chatgptProvider(partial: Partial<ModelProvider> = {}): ModelProvider {
	return {
		id: 'cg-1',
		name: 'ChatGPT',
		provider_type: 'custom',
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
		needs_reauth: true,
		codex_access_token_set: true,
		created_at: null,
		updated_at: null,
		...partial
	};
}

beforeEach(() => {
	Object.values(mocks).forEach((m) => m.mockReset());
	mocks.hydrateModelRegistry.mockResolvedValue(undefined);
	mocks.listCodexRateLimitResetCredits.mockResolvedValue({ available_count: 0, credits: [] });
	mocks.listGrokRateLimitResetCredits.mockResolvedValue({ available_count: 0, credits: [] });
	mocks.codexDeviceStart.mockResolvedValue({
		user_code: 'ABCD-1234',
		verification_uri: 'https://example.test/verify'
	});
	mocks.codexDevicePoll.mockResolvedValue({ status: 'pending' });
	mocks.xaiDeviceStart.mockResolvedValue({
		user_code: 'WXYZ-5678',
		verification_uri: 'https://auth.x.ai/activate'
	});
	mocks.xaiDevicePoll.mockResolvedValue({ status: 'pending' });
	// Default integrations fetch: no Anthropic key set. Tests that need a key set
	// override this before calling `providers.loadIntegrations()`.
	vi.stubGlobal(
		'fetch',
		vi.fn().mockResolvedValue({ ok: true, json: async () => ({ anthropic_api_key_set: false }) })
	);
	providers.clear();
});

afterEach(() => {
	cleanup();
	vi.unstubAllGlobals();
});

describe('ProviderList — ChatGPT row reconnect', () => {
	it('clicking the ChatGPT row Reconnect button opens the reconnect dialog', async () => {
		mocks.listModelProviders.mockResolvedValue({ providers: [chatgptProvider()] });
		await providers.loadProviders();

		const { getByRole, queryByRole } = render(ProviderList);

		// No reconnect dialog before the click.
		expect(queryByRole('dialog', { name: 'Reconnect ChatGPT' })).toBeNull();

		await expandRow(getByRole, /show chatgpt settings/i);

		// needs_reauth → the panel's button label is "Reconnect".
		const btn = getByRole('button', { name: 'Reconnect' });
		await fireEvent.click(btn);

		await waitFor(() =>
			expect(getByRole('dialog', { name: 'Reconnect ChatGPT' })).toBeTruthy()
		);
		// The hosted device flow reuses the existing row (no create).
		expect(mocks.createModelProvider).not.toHaveBeenCalled();
		await waitFor(() => expect(mocks.codexDeviceStart).toHaveBeenCalledWith('cg-1'));
	});

	it('chatgpt row exposes a Manage models button', async () => {
		mocks.listModelProviders.mockResolvedValue({ providers: [chatgptProvider()] });
		await providers.loadProviders();

		const { getByRole } = render(ProviderList);
		await expandRow(getByRole, /show chatgpt settings/i);
		expect(getByRole('button', { name: 'Manage models' })).toBeTruthy();
	});
});

function grokProvider(partial: Partial<ModelProvider> = {}): ModelProvider {
	return {
		id: 'grok-1',
		name: 'Grok',
		provider_type: 'grok',
		base_url: 'https://api.x.ai/v1',
		auth_token_set: false,
		auth_token_masked: '',
		models: [],
		default_model: null,
		small_fast_model: null,
		extra_env: {},
		context_window: null,
		max_output_tokens: null,
		enabled: true,
		auth_mode: 'grok',
		needs_reauth: true,
		codex_access_token_set: true,
		created_at: null,
		updated_at: null,
		...partial
	};
}

describe('ProviderList — Grok row reconnect', () => {
	it('clicking the Grok row Reconnect button opens the reconnect dialog', async () => {
		mocks.listModelProviders.mockResolvedValue({ providers: [grokProvider()] });
		await providers.loadProviders();

		const { getByRole, queryByRole } = render(ProviderList);

		expect(queryByRole('dialog', { name: 'Reconnect Grok' })).toBeNull();

		await expandRow(getByRole, 'Show Grok settings');

		const btn = getByRole('button', { name: 'Reconnect' });
		await fireEvent.click(btn);

		await waitFor(() =>
			expect(getByRole('dialog', { name: 'Reconnect Grok' })).toBeTruthy()
		);
		expect(mocks.createModelProvider).not.toHaveBeenCalled();
		await waitFor(() => expect(mocks.xaiDeviceStart).toHaveBeenCalledWith('grok-1'));
	});
});

describe('ProviderList — banked resets action', () => {
	it('shows only for connected canonical and legacy ChatGPT rows', async () => {
		mocks.listModelProviders.mockResolvedValue({
			providers: [
				chatgptProvider({
					id: 'canonical',
					name: 'Canonical ChatGPT',
					provider_type: 'chatgpt',
					auth_mode: null,
					needs_reauth: false
				}),
				chatgptProvider({ id: 'legacy', name: 'Legacy ChatGPT', needs_reauth: false }),
				chatgptProvider({
					id: 'disconnected',
					name: 'Disconnected ChatGPT',
					codex_access_token_set: false,
					needs_reauth: false
				}),
				chatgptProvider({ id: 'reconnect', name: 'Reconnect ChatGPT', needs_reauth: true }),
				chatgptProvider({
					id: 'openai',
					name: 'OpenAI gateway',
					provider_type: 'openai',
					auth_mode: null,
					needs_reauth: false
				})
			]
		});
		await providers.loadProviders();

		const { getByRole, queryByRole } = render(ProviderList);

		// The first ChatGPT row fills the static slot, so it is titled "ChatGPT";
		// the extras keep their own names. Only one panel opens at a time, so each
		// row is checked in turn.
		const expectBankedResets = async (rowName: string | RegExp, shown: boolean) => {
			await expandRow(getByRole, rowName);
			const btn = queryByRole('button', { name: 'Banked resets' });
			if (shown) expect(btn).toBeTruthy();
			else expect(btn).toBeNull();
		};

		await expectBankedResets(/show chatgpt settings/i, true);
		await expectBankedResets('Show Legacy ChatGPT settings', true);
		await expectBankedResets('Show Disconnected ChatGPT settings', false);
		await expectBankedResets('Show Reconnect ChatGPT settings', false);
		// The OpenAI-key row is a gateway: its panel never offers banked resets.
		await expectBankedResets('Show OpenAI API key settings', false);
	});

	it('opens the clicked provider and restores focus to its exact opener', async () => {
		mocks.listModelProviders.mockResolvedValue({
			providers: [
				chatgptProvider({ id: 'first', name: 'First ChatGPT', needs_reauth: false }),
				chatgptProvider({ id: 'second', name: 'Second ChatGPT', needs_reauth: false })
			]
		});
		await providers.loadProviders();

		const { getByRole } = render(ProviderList);
		await expandRow(getByRole, 'Show Second ChatGPT settings');
		const opener = getByRole('button', { name: 'Banked resets' });
		await fireEvent.click(opener);

		await waitFor(() =>
			expect(mocks.listCodexRateLimitResetCredits).toHaveBeenCalledWith('second')
		);
		expect(getByRole('dialog', { name: 'Banked resets' })).toBeTruthy();

		await fireEvent.click(getByRole('button', { name: 'Close' }));
		await waitFor(() => expect(document.activeElement).toBe(opener));
	});

	it('shows Banked resets on a connected Grok row and opens the same dialog', async () => {
		mocks.listModelProviders.mockResolvedValue({
			providers: [grokProvider({ needs_reauth: false })]
		});
		await providers.loadProviders();

		const { getByRole } = render(ProviderList);
		await expandRow(getByRole, 'Show Grok settings');
		expect(getByRole('button', { name: 'Banked resets' })).toBeTruthy();

		await fireEvent.click(getByRole('button', { name: 'Banked resets' }));
		await waitFor(() =>
			expect(mocks.listGrokRateLimitResetCredits).toHaveBeenCalledWith('grok-1')
		);
		expect(getByRole('dialog', { name: 'Banked resets' })).toBeTruthy();
		expect(mocks.listCodexRateLimitResetCredits).not.toHaveBeenCalled();
	});

	it('does not show Banked resets on an xAI API-key row', async () => {
		mocks.listModelProviders.mockResolvedValue({
			providers: [
				chatgptProvider({
					id: 'xai-key',
					name: 'xAI API',
					provider_type: 'xai',
					auth_mode: null,
					needs_reauth: false,
					codex_access_token_set: false,
					auth_token_set: true
				})
			]
		});
		await providers.loadProviders();

		const { getByRole, queryByRole } = render(ProviderList);
		await expandRow(getByRole, 'Show Grok API key settings');
		expect(queryByRole('button', { name: 'Banked resets' })).toBeNull();
	});
});

describe('ProviderList — static catalog', () => {
	it('renders every supported provider row with an empty providers list', async () => {
		mocks.listModelProviders.mockResolvedValue({ providers: [] });
		await providers.loadProviders();
		await providers.loadIntegrations(); // default stub → no Anthropic key

		const { getByText } = render(ProviderList);
		for (const title of [
			'Claude',
			'Anthropic API key',
			'ChatGPT',
			'OpenAI API key',
			'Grok',
			'Grok API key',
			'OpenRouter'
		]) {
			expect(getByText(title)).toBeTruthy();
		}
	});
});

describe('ProviderList — Anthropic API key row', () => {
	it('renders the row as not connected when no key is set', async () => {
		mocks.listModelProviders.mockResolvedValue({ providers: [] });
		await providers.loadProviders();
		await providers.loadIntegrations(); // default stub → key unset

		const { getByText } = render(ProviderList);
		expect(getByText('Anthropic API key')).toBeTruthy();
		expect(
			getByText('Not connected — paste an Anthropic key to use it instead of the subscription.')
		).toBeTruthy();
	});

	it('shows the masked key in the row subtitle when a key is set', async () => {
		mocks.listModelProviders.mockResolvedValue({ providers: [] });
		await providers.loadProviders();

		vi.stubGlobal(
			'fetch',
			vi.fn().mockResolvedValue({
				ok: true,
				json: async () => ({
					anthropic_api_key_set: true,
					anthropic_api_key_masked: 'sk-ant-…xyz',
					claude_code_auth_method: 'api_key'
				})
			})
		);
		await providers.loadIntegrations();

		const { getByText } = render(ProviderList);
		expect(getByText('Anthropic API key')).toBeTruthy();
		// Masked key is shown as the row's status line.
		expect(getByText('Key set · sk-ant-…xyz')).toBeTruthy();
	});
});

describe('ProviderList — Claude subscription row', () => {
	it('reads as not connected when unauthenticated with no saved accounts', async () => {
		mocks.listModelProviders.mockResolvedValue({ providers: [] });
		await providers.loadProviders();

		const { getByText } = render(ProviderList);
		// Default auth store is unauthenticated and no accounts were loaded.
		expect(getByText('Not connected — sign in with your Claude account.')).toBeTruthy();
	});
});
