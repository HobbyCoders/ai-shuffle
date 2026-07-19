/**
 * Smoke tests for the providers store (Chunk 2).
 *
 * Covers the model_providers registry cluster:
 *  1. `loadProviders` populates state from the list API.
 *  2. `toggleProvider` calls the toggle API, then refreshes (re-lists + re-hydrates
 *     the model registry).
 *  3. `refreshProviders` invokes `hydrateModelRegistry` (landmine L4 — every
 *     mutation must re-hydrate so the profile editor's picker stays fresh).
 *
 * The repo's convention is to mock the API module (see git.svelte.test.ts /
 * gitOperations.svelte.test.ts). Here we mock `$lib/api/model-providers` and the
 * `hydrateModelRegistry` coupling in `$lib/constants/models.svelte`.
 */

import { describe, it, expect, vi, beforeEach } from 'vitest';
import type { ModelProvider } from '$lib/api/model-providers';

const mocks = vi.hoisted(() => ({
	listModelProviders: vi.fn(),
	toggleModelProvider: vi.fn(),
	hydrateModelRegistry: vi.fn()
}));

vi.mock('$lib/api/model-providers', async (importOriginal) => {
	const actual = await importOriginal<typeof import('$lib/api/model-providers')>();
	return {
		...actual,
		listModelProviders: mocks.listModelProviders,
		toggleModelProvider: mocks.toggleModelProvider
	};
});

vi.mock('$lib/constants/models.svelte', async (importOriginal) => {
	const actual = await importOriginal<typeof import('$lib/constants/models.svelte')>();
	return { ...actual, hydrateModelRegistry: mocks.hydrateModelRegistry };
});

import { providers } from './providers.svelte';

function provider(partial: Partial<ModelProvider>): ModelProvider {
	return {
		id: 'p1',
		name: 'OpenRouter',
		provider_type: 'openrouter',
		base_url: 'https://openrouter.ai/api',
		auth_token_set: true,
		auth_token_masked: 'sk-…abc',
		models: [],
		default_model: null,
		small_fast_model: null,
		extra_env: {},
		context_window: null,
		max_output_tokens: null,
		enabled: true,
		created_at: null,
		updated_at: null,
		...partial
	};
}

beforeEach(() => {
	mocks.listModelProviders.mockReset();
	mocks.toggleModelProvider.mockReset();
	mocks.hydrateModelRegistry.mockReset();
	mocks.hydrateModelRegistry.mockResolvedValue(undefined);
	providers.clear();
});

describe('providers store — model_providers registry', () => {
	it('loadProviders populates state from the list API', async () => {
		const rows = [provider({ id: 'p1' }), provider({ id: 'p2', name: 'Custom', provider_type: 'custom' })];
		mocks.listModelProviders.mockResolvedValue({ providers: rows });

		await providers.loadProviders();

		expect(mocks.listModelProviders).toHaveBeenCalledTimes(1);
		expect(providers.current.providers).toEqual(rows);
		expect(providers.current.providersLoading).toBe(false);
		expect(providers.current.providersError).toBe('');
	});

	it('toggleProvider calls the toggle API then refreshes (re-list + re-hydrate)', async () => {
		mocks.toggleModelProvider.mockResolvedValue(provider({ id: 'p1', enabled: false }));
		mocks.listModelProviders.mockResolvedValue({ providers: [provider({ id: 'p1', enabled: false })] });

		await providers.toggleProvider('p1');

		expect(mocks.toggleModelProvider).toHaveBeenCalledWith('p1');
		// refreshProviders() ran: list re-fetched AND the registry re-hydrated (L4).
		expect(mocks.listModelProviders).toHaveBeenCalledTimes(1);
		expect(mocks.hydrateModelRegistry).toHaveBeenCalledTimes(1);
		expect(providers.current.togglingProviderId).toBe(null);
		expect(providers.current.providers).toEqual([provider({ id: 'p1', enabled: false })]);
	});

	it('refreshProviders invokes hydrateModelRegistry (landmine L4)', async () => {
		mocks.listModelProviders.mockResolvedValue({ providers: [] });

		await providers.refreshProviders();

		expect(mocks.hydrateModelRegistry).toHaveBeenCalledTimes(1);
		expect(mocks.listModelProviders).toHaveBeenCalledTimes(1);
	});
});
