/**
 * Providers Store — unified model-source state for Settings > Providers.
 *
 * Owns the state the Providers collection reads:
 *
 *   1. The `/settings/integrations` credentials: the Anthropic API key and the
 *      fal.ai media key with its on/off flag. (Claude sign-in is the stock
 *      CLI's own; see `claude-cli-login.svelte.ts` and `$lib/api/claude-cli`.)
 *   2. The Claude Code mode summary (`/settings/claude-auth`): subscription,
 *      API key or off.
 *   3. The `model_providers` registry: load / save / enable / delete / fetch.
 *      `refreshProviders()` funnels EVERY mutation through
 *      `hydrateModelRegistry()` so the model pickers never go stale.
 *   4. The usage snapshot (`/usage/limits`) the Usage tabs and facts read.
 *
 * Follows the existing store convention (see `github.svelte.ts`): a single
 * `$state` object + the `createSubscribers` subscribe shim, exposed as an object
 * with a `get current()` getter, `subscribe`, and action methods. Components read
 * the store directly — no prop drill.
 */

import { apiBase, withAuth } from '$lib/api/runtime';
import { auth } from './auth.svelte';
import { createSubscribers } from './store-utils';
import { hydrateModelRegistry } from '$lib/constants/models.svelte';
import { api, getErrorMessage, getUsageLimits, type UsageLimitsSnapshot } from '$lib/api/client';
import {
	listModelProviders,
	createModelProvider,
	updateModelProvider,
	deleteModelProvider,
	fetchProviderModels as apiFetchProviderModels,
	type ModelProvider,
	type ModelProviderCreate,
	type FetchModelsResult
} from '$lib/api/model-providers';
import {
	clearClaudeCloud as apiClearClaudeCloud,
	getClaudeAuth,
	saveClaudeCloud as apiSaveClaudeCloud,
	setClaudeAuthMode,
	type ClaudeAuthMode,
	type ClaudeAuthSummary,
	type ClaudeCloudConfig,
	type ClaudeCloudMethodId
} from '$lib/api/claude-auth';

// ============================================================================
// Types
// ============================================================================

interface ProvidersState {
	// ----- model_providers registry -----
	providers: ModelProvider[];
	providersLoading: boolean;
	providersError: string;
	/** Id whose enabled flag is being saved, or null. */
	togglingProviderId: string | null;

	// ----- Integrations (from /settings/integrations) -----
	/** True when an Anthropic API key is stored. */
	anthropicKeySet: boolean;
	falKeySet: boolean;
	/** Masked fal.ai media key, '' when none is set. */
	falKeyMasked: string;
	/** fal.ai on/off. Unset on the worker means on. */
	falEnabled: boolean;
	/** Masked form of the stored key, or null when none is set. */
	anthropicKeyMasked: string | null;
	/** Which auth method Claude Code runs with: 'api_key', 'account', 'cloud' or 'off'. */
	claudeCodeAuthMethod: 'account' | 'api_key' | 'cloud' | 'off' | null;
	/** Non-null when the integrations load failed unexpectedly. */
	integrationsError: string | null;
	/** True once `/settings/integrations` answered. */
	integrationsLoaded: boolean;

	// ----- Claude Code mode (from /settings/claude-auth) -----
	claude: ClaudeAuthSummary | null;
	claudeError: string | null;
	/** The worker refused the read (an unapproved device). */
	claudeForbidden: boolean;

	// ----- Usage snapshot (from /usage/limits) -----
	usage: UsageLimitsSnapshot | null;
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
	falKeyMasked: '',
	falEnabled: true,
	anthropicKeyMasked: null,
	claudeCodeAuthMethod: null,
	integrationsError: null,
	integrationsLoaded: false,

	claude: null,
	claudeError: null,
	claudeForbidden: false,

	usage: null
};

// ============================================================================
// Reactive State
// ============================================================================

let _state = $state<ProvidersState>({ ...initialState });

/**
 * One-shot guard so `ensureLoaded()` fetches once per session. Reset by
 * `clear()`. Not `$state` — a plain module-scope latch, never rendered.
 */
let _initialLoadStarted = false;

