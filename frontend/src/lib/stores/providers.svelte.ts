/**
 * Providers Store — unified model-source state for the Settings "Providers" surface.
 *
 * Owns the two state clusters that `SettingsCard.svelte` / `ConnectionsSection.svelte`
 * currently prop-drill (see PROVIDERS_UI_RESTRUCTURE_PLAN.md §2.4, landmine L2):
 *
 *   1. Claude native-auth: the OAuth connect/paste-code flow + the saved-accounts
 *      list (switch / rename / delete / add). Lifted verbatim from
 *      `SettingsCard.svelte` — SAME `$lib/api/auth` calls, SAME `auth.checkAuth()`
 *      / `rateLimit.markStale()` side effects, SAME error handling (D8: mechanics
 *      unchanged, presentation relocated in later chunks).
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

import { auth } from './auth.svelte';
import { rateLimit } from './rate-limit';
import { createSubscribers } from './store-utils';
import { hydrateModelRegistry } from '$lib/constants/models.svelte';
import { getErrorMessage } from '$lib/api/client';
import {
	startClaudeLogin,
	completeClaudeLogin,
	listClaudeAccounts,
	startAddClaudeAccount,
	completeAddClaudeAccount,
	activateClaudeAccount,
	renameClaudeAccount,
	deleteClaudeAccount,
	type ClaudeAccount
} from '$lib/api/auth';
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
	// ----- Claude native-auth: primary connect / paste-code flow -----
	claudeLoginLoading: boolean;
	claudeOAuthUrl: string | null;
	/** PKCE state echoed back on complete; not shown to the user. */
	claudeOAuthState: string | null;
	claudeAuthCode: string;
	claudeCompletingLogin: boolean;
	/**
	 * Error surface for the primary Claude OAuth flow. In SettingsCard these
	 * failures wrote to the card-wide `error` banner; the store owns a dedicated
	 * field so it stays self-contained (mechanics — the messages — unchanged).
	 */
	claudeAuthError: string;

	// ----- Claude native-auth: saved-accounts list -----
	claudeAccounts: ClaudeAccount[];
	activeAccountId: string | null;
	/** When false the encrypted store can't be opened — show a banner, not rows. */
	accountsEncryptionReady: boolean;
	accountsLoading: boolean;
	accountsError: string;

	// ----- Claude native-auth: add-account flow -----
	addAccountOAuthUrl: string | null;
	addAccountState: string | null;
	addAccountCode: string;
	addAccountName: string;
	addAccountMakeActive: boolean;
	addAccountLoading: boolean;
	addAccountCompleting: boolean;
	/** Id of the row mid-switch (drives its inline spinner), else null. */
	switchingAccountId: string | null;

	// ----- model_providers registry -----
	providers: ModelProvider[];
	providersLoading: boolean;
	providersError: string;
	/** Id mid-toggle (drives its inline spinner), or null. */
	togglingProviderId: string | null;

	// ----- Anthropic API key integration (from /settings/integrations) -----
	/** True when an Anthropic API key is stored (overrides the connected account). */
	anthropicKeySet: boolean;
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
	claudeLoginLoading: false,
	claudeOAuthUrl: null,
	claudeOAuthState: null,
	claudeAuthCode: '',
	claudeCompletingLogin: false,
	claudeAuthError: '',

	claudeAccounts: [],
	activeAccountId: null,
	accountsEncryptionReady: true,
	accountsLoading: false,
	accountsError: '',

	addAccountOAuthUrl: null,
	addAccountState: null,
	addAccountCode: '',
	addAccountName: '',
	addAccountMakeActive: false,
	addAccountLoading: false,
	addAccountCompleting: false,
	switchingAccountId: null,

	providers: [],
	providersLoading: false,
	providersError: '',
	togglingProviderId: null,

	anthropicKeySet: false,
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
 * fetches; this flag lets `ensureLoaded()` cache the provider + account loads
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

/** Pull a `{ detail }`-style message off an unknown caught value (account cluster
 *  convention — mirrors SettingsCard's `e?.detail || '…'`). */
function detailOf(e: unknown, fallback: string): string {
	return (e as { detail?: string })?.detail || fallback;
}

// ============================================================================
// Exported Store
// ============================================================================

