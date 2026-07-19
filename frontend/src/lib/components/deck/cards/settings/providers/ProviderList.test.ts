/**
 * Behavior test for the ProviderList → ChatGPT-row Reconnect wiring.
 *
 * The ChatGPT row's "Sign in"/"Reconnect" button was a no-op stub; it must now
 * open the reconnect modal hosting `ChatGptDeviceFlow` against the existing row.
 * We hydrate the real `providers` store with one ChatGPT row (via a mocked list
 * API), render the list, click the row's button, and assert the dialog renders.
 *
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
	codexDeviceStart: vi.fn(),
	codexDevicePoll: vi.fn(),
	hydrateModelRegistry: vi.fn()
}));

vi.mock('$lib/api/model-providers', async (importOriginal) => {
	const actual = await importOriginal<typeof import('$lib/api/model-providers')>();
	return {
		...actual,
		listModelProviders: mocks.listModelProviders,
		createModelProvider: mocks.createModelProvider,
		deleteModelProvider: mocks.deleteModelProvider,
		codexDeviceStart: mocks.codexDeviceStart,
		codexDevicePoll: mocks.codexDevicePoll
	};
});
vi.mock('$lib/constants/models.svelte', async (importOriginal) => {
	const actual = await importOriginal<typeof import('$lib/constants/models.svelte')>();
	return { ...actual, hydrateModelRegistry: mocks.hydrateModelRegistry };
});

import ProviderList from './ProviderList.svelte';
import { providers } from '$lib/stores/providers.svelte';

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
	mocks.codexDeviceStart.mockResolvedValue({
		user_code: 'ABCD-1234',
		verification_uri: 'https://example.test/verify'
	});
	mocks.codexDevicePoll.mockResolvedValue({ status: 'pending' });
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

		// needs_reauth → the row's button label is "Reconnect".
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
		expect(getByRole('button', { name: 'Manage models' })).toBeTruthy();
	});
});

describe('ProviderList — Anthropic API key row (conditional)', () => {
	it('does NOT render the Anthropic key row when no key is set', async () => {
		mocks.listModelProviders.mockResolvedValue({ providers: [] });
		await providers.loadProviders();
		await providers.loadIntegrations(); // default stub → key unset

		const { queryByText } = render(ProviderList);
		expect(queryByText('Anthropic API key')).toBeNull();
	});

	it('renders the Anthropic key row with masked meta when a key is set', async () => {
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
		// Masked key is shown as the row's meta line.
		expect(getByText('sk-ant-…xyz')).toBeTruthy();
	});
});

describe('ProviderList — Claude subscription row (conditional)', () => {
	it('does NOT render the Claude subscription row when unauthenticated with no saved accounts', async () => {
		mocks.listModelProviders.mockResolvedValue({ providers: [] });
		await providers.loadProviders();

		const { queryByText } = render(ProviderList);
		// Default auth store is unauthenticated and no accounts were loaded.
		expect(queryByText('Claude subscription')).toBeNull();
	});
});
