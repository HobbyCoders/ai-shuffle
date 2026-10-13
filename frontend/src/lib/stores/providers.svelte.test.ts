/**
 * Smoke tests for the providers store.
 *
 * Covers the model_providers registry cluster:
 *  1. `loadProviders` populates state from the list API.
 *  2. `setProviderEnabled` PATCHes `{enabled}`, then refreshes (re-lists + re-hydrates
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
	updateModelProvider: vi.fn(),
	hydrateModelRegistry: vi.fn(),
	getAuthStatus: vi.fn()
}));

vi.mock('$lib/api/model-providers', async (importOriginal) => {
	const actual = await importOriginal<typeof import('$lib/api/model-providers')>();
	return {
		...actual,
		listModelProviders: mocks.listModelProviders,
		updateModelProvider: mocks.updateModelProvider
	};
});

vi.mock('$lib/api/auth', async (importOriginal) => {
	const actual = await importOriginal<typeof import('$lib/api/auth')>();
	return {
		...actual,
		getAuthStatus: mocks.getAuthStatus
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
	mocks.updateModelProvider.mockReset();
	mocks.hydrateModelRegistry.mockReset();
	mocks.getAuthStatus.mockReset();
	mocks.hydrateModelRegistry.mockResolvedValue(undefined);
	mocks.getAuthStatus.mockResolvedValue({ authenticated: true });
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

	it('setProviderEnabled PATCHes {enabled} then refreshes (re-list + re-hydrate)', async () => {
		mocks.updateModelProvider.mockResolvedValue(provider({ id: 'p1', enabled: false }));
		mocks.listModelProviders.mockResolvedValue({ providers: [provider({ id: 'p1', enabled: false })] });

		await providers.setProviderEnabled('p1', false);

		expect(mocks.updateModelProvider).toHaveBeenCalledWith('p1', { enabled: false });
		// refreshProviders() ran: list re-fetched AND the registry re-hydrated (L4).
		expect(mocks.listModelProviders).toHaveBeenCalledTimes(1);
		expect(mocks.hydrateModelRegistry).toHaveBeenCalledTimes(1);
		expect(providers.current.togglingProviderId).toBe(null);
		expect(providers.current.providers).toEqual([provider({ id: 'p1', enabled: false })]);
	});

	it('setProviderEnabled rolls the switch back and rethrows when the save fails', async () => {
		mocks.listModelProviders.mockResolvedValue({ providers: [provider({ id: 'p1', enabled: true })] });
		await providers.loadProviders();
		mocks.updateModelProvider.mockRejectedValue({ status: 500, detail: 'nope' });

		await expect(providers.setProviderEnabled('p1', false)).rejects.toEqual({ status: 500, detail: 'nope' });

		expect(providers.current.providers[0].enabled).toBe(true);
		expect(providers.current.togglingProviderId).toBe(null);
	});

	it('refreshProviders invokes hydrateModelRegistry (landmine L4)', async () => {
		mocks.listModelProviders.mockResolvedValue({ providers: [] });

		await providers.refreshProviders();

		expect(mocks.hydrateModelRegistry).toHaveBeenCalledTimes(1);
		expect(mocks.listModelProviders).toHaveBeenCalledTimes(1);
	});
});

describe('providers store — Anthropic API key integration', () => {
	it('loadIntegrations populates native API key status from /settings/integrations', async () => {
		const fetchMock = vi.fn().mockResolvedValue({
			ok: true,
			json: async () => ({
				anthropic_api_key_set: true,
				fal_api_key_set: true,
				anthropic_api_key_masked: 'sk-ant-…zzz',
				claude_code_auth_method: 'api_key'
			})
		});
		vi.stubGlobal('fetch', fetchMock);

		await providers.loadIntegrations();

		expect(fetchMock).toHaveBeenCalledWith('/api/v1/settings/integrations', {
			redirect: 'error', credentials: 'include'
		});
		expect(providers.current.anthropicKeySet).toBe(true);
		expect(providers.current.falKeySet).toBe(true);
		expect(providers.current.anthropicKeyMasked).toBe('sk-ant-…zzz');
		expect(providers.current.claudeCodeAuthMethod).toBe('api_key');
		expect(providers.current.integrationsError).toBe(null);

		vi.unstubAllGlobals();
	});

	it('setAnthropicKey POSTs the key then reloads the integration state', async () => {
		const fetchMock = vi
			.fn()
			// 1) POST /anthropic
			.mockResolvedValueOnce({ ok: true, json: async () => ({ masked_key: 'sk-ant-…new' }) })
			// 2) reload GET /integrations
			.mockResolvedValueOnce({
				ok: true,
				json: async () => ({
					anthropic_api_key_set: true,
					anthropic_api_key_masked: 'sk-ant-…new',
					claude_code_auth_method: 'api_key'
				})
			});
		vi.stubGlobal('fetch', fetchMock);

		await providers.setAnthropicKey('sk-ant-secret');

		expect(fetchMock).toHaveBeenCalledTimes(2);
		expect(fetchMock.mock.calls[0][0]).toBe('/api/v1/settings/integrations/anthropic');
		expect(fetchMock.mock.calls[0][1].method).toBe('POST');
		// State reflects the reload, not a local write.
		expect(providers.current.anthropicKeySet).toBe(true);
		expect(providers.current.anthropicKeyMasked).toBe('sk-ant-…new');
		expect(providers.current.claudeCodeAuthMethod).toBe('api_key');

		vi.unstubAllGlobals();
	});

	it('setAnthropicKey throws the server message on failure', async () => {
		const fetchMock = vi
			.fn()
			.mockResolvedValueOnce({ ok: false, status: 400, json: async () => ({ detail: 'bad key' }) });
		vi.stubGlobal('fetch', fetchMock);

		await expect(providers.setAnthropicKey('sk-ant-secret')).rejects.toThrow('bad key');
		// No reload happened after a failed save.
		expect(fetchMock).toHaveBeenCalledTimes(1);

		vi.unstubAllGlobals();
	});

	it('clearAnthropicKey DELETEs then reloads the integration state', async () => {
		const fetchMock = vi
			.fn()
			// 1) DELETE /anthropic
			.mockResolvedValueOnce({ ok: true })
			// 2) reload GET /integrations → now unset
			.mockResolvedValueOnce({
				ok: true,
				json: async () => ({ anthropic_api_key_set: false, anthropic_api_key_masked: null })
			});
		vi.stubGlobal('fetch', fetchMock);

		await providers.clearAnthropicKey();

		expect(fetchMock).toHaveBeenCalledTimes(2);
		expect(fetchMock.mock.calls[0][0]).toBe('/api/v1/settings/integrations/anthropic');
		expect(fetchMock.mock.calls[0][1].method).toBe('DELETE');
		expect(providers.current.anthropicKeySet).toBe(false);
		expect(providers.current.anthropicKeyMasked).toBe(null);
		expect(providers.current.claudeCodeAuthMethod).toBe('account');

		vi.unstubAllGlobals();
	});
});

describe('providers store — refreshAll silent refetch', () => {
	it('refetches without raising loading flags when data is already present', async () => {
		mocks.listModelProviders.mockResolvedValue({ providers: [provider({ id: 'p1' })] });
		vi.stubGlobal(
			'fetch',
			vi.fn().mockResolvedValue({ ok: true, json: async () => ({ anthropic_api_key_set: false }) })
		);

		await providers.loadProviders();
		expect(providers.current.providers.length).toBe(1);

		// Re-arm the list API with an in-flight promise so we can inspect state mid-refresh.
		let resolveList!: (v: unknown) => void;
		mocks.listModelProviders.mockReturnValueOnce(
			new Promise((r) => {
				resolveList = r;
			})
		);

		const done = providers.refreshAll();

		// Data already present + silent → the loading flag never rose.
		expect(providers.current.providersLoading).toBe(false);

		resolveList({ providers: [provider({ id: 'p1' })] });
		await done;

		// The refetch still hit the network (list API called a second time).
		expect(mocks.listModelProviders).toHaveBeenCalledTimes(2);

		vi.unstubAllGlobals();
	});
});

describe('providers store — fal.ai and Claude mode', () => {
	it('loadIntegrations reads the fal key and its on/off flag (unset means on)', async () => {
		vi.stubGlobal('fetch', vi.fn()
			.mockResolvedValueOnce({ ok: true, json: async () => ({ fal_api_key_set: true, fal_api_key_masked: '…9c1e', fal_enabled: false }) })
			.mockResolvedValueOnce({ ok: true, json: async () => ({ fal_api_key_set: true }) }));
		await providers.loadIntegrations();
		expect(providers.current.falKeyMasked).toBe('…9c1e');
		expect(providers.current.falEnabled).toBe(false);
		await providers.loadIntegrations();
		expect(providers.current.falEnabled).toBe(true);
		vi.unstubAllGlobals();
	});

	it('setFalEnabled PUTs the flag, keeps the answer and rolls back on failure', async () => {
		const client = await import('$lib/api/client');
		const put = vi.spyOn(client.api, 'put').mockResolvedValue({ fal_enabled: false, fal_api_key_set: true });
		await providers.setFalEnabled(false);
		expect(put).toHaveBeenCalledWith('/settings/integrations/fal-ai/enabled', { enabled: false });
		expect(providers.current.falEnabled).toBe(false);
		put.mockRejectedValueOnce({ status: 500 });
		await expect(providers.setFalEnabled(true)).rejects.toEqual({ status: 500 });
		expect(providers.current.falEnabled).toBe(false);
		put.mockRestore();
	});

	it('setClaudeMode stores the mode, off included, and re-reads the model registry', async () => {
		const client = await import('$lib/api/client');
		const summary = { mode: 'off', mode_label: 'Off', explicit: true, modes: [], cli: {}, api_key: { set: false, last4: null } };
		const put = vi.spyOn(client.api, 'put').mockResolvedValue(summary);
		vi.stubGlobal('fetch', vi.fn().mockResolvedValue({ ok: false, json: async () => ({}) }));
		await providers.setClaudeMode('off');
		expect(put).toHaveBeenCalledWith('/settings/claude-auth', { mode: 'off' });
		expect(providers.current.claude?.mode).toBe('off');
		expect(mocks.hydrateModelRegistry).toHaveBeenCalled();
		put.mockRestore();
		vi.unstubAllGlobals();
	});

	it('loadClaude marks a 403 as forbidden without an error', async () => {
		const client = await import('$lib/api/client');
		const get = vi.spyOn(client.api, 'get').mockRejectedValue({ status: 403, detail: 'Admin only' });
		await providers.loadClaude();
		expect(providers.current.claudeForbidden).toBe(true);
		expect(providers.current.claudeError).toBe(null);
		get.mockRestore();
	});
});