export const providers = {
	get current() {
		return _state;
	},

	subscribe,

	// ------------------------------------------------------------------------
	// Input setters (were prop `onChange` callbacks from SettingsCard)
	// ------------------------------------------------------------------------

	setClaudeAuthCode(value: string) {
		_update((s) => ({ ...s, claudeAuthCode: value }));
	},
	setAddAccountCode(value: string) {
		_update((s) => ({ ...s, addAccountCode: value }));
	},
	setAddAccountName(value: string) {
		_update((s) => ({ ...s, addAccountName: value }));
	},
	setAddAccountMakeActive(value: boolean) {
		_update((s) => ({ ...s, addAccountMakeActive: value }));
	},

	// ------------------------------------------------------------------------
	// Claude native-auth: primary connect flow (lifted from SettingsCard:357-403)
	// ------------------------------------------------------------------------

	/**
	 * `forceReauth` drives the recovery path: it deletes the existing (possibly
	 * expired / unrefreshable) credential server-side and starts a fresh OAuth
	 * flow, so a green-but-dead token can be replaced from the UI. The non-forced
	 * path short-circuits when already authenticated.
	 */
	async startClaudeOAuth(forceReauth = false) {
		_update((s) => ({ ...s, claudeLoginLoading: true }));
		try {
			const result = await startClaudeLogin(forceReauth);
			if (result.already_authenticated) {
				await auth.checkAuth();
				return;
			}
			if (result.success && result.oauth_url && result.state) {
				_update((s) => ({
					...s,
					claudeOAuthUrl: result.oauth_url ?? null,
					claudeOAuthState: result.state ?? null,
					claudeAuthCode: ''
				}));
				window.open(result.oauth_url, '_blank', 'width=600,height=700');
			} else {
				_update((s) => ({
					...s,
					claudeAuthError: result.error || result.message || 'Failed to start OAuth'
				}));
			}
		} catch (e) {
			_update((s) => ({ ...s, claudeAuthError: detailOf(e, 'Failed to start OAuth') }));
		} finally {
			_update((s) => ({ ...s, claudeLoginLoading: false }));
		}
	},

	/** Force a fresh login even if a credential already exists on disk. */
	startClaudeReauth() {
		return providers.startClaudeOAuth(true);
	},

	async completeClaudeOAuth() {
		if (!_state.claudeAuthCode.trim() || !_state.claudeOAuthState) return;
		_update((s) => ({ ...s, claudeCompletingLogin: true }));
		try {
			const result = await completeClaudeLogin(
				_state.claudeAuthCode.trim(),
				_state.claudeOAuthState
			);
			if (!result.success) {
				_update((s) => ({
					...s,
					claudeAuthError: result.error || result.message || 'Failed to complete OAuth'
				}));
				return;
			}
			_update((s) => ({
				...s,
				claudeOAuthUrl: null,
				claudeOAuthState: null,
				claudeAuthCode: ''
			}));
			await auth.checkAuth();
		} catch (e) {
			_update((s) => ({ ...s, claudeAuthError: detailOf(e, 'Failed to complete OAuth') }));
		} finally {
			_update((s) => ({ ...s, claudeCompletingLogin: false }));
		}
	},

	// ------------------------------------------------------------------------
	// Claude native-auth: saved accounts (lifted from SettingsCard:405-533)
	// ------------------------------------------------------------------------

	/**
	 * Refresh the account list. Called on mount and after every mutation so the
	 * UI stays a mirror of the server (switching is instant server-side). Never
	 * throws — a locked store just flips `accountsEncryptionReady` false.
	 */
	async loadClaudeAccounts(silent = false) {
		// SILENT background refresh: don't flip the loading flag when rows are
		// already on screen, so a re-open refetch never flickers them.
		const showLoading = !silent || _state.claudeAccounts.length === 0;
		_update((s) => ({ ...s, accountsError: '', ...(showLoading ? { accountsLoading: true } : {}) }));
		try {
			const res = await listClaudeAccounts();
			_update((s) => ({
				...s,
				claudeAccounts: res.accounts,
				activeAccountId: res.active_account_id,
				accountsEncryptionReady: res.encryption_ready
			}));
		} catch (e) {
			// A locked store surfaces as a friendly banner via the flag, not an
			// error toast; only genuinely unexpected failures set accountsError.
			_update((s) => ({ ...s, accountsError: detailOf(e, 'Failed to load Claude accounts') }));
		} finally {
			if (showLoading) _update((s) => ({ ...s, accountsLoading: false }));
		}
	},

	/**
	 * Start OAuth for ADDING an account — opens the browser popup exactly like the
	 * primary connect flow, but hits the /accounts/start endpoint so the current
	 * login is untouched.
	 */
	async startAddAccount() {
		_update((s) => ({ ...s, addAccountLoading: true, accountsError: '' }));
		try {
			const result = await startAddClaudeAccount();
			if (result.success && result.oauth_url && result.state) {
				_update((s) => ({
					...s,
					addAccountOAuthUrl: result.oauth_url ?? null,
					addAccountState: result.state ?? null,
					addAccountCode: ''
				}));
				window.open(result.oauth_url, '_blank', 'width=600,height=700');
			} else {
				_update((s) => ({
					...s,
					accountsError: result.error || result.message || 'Failed to start sign-in'
				}));
			}
		} catch (e) {
			_update((s) => ({ ...s, accountsError: detailOf(e, 'Failed to start sign-in') }));
		} finally {
			_update((s) => ({ ...s, addAccountLoading: false }));
		}
	},

	async completeAddAccount() {
		if (!_state.addAccountCode.trim() || !_state.addAccountState) return;
		_update((s) => ({ ...s, addAccountCompleting: true, accountsError: '' }));
		try {
			await completeAddClaudeAccount(
				_state.addAccountCode.trim(),
				_state.addAccountState,
				_state.addAccountName.trim() || undefined,
				_state.addAccountMakeActive
			);
			// Reset the add-account flow.
			const wasMadeActive = _state.addAccountMakeActive;
			_update((s) => ({
				...s,
				addAccountOAuthUrl: null,
				addAccountState: null,
				addAccountCode: '',
				addAccountName: '',
				addAccountMakeActive: false
			}));
			await providers.loadClaudeAccounts();
			// If the new account became active, the app's live credentials changed —
			// refresh the token-valid stores.
			if (wasMadeActive) await auth.checkAuth();
		} catch (e) {
			// 409 = encryption locked while completing.
			_update((s) => ({ ...s, accountsError: detailOf(e, 'Failed to add account') }));
		} finally {
			_update((s) => ({ ...s, addAccountCompleting: false }));
		}
	},

	cancelAddAccount() {
		_update((s) => ({
			...s,
			addAccountOAuthUrl: null,
			addAccountState: null,
			addAccountCode: '',
			addAccountName: '',
			addAccountMakeActive: false,
			accountsError: ''
		}));
	},

	/**
	 * Switch the active account, then re-check auth: the credentials the app runs
	 * with just changed, so the token-valid stores must be refreshed.
	 */
	async activateAccount(id: string) {
		_update((s) => ({ ...s, switchingAccountId: id, accountsError: '' }));
		// Dim the usage indicator immediately — the shown numbers belong to the
		// outgoing account until the backend broadcasts the new one's snapshot.
		rateLimit.markStale();
		try {
			await activateClaudeAccount(id);
			await providers.loadClaudeAccounts();
			await auth.checkAuth();
		} catch (e) {
			// 409 = encryption locked, 404 = unknown id.
			_update((s) => ({ ...s, accountsError: detailOf(e, 'Failed to switch account') }));
		} finally {
			_update((s) => ({ ...s, switchingAccountId: null }));
		}
	},

	async renameAccount(id: string, name: string) {
		_update((s) => ({ ...s, accountsError: '' }));
		try {
			await renameClaudeAccount(id, name);
			await providers.loadClaudeAccounts();
		} catch (e) {
			_update((s) => ({ ...s, accountsError: detailOf(e, 'Failed to rename account') }));
		}
	},

	/**
	 * Delete surfaces the server's 409 "switch first" message verbatim so the user
	 * knows why an active-account delete was refused. Deleting the active account
	 * changed the app's credentials → re-check auth after a successful delete.
	 */
	async deleteAccount(id: string) {
		_update((s) => ({ ...s, accountsError: '' }));
		const wasActive = id === _state.activeAccountId;
		try {
			await deleteClaudeAccount(id);
			await providers.loadClaudeAccounts();
			if (wasActive) await auth.checkAuth();
		} catch (e) {
			_update((s) => ({ ...s, accountsError: detailOf(e, 'Failed to delete account') }));
		}
	},

	// ------------------------------------------------------------------------
	// model_providers registry (lifted from ConnectionsSection:454-609)
	// ------------------------------------------------------------------------

	/**
	 * Load providers + Claude accounts exactly once per session (L1/PR6). Safe to
	 * call from every `ProvidersSection` mount — only the first call fetches; later
	 * mounts read the already-populated store. Mutations refresh independently, so
	 * the cache never goes stale after a create/toggle/delete/switch.
	 */
	async ensureLoaded() {
		if (_initialLoadStarted) return;
		_initialLoadStarted = true;
		await Promise.all([
			providers.loadProviders(),
			providers.loadClaudeAccounts(),
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
			providers.loadClaudeAccounts(true),
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
			const res = await fetch('/api/v1/settings/integrations', { credentials: 'include' });
			if (!res.ok) return;
			const data = await res.json();
			_update((s) => ({
				...s,
				anthropicKeySet: !!data.anthropic_api_key_set,
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
			res = await fetch('/api/v1/settings/integrations/anthropic', {
				method: 'POST',
				credentials: 'include',
				headers: { 'content-type': 'application/json' },
				body: JSON.stringify({ api_key: trimmed })
			});
		} catch (e) {
			throw new Error(e instanceof Error ? e.message : 'Network error');
		}
		if (!res.ok) {
			const body = await res.json().catch(() => ({}));
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
			res = await fetch('/api/v1/settings/integrations/anthropic', {
				method: 'DELETE',
				credentials: 'include'
			});
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

export const claudeAccountsList = createDerived(() => _state.claudeAccounts);
export const modelProvidersList = createDerived(() => _state.providers);
