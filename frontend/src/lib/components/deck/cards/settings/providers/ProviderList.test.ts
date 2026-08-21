/**
 * Behavior tests for ProviderList — the static provider catalog.
 *
 * Every supported provider holds a row whether or not it is configured, and each
 * row's panel has the same shape: two actions (`Sign in` or `Add key`, plus
 * `Remove`), then the inline settings, models and banked-reset blocks. So each
 * test expands the row first, via its "Show {title} settings" control, then
 * looks at what the panel offers.
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
	fetchProviderModels: vi.fn(),
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
		fetchProviderModels: mocks.fetchProviderModels,
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
	mocks.fetchProviderModels.mockResolvedValue({ source: 'upstream', models: [] });
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

describe('ProviderList — ChatGPT row sign-in', () => {
	it('clicking the ChatGPT row Sign in button opens the reconnect dialog', async () => {
		mocks.listModelProviders.mockResolvedValue({ providers: [chatgptProvider()] });
		await providers.loadProviders();

		const { getByRole, queryByRole } = render(ProviderList);

		// No reconnect dialog before the click.
		expect(queryByRole('dialog', { name: 'Reconnect ChatGPT' })).toBeNull();

		await expandRow(getByRole, /show chatgpt settings/i);

		// Same label on every subscription row, connected or not.
		const btn = getByRole('button', { name: 'Sign in' });
		await fireEvent.click(btn);

		await waitFor(() => expect(getByRole('dialog', { name: 'Reconnect ChatGPT' })).toBeTruthy());
		// The hosted device flow reuses the existing row (no create).
		expect(mocks.createModelProvider).not.toHaveBeenCalled();
		await waitFor(() => expect(mocks.codexDeviceStart).toHaveBeenCalledWith('cg-1'));
	});

	it('chatgpt row shows the inline model list instead of a drawer', async () => {
		mocks.listModelProviders.mockResolvedValue({ providers: [chatgptProvider()] });
		await providers.loadProviders();

		const { getByRole, getByText, queryByRole } = render(ProviderList);
		await expandRow(getByRole, /show chatgpt settings/i);

		await waitFor(() => expect(mocks.fetchProviderModels).toHaveBeenCalledWith('cg-1'));
		expect(getByText(/^Models ·/)).toBeTruthy();
		expect(getByRole('button', { name: 'Refresh models' })).toBeTruthy();
		expect(queryByRole('button', { name: 'Manage models' })).toBeNull();
	});

	it('chatgpt row has no settings fields — its endpoint is pinned by the OAuth flow', async () => {
		mocks.listModelProviders.mockResolvedValue({ providers: [chatgptProvider()] });
		await providers.loadProviders();

		const { getByRole, queryByLabelText } = render(ProviderList);
		await expandRow(getByRole, /show chatgpt settings/i);

		for (const field of ['Name', 'Endpoint', 'Auth token', 'Context window', 'Max output tokens']) {
			expect(queryByLabelText(field)).toBeNull();
		}
		// The models block is still there — only the settings fields are gone.
		expect(getByRole('button', { name: 'Refresh models' })).toBeTruthy();
	});

	it('chatgpt row offers the shared Remove action with an inline confirm', async () => {
		mocks.listModelProviders.mockResolvedValue({ providers: [chatgptProvider()] });
		mocks.deleteModelProvider.mockResolvedValue({ success: true });
		await providers.loadProviders();

		const { getByRole, getByText } = render(ProviderList);
		await expandRow(getByRole, /show chatgpt settings/i);

		await fireEvent.click(getByRole('button', { name: 'Remove' }));
		expect(getByText('Remove?')).toBeTruthy();
		await fireEvent.click(getByRole('button', { name: 'Remove' }));
		await waitFor(() => expect(mocks.deleteModelProvider).toHaveBeenCalledWith('cg-1'));
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

describe('ProviderList — Grok row sign-in', () => {
	it('clicking the Grok row Sign in button opens the reconnect dialog', async () => {
		mocks.listModelProviders.mockResolvedValue({ providers: [grokProvider()] });
		await providers.loadProviders();

		const { getByRole, queryByRole } = render(ProviderList);

		expect(queryByRole('dialog', { name: 'Reconnect Grok' })).toBeNull();

		await expandRow(getByRole, 'Show Grok settings');

		const btn = getByRole('button', { name: 'Sign in' });
		await fireEvent.click(btn);

		await waitFor(() => expect(getByRole('dialog', { name: 'Reconnect Grok' })).toBeTruthy());
		expect(mocks.createModelProvider).not.toHaveBeenCalled();
		await waitFor(() => expect(mocks.xaiDeviceStart).toHaveBeenCalledWith('grok-1'));
	});
});

describe('ProviderList — inline banked resets block', () => {
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

		const { getByRole, queryByText } = render(ProviderList);

		// The first ChatGPT row fills the static slot, so it is titled "ChatGPT";
		// the extras keep their own names. Only one panel opens at a time, so each
		// row is checked in turn.
		const expectBankedResets = async (rowName: string | RegExp, shown: boolean) => {
			await expandRow(getByRole, rowName);
			const block = queryByText(/^Banked resets/);
			if (shown) expect(block).toBeTruthy();
			else expect(block).toBeNull();
		};

		await expectBankedResets(/show chatgpt settings/i, true);
		await expectBankedResets('Show Legacy ChatGPT settings', true);
		await expectBankedResets('Show Disconnected ChatGPT settings', false);
		await expectBankedResets('Show Reconnect ChatGPT settings', false);
		// The OpenAI-key row is a gateway: its panel never offers banked resets.
		await expectBankedResets('Show OpenAI API key settings', false);
	});

	it('loads the inventory of the row that was expanded', async () => {
		mocks.listModelProviders.mockResolvedValue({
			providers: [
				chatgptProvider({ id: 'first', name: 'First ChatGPT', needs_reauth: false }),
				chatgptProvider({ id: 'second', name: 'Second ChatGPT', needs_reauth: false })
			]
		});
		await providers.loadProviders();

		const { getByRole, getByText } = render(ProviderList);
		await expandRow(getByRole, 'Show Second ChatGPT settings');

		await waitFor(() => expect(mocks.listCodexRateLimitResetCredits).toHaveBeenCalledWith('second'));
		expect(mocks.listCodexRateLimitResetCredits).toHaveBeenCalledTimes(1);
		await waitFor(() => expect(getByText('No banked resets.')).toBeTruthy());
	});

	it('shows the block on a connected Grok row and loads the Grok inventory', async () => {
		mocks.listModelProviders.mockResolvedValue({
			providers: [grokProvider({ needs_reauth: false })]
		});
		await providers.loadProviders();

		const { getByRole, getByText } = render(ProviderList);
		await expandRow(getByRole, 'Show Grok settings');
		expect(getByText(/^Banked resets/)).toBeTruthy();

		await waitFor(() => expect(mocks.listGrokRateLimitResetCredits).toHaveBeenCalledWith('grok-1'));
		expect(mocks.listCodexRateLimitResetCredits).not.toHaveBeenCalled();
	});

	it('does not show banked resets on an xAI API-key row', async () => {
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

		const { getByRole, queryByText } = render(ProviderList);
		await expandRow(getByRole, 'Show Grok API key settings');
		expect(queryByText(/^Banked resets/)).toBeNull();
	});
});

describe('ProviderList — gateway row panel', () => {
	it('offers Add key plus the inline settings fields for a stored row', async () => {
		mocks.listModelProviders.mockResolvedValue({
			providers: [
				chatgptProvider({
					id: 'or-1',
					name: 'OpenRouter',
					provider_type: 'openrouter',
					base_url: 'https://openrouter.ai/api',
					auth_mode: null,
					needs_reauth: false,
					codex_access_token_set: false,
					auth_token_set: true,
					auth_token_masked: 'sk-or-…zzz'
				})
			]
		});
		await providers.loadProviders();

		const { getByRole, getByLabelText } = render(ProviderList);
		await expandRow(getByRole, 'Show OpenRouter settings');

		expect(getByRole('button', { name: 'Add key' })).toBeTruthy();
		expect((getByLabelText('Name') as HTMLInputElement).value).toBe('OpenRouter');
		expect((getByLabelText('Endpoint') as HTMLInputElement).value).toBe('https://openrouter.ai/api');
		// Save stays inert until the form is dirty.
		expect((getByRole('button', { name: 'Save' }) as HTMLButtonElement).disabled).toBe(true);

		// `Add key` on a row that already has one just focuses the token field.
		await fireEvent.click(getByRole('button', { name: 'Add key' }));
		expect(document.activeElement).toBe(getByLabelText('Auth token'));
	});

	it('offers only Add key on an unconfigured slot', async () => {
		mocks.listModelProviders.mockResolvedValue({ providers: [] });
		await providers.loadProviders();

		const { getByRole, queryByRole } = render(ProviderList);
		await expandRow(getByRole, 'Show OpenRouter settings');

		expect(getByRole('button', { name: 'Add key' })).toBeTruthy();
		expect(queryByRole('button', { name: 'Remove' })).toBeNull();
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

	it('lists the built-in models read-only, with no curation controls', async () => {
		mocks.listModelProviders.mockResolvedValue({ providers: [] });
		await providers.loadProviders();
		await providers.loadIntegrations();

		const { getByRole, getByText, queryByRole } = render(ProviderList);
		await expandRow(getByRole, 'Show Anthropic API key settings');

		expect(getByText('Models · built in')).toBeTruthy();
		// Label plus the model id, same shape as a gateway row's list.
		expect(getByText('Sonnet')).toBeTruthy();
		expect(getByText('sonnet')).toBeTruthy();
		expect(queryByRole('checkbox')).toBeNull();
		expect(mocks.fetchProviderModels).not.toHaveBeenCalled();
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

	it('offers Sign in and the built-in model list, and no Remove without an account', async () => {
		mocks.listModelProviders.mockResolvedValue({ providers: [] });
		await providers.loadProviders();

		const { getByRole, getByText, queryByRole } = render(ProviderList);
		await expandRow(getByRole, 'Show Claude settings');

		expect(getByRole('button', { name: 'Sign in' })).toBeTruthy();
		expect(queryByRole('button', { name: 'Remove' })).toBeNull();
		expect(getByText('Models · built in')).toBeTruthy();
	});
});
