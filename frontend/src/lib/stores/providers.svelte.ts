/**
 * Providers Store — unified model-source state for the Settings "Providers" surface.
 *
 * Owns the two state clusters that `SettingsCard.svelte` / `ConnectionsSection.svelte`
 * currently prop-drill (see PROVIDERS_UI_RESTRUCTURE_PLAN.md §2.4, landmine L2):
 *
 *   1. The Anthropic API key integration status. (Claude sign-in is the stock
 *      CLI's own; see `ClaudeCliPanel` and `$lib/api/claude-cli`.)
 *   2. The `model_providers` registry: load / save / toggle / delete / fetch,
 *      lifted from `ConnectionsSection.svelte`. `refreshProviders()` funnels EVERY
 *      mutation through `hydrateModelRegistry()` so the profile editor's model
 *      picker never goes stale (landmine L4).
 *
 * Follows the existing store convention (see `github.svelte.ts`): a single
 * `$state` object + the `createSubscribers` subscribe shim, exposed as an object
 * with a `get current()` getter, `subscribe`, and action methods. Components read
 * the store directly — no prop drill.
 *
 * This chunk (Chunk 2) only CREATES the store; the originals in SettingsCard /
 * ConnectionsSection are untouched here. Chunks 3–5 rewire the components to
 * consume it and delete the lifted originals.
 */

import { apiBase, withAuth } from '$lib/api/runtime';
import { auth } from './auth.svelte';
import { createSubscribers } from './store-utils';
import { hydrateModelRegistry } from '$lib/constants/models.svelte';
import { getErrorMessage } from '$lib/api/client';
import {
	listModelProviders,
	createModelProvider,
	updateModelProvider,
	deleteModelProvider,
	toggleModelProvider,
	fetchProviderModels as apiFetchProviderModels,
	type ModelProvider,
	type ModelProviderCreate,
	type FetchModelsResult
} from '$lib/api/model-providers';

// ============================================================================
// Types
// ============================================================================

interface ProvidersState {
	// ----- model_providers registry -----
	providers: ModelProvider[];
	providersLoading: boolean;
	providersError: string;
	/** Id mid-toggle (drives its inline spinner), or null. */
	togglingProviderId: string | null;

	// ----- Anthropic API key integration (from /settings/integrations) -----
	/** True when an Anthropic API key is stored (overrides the connected account). */
	anthropicKeySet: boolean;
	falKeySet: boolean;
	/** Masked form of the stored key, or null when none is set. */
	anthropicKeyMasked: string | null;
	/** Which auth method Claude Code runs with: 'api_key' overrides 'account'. */
	claudeCodeAuthMethod: 'account' | 'api_key' | null;
	/** Non-null when the integrations load failed unexpectedly. */
	integrationsError: string | null;
}

// ============================================================================
// Initial State
// ============================================================================

const initialState: ProvidersState = {
	providers: [],
	providersLoading: false,
	providersError: '',
	togglingProviderId: null,

	anthropicKeySet: false,
	falKeySet: false,
	anthropicKeyMasked: null,
	claudeCodeAuthMethod: null,
	integrationsError: null
};

// ============================================================================
// Reactive State
// ============================================================================

let _state = $state<ProvidersState>({ ...initialState });

/**
 * One-shot guard for the first Providers-tab open (landmine L1 / ruling PR6).
 * The settings sections remount on every tab switch and re-run their `onMount`
 * fetches; this flag lets `ensureLoaded()` cache the provider and integration loads
 * across those remounts so switching to Providers doesn't refetch every time.
 * Reset by `clear()`. Not `$state` — it's a plain module-scope latch, never
 * rendered.
 */
let _initialLoadStarted = false;

const { subscribe, update: _update, notifySubscribers: _notifySubscribers, createDerived } =
	createSubscribers<ProvidersState>(
		() => _state,
		(v) => {
			_state = v;
		}
	);

// ============================================================================
// Exported Store
// ============================================================================