const { subscribe, update: _update, notifySubscribers: _notifySubscribers } =
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
	// Loading
	// ------------------------------------------------------------------------

	/** Load providers and integrations once per session. */
	async ensureLoaded() {
		if (_initialLoadStarted) return;
		_initialLoadStarted = true;
		await Promise.all([providers.loadProviders(), providers.loadIntegrations()]);
	},

	/**
	 * Refetch everything on every open, silently: each loader raises its loading
	 * flag only while its data is still empty, so rendered rows never flicker.
	 */
	async refreshAll() {
		_initialLoadStarted = true;
		await Promise.all([
			providers.loadProviders(true),
			providers.loadIntegrations(),
			providers.loadClaude()
		]);
	},

	async loadProviders(silent = false) {
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
	// Integrations: Anthropic API key and fal.ai media key
	// ------------------------------------------------------------------------

	/**
	 * Load the key status from the shared `/settings/integrations` payload.
	 * Never throws — a failed load surfaces via `integrationsError`.
	 */
	async loadIntegrations() {
		try {
			const res = await fetch(`${apiBase()}/settings/integrations`, withAuth({}));
			if (!res.ok) return;
			const data = await res.json();
			_update((s) => ({
				...s,
				anthropicKeySet: !!data.anthropic_api_key_set,
				falKeySet: !!data.fal_api_key_set,
				falKeyMasked: data.fal_api_key_masked ?? '',
				// Older workers do not report the flag: the key is then always on.
				falEnabled: data.fal_enabled !== false,
				anthropicKeyMasked: data.anthropic_api_key_masked ?? null,
				claudeCodeAuthMethod:
					data.claude_code_auth_method ?? (data.anthropic_api_key_set ? 'api_key' : 'account'),
				integrationsError: null,
				integrationsLoaded: true
			}));
		} catch (e) {
			_update((s) => ({ ...s, integrationsError: getErrorMessage(e, 'Failed to load integrations') }));
		}
	},

	/**
	 * Store an Anthropic API key, then reload the integration status (callers
	 * re-read the Claude summary). Saving a key never changes the mode unless
	 * `useForClaudeCode` asks for it. Throws with the server's message.
	 */
	async setAnthropicKey(key: string, options: { useForClaudeCode?: boolean } = {}) {
		const trimmed = key.trim();
		if (!trimmed) return;
		let res: Response;
		try {
			res = await fetch(`${apiBase()}/settings/integrations/anthropic`, withAuth({
				method: 'POST',
				headers: { 'content-type': 'application/json' },
				body: JSON.stringify(
					options.useForClaudeCode ? { api_key: trimmed, use_for_claude_code: true } : { api_key: trimmed }
				)
			}));
		} catch (e) {
			throw new Error(e instanceof Error ? e.message : 'Network error');
		}
		if (!res.ok) {
			const body = await res.json().catch(() => ({}));
			throw new Error(body.detail ?? `Save failed (${res.status})`);
		}
		await providers.loadIntegrations();
	},

	/** Remove the stored Anthropic key, then reload status. Throws on failure. */
	async clearAnthropicKey() {
		let res: Response;
		try {
			res = await fetch(`${apiBase()}/settings/integrations/anthropic`, withAuth({ method: 'DELETE' }));
		} catch (e) {
			throw new Error(e instanceof Error ? e.message : 'Network error');
		}
		if (!res.ok) throw new Error(`Remove failed (${res.status})`);
		await providers.loadIntegrations();
	},

	/**
	 * Save the fal.ai media key (it also reads the balance). Returns whether fal
	 * verified it. Throws with the server's message.
	 */
	async setFalKey(key: string): Promise<{ valid: boolean; masked: string }> {
		const trimmed = key.trim();
		let res: Response;
		try {
			res = await fetch(`${apiBase()}/settings/integrations/fal-ai`, withAuth({
				method: 'POST',
				headers: { 'content-type': 'application/json' },
				body: JSON.stringify({ api_key: trimmed })
			}));
		} catch (e) {
			throw new Error(e instanceof Error ? e.message : 'Network error');
		}
		const data = await res.json().catch(() => ({}));
		if (!res.ok) throw new Error(data.detail ?? `Save failed (${res.status})`);
		await providers.loadIntegrations();
		return { valid: !!data.valid, masked: data.masked_key ?? '' };
	},

	/** Remove the fal.ai media key. Throws on failure. */
	async clearFalKey() {
		let res: Response;
		try {
			res = await fetch(`${apiBase()}/settings/integrations/fal-ai`, withAuth({ method: 'DELETE' }));
		} catch (e) {
			throw new Error(e instanceof Error ? e.message : 'Network error');
		}
		if (!res.ok) throw new Error(`Remove failed (${res.status})`);
		await providers.loadIntegrations();
	},

	/** Turn fal.ai on or off without touching its key. Optimistic; throws on failure. */
	async setFalEnabled(enabled: boolean) {
		const previous = _state.falEnabled;
		_update((s) => ({ ...s, falEnabled: enabled }));
		try {
			const res = await api.put<{ fal_enabled?: boolean; fal_api_key_set?: boolean }>(
				'/settings/integrations/fal-ai/enabled',
				{ enabled }
			);
			_update((s) => ({
				...s,
				falEnabled: typeof res?.fal_enabled === 'boolean' ? res.fal_enabled : enabled,
				falKeySet: typeof res?.fal_api_key_set === 'boolean' ? res.fal_api_key_set : s.falKeySet
			}));
		} catch (e) {
			_update((s) => ({ ...s, falEnabled: previous }));
			throw e;
		}
	},

	// ------------------------------------------------------------------------
	// Claude Code mode
	// ------------------------------------------------------------------------

	/** Read the mode, the CLI account, the key's last four and the gate. Never throws. */
	async loadClaude(refresh = false) {
		try {
			const claude = await getClaudeAuth(refresh);
			_update((s) => ({ ...s, claude, claudeError: null, claudeForbidden: false }));
		} catch (e) {
			const forbidden = (e as { status?: unknown } | null)?.status === 403;
			_update((s) => ({
				...s,
				claudeForbidden: forbidden,
				claudeError: forbidden ? null : getErrorMessage(e, 'Could not read the Claude Code mode')
			}));
		}
	},

	/**
	 * Store the mode (owner only): subscription, API key, or off. The model
	 * registry is re-read so the pickers add or drop Claude. Throws.
	 */
	async setClaudeMode(mode: ClaudeAuthMode, acknowledgeWarning = false) {
		const claude = await setClaudeAuthMode(mode, acknowledgeWarning);
		_update((s) => ({ ...s, claude }));
		auth.checkAuth().catch(() => {});
		await Promise.all([hydrateModelRegistry(), providers.loadIntegrations()]);
	},

	/**
	 * Save the cloud provider or gateway (owner only; the warning was
	 * acknowledged in the form). `activate` also switches Claude to it, and
	 * then the pickers are re-read like a mode change. Throws.
	 */
	async saveClaudeCloud(
		method: ClaudeCloudMethodId,
		values: Record<string, string>,
		activate = false
	): Promise<ClaudeCloudConfig> {
		const { auth: claude, ...config } = await apiSaveClaudeCloud(method, values, { activate });
		_update((s) => ({ ...s, claude }));
		if (activate) {
			auth.checkAuth().catch(() => {});
			await Promise.all([hydrateModelRegistry(), providers.loadIntegrations()]);
		}
		return config;
	},

	/** Delete the cloud method and its secrets. The mode stays as it is. Throws. */
	async clearClaudeCloud() {
		const { auth: claude } = await apiClearClaudeCloud();
		_update((s) => ({ ...s, claude }));
		await hydrateModelRegistry();
	},

	// ------------------------------------------------------------------------
	// model_providers registry
	// ------------------------------------------------------------------------

	/**
	 * Refresh the provider list and the hydrated model registry together.
	 * EVERY provider mutation funnels through here so the pickers stay fresh.
	 */
	async refreshProviders() {
		await Promise.all([providers.loadProviders(), hydrateModelRegistry()]);
	},

	/**
	 * Create (no `editingProviderId`) or update a provider, then refresh.
	 * Throws on failure — the caller owns its error surface.
	 */
	async saveProvider(body: ModelProviderCreate, editingProviderId?: string | null) {
		if (editingProviderId) {
			await updateModelProvider(editingProviderId, body);
		} else {
			await createModelProvider(body);
		}
		await providers.refreshProviders();
	},

	/**
	 * Turn one credential row on or off. Idempotent (PATCH `{enabled}`), never
	 * the flipping `/toggle`, so a double click or a stale view cannot invert
	 * it. Optimistic; throws with the server's message after rolling back.
	 */
	async setProviderEnabled(id: string, enabled: boolean) {
		_update((s) => ({
			...s,
			togglingProviderId: id,
			providersError: '',
			providers: s.providers.map((p) => (p.id === id ? { ...p, enabled } : p))
		}));
		try {
			await updateModelProvider(id, { enabled });
			await providers.refreshProviders();
		} catch (e) {
			_update((s) => ({
				...s,
				providers: s.providers.map((p) => (p.id === id ? { ...p, enabled: !enabled } : p))
			}));
			throw e;
		} finally {
			_update((s) => ({ ...s, togglingProviderId: null }));
		}
	},

	/**
	 * Create a custom gateway: its own provider in Settings and its own group in
	 * the model picker. `api_dialect` is the wire format its endpoint speaks.
	 * Throws on failure.
	 */
	async createGateway(body: {
		name: string;
		base_url: string;
		api_dialect: 'anthropic' | 'openai';
		auth_token?: string | null;
	}): Promise<ModelProvider> {
		const token = body.auth_token?.trim();
		const row = await createModelProvider({
			name: body.name.trim(),
			provider_type: 'custom',
			base_url: body.base_url.trim(),
			api_dialect: body.api_dialect,
			...(token ? { auth_token: token } : {})
		});
		await providers.refreshProviders();
		return row;
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
	 * preset roster). Persists nothing; throws on failure.
	 */
	async fetchProviderModels(id: string): Promise<FetchModelsResult> {
		return apiFetchProviderModels(id);
	},

	// ------------------------------------------------------------------------
	// Usage snapshot
	// ------------------------------------------------------------------------

	/** Subscription windows, credits and balances. Never throws. */
	async loadUsage(refresh = false) {
		try {
			const usage = await getUsageLimits(refresh);
			_update((s) => ({ ...s, usage }));
		} catch {
			// Optional detail: the Usage tabs say "Not reported yet".
		}
	},

	/** Reset the whole store (test/teardown helper). */
	clear() {
		_state = { ...initialState };
		_initialLoadStarted = false;
		_notifySubscribers();
	}
};