export const providers = {
	get current() {
		return _state;
	},

	subscribe,

	// ------------------------------------------------------------------------
	// model_providers registry (lifted from ConnectionsSection:454-609)
	// ------------------------------------------------------------------------

	/**
	 * Load providers and integrations exactly once per session (L1/PR6). Safe to
	 * call from every `ProvidersSection` mount — only the first call fetches; later
	 * mounts read the already-populated store. Mutations refresh independently, so
	 * the cache never goes stale after a create/toggle/delete/switch.
	 */
	async ensureLoaded() {
		if (_initialLoadStarted) return;
		_initialLoadStarted = true;
		await Promise.all([
			providers.loadProviders(),
			providers.loadIntegrations()
		]);
	},

	/**
	 * Refetch EVERYTHING on every card-open / tab-switch (ruling: fixes the stuck
	 * "Not connected" bug where a Codex sign-in that finishes after the UI stopped
	 * watching never surfaced). Unlike `ensureLoaded`, this ignores the one-shot
	 * latch and always hits the network — but SILENTLY: each loader flips its
	 * loading flag only when its data is still empty, so already-rendered rows
	 * don't flicker while the background refresh runs.
	 */
	async refreshAll() {
		_initialLoadStarted = true;
		await Promise.all([
			providers.loadProviders(true),
			providers.loadIntegrations()
		]);
	},

	async loadProviders(silent = false) {
		// SILENT background refresh: keep the loading flag down when rows already
		// render, so a re-open refetch doesn't flash the "Loading…" placeholder.
		const showLoading = !silent || _state.providers.length === 0;
		_update((s) => ({ ...s, providersError: '', ...(showLoading ? { providersLoading: true } : {}) }));
		try {
			const res = await listModelProviders();
			_update((s) => ({ ...s, providers: res.providers ?? [] }));
		} catch (e) {
			_update((s) => ({ ...s, providersError: getErrorMessage(e, 'Failed to load providers') }));
		} finally {
			if (showLoading) _update((s) => ({ ...s, providersLoading: false }));
		}
	},

	// ------------------------------------------------------------------------
	// Anthropic API key integration (lifted from ClaudeApiKeyRow's raw fetches —
	// mechanics unchanged, relocated into the store so ProviderList reads it from
	// one place and refreshAll can refetch it on every open).
	// ------------------------------------------------------------------------

	/**
	 * Load the Anthropic-key status from the shared `/settings/integrations`
	 * payload. Never throws — a failed load surfaces via `integrationsError` (the
	 * original row silently swallowed load errors; the field lets callers react).
	 */
	async loadIntegrations() {
		try {
			const res = await fetch(`${apiBase()}/settings/integrations`, withAuth({ }));
			if (!res.ok) return;
			const data = await res.json();
			_update((s) => ({
				...s,
				anthropicKeySet: !!data.anthropic_api_key_set,
				falKeySet: !!data.fal_api_key_set,
				anthropicKeyMasked: data.anthropic_api_key_masked ?? null,
				claudeCodeAuthMethod:
					data.claude_code_auth_method ?? (data.anthropic_api_key_set ? 'api_key' : 'account'),
				integrationsError: null
			}));
		} catch (e) {
			_update((s) => ({ ...s, integrationsError: getErrorMessage(e, 'Failed to load integrations') }));
		}
	},

	/**
	 * Store an Anthropic API key (Claude Code then authenticates with it, overriding
	 * the connected account), then reload the integration status. Throws with the
	 * server's message on failure so the caller owns its own error surface — mirrors
	 * ClaudeApiKeyRow's original save (which did NOT re-check auth).
	 */
	async setAnthropicKey(key: string) {
		const trimmed = key.trim();
		if (!trimmed) return;
		let res: Response;
		try {
			res = await fetch(`${apiBase()}/settings/integrations/anthropic`, withAuth({
				method: 'POST',
				headers: { 'content-type': 'application/json' },
				body: JSON.stringify({ api_key: trimmed })
			}));
		} catch (e) {
			throw new Error(e instanceof Error ? e.message : 'Network error');
		}
		if (!res.ok) {
			const body = await res.json().catch(() => ({ }));
			throw new Error(body.detail ?? `Save failed (${res.status})`);
		}
		await providers.loadIntegrations();
	},

	/**
	 * Remove the stored Anthropic key (Claude Code reverts to the connected
	 * account), then reload status. Throws on failure (mirrors the original remove).
	 */
	async clearAnthropicKey() {
		let res: Response;
		try {
			res = await fetch(`${apiBase()}/settings/integrations/anthropic`, withAuth({
				method: 'DELETE' }));
		} catch (e) {
			throw new Error(e instanceof Error ? e.message : 'Network error');
		}
		if (!res.ok) throw new Error(`Remove failed (${res.status})`);
		await providers.loadIntegrations();
	},

	/**
	 * Refresh both the provider list and the hydrated model registry together
	 * (they're independent reads — no need to sequence them). EVERY provider
	 * mutation funnels through here so the profile editor's picker stays fresh
	 * (landmine L4).
	 */
	async refreshProviders() {
		await Promise.all([providers.loadProviders(), hydrateModelRegistry()]);
	},

	/**
	 * Create (when `editingProviderId` is null/undefined) or update a provider,
	 * then refresh. Throws on failure — the caller (the Add/edit form component)
	 * owns its own error surface, since form state is component-local per §2.4.
	 */
	async saveProvider(body: ModelProviderCreate, editingProviderId?: string | null) {
		if (editingProviderId) {
			await updateModelProvider(editingProviderId, body);
		} else {
			await createModelProvider(body);
		}
		await providers.refreshProviders();
	},

	async toggleProvider(id: string) {
		_update((s) => ({ ...s, togglingProviderId: id, providersError: '' }));
		try {
			await toggleModelProvider(id);
			await providers.refreshProviders();
		} catch (e) {
			_update((s) => ({ ...s, providersError: getErrorMessage(e, 'Could not update provider') }));
		} finally {
			_update((s) => ({ ...s, togglingProviderId: null }));
		}
	},

	async deleteProvider(id: string) {
		_update((s) => ({ ...s, providersError: '' }));
		try {
			await deleteModelProvider(id);
			await providers.refreshProviders();
		} catch (e) {
			_update((s) => ({ ...s, providersError: getErrorMessage(e, 'Could not delete provider') }));
		}
	},

	/**
	 * Fetch a provider's model catalog (upstream `/models`, or the built-in
	 * preset roster). Thin passthrough — returns the raw result and throws on
	 * failure so the caller (the Manage-models drawer) owns the check-select UI
	 * and its own error surface.
	 */
	async fetchProviderModels(id: string): Promise<FetchModelsResult> {
		return apiFetchProviderModels(id);
	},

	/** Reset the whole store (test/teardown helper). */
	clear() {
		_state = { ...initialState };
		_initialLoadStarted = false;
		_notifySubscribers();
	}
};

// ============================================================================
// Derived stores
// ============================================================================

export const modelProvidersList = createDerived(() => _state.providers);
