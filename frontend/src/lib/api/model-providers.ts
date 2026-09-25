/**
 * Model-provider registry API (non-Anthropic gateways).
 *
 * Admin-gated CRUD over `/api/v1/model-providers` — each row is an OpenRouter
 * or custom Anthropic-compatible gateway whose env gets injected into spawned
 * sessions. Mirrors `app/api/model_providers.py`.
 *
 * The encrypted `auth_token` is NEVER returned — responses only carry
 * `auth_token_set` (bool) and `auth_token_masked`. The token is write-only:
 * send it to set/replace it, omit/blank it to leave the stored value untouched.
 *
 * These functions use the shared `api` client, which throws `{ detail, status }`
 * on non-2xx; callers surface `.detail`.
 */

import { api } from './client';

/** Normalized provider evidence; null levels mean unknown, [] means unsupported. */
export interface ReasoningCapabilities {
	version: 1;
	effort_levels: string[] | null;
	default_effort: string | null;
	mandatory?: boolean;
}

/** One curated model offered by a gateway. */
export interface ProviderModelEntry {
	id: string;
	label: string;
	/** OpenRouter per-token prices retained for server-side spend projection. */
	pricing?: Partial<
		Record<'prompt' | 'completion' | 'input_cache_read' | 'input_cache_write', string | number>
	>;
	/**
	 * Per-model context window (tokens), when known: carried through from the
	 * upstream catalog (`context_length` / `top_provider.context_length`) or a
	 * previous curation. Absent means "no per-model value" — the backend then
	 * falls back to the preset roster and finally the row's own column (see
	 * `model_registry.resolve_provider_context_window`). Editors MUST preserve
	 * it when rebuilding entries; dropping it re-flattens every model onto the
	 * row's single shared window.
	 */
	context_window?: number | null;
	reasoning_capabilities?: ReasoningCapabilities;
	supported_parameters?: string[];
	supported_reasoning_levels?: string[];
	default_reasoning_level?: string | null;
}

/** Copy canonical evidence and legacy raw metadata. Computed registry fields are never saved.
 * Undefined means absent; null defaults and empty arrays are explicit metadata.
 */
export function copyReasoningMetadata(model?: Partial<ProviderModelEntry>) {
	return {
		...(model?.reasoning_capabilities !== undefined ? { reasoning_capabilities: model.reasoning_capabilities } : {}),
		...(model?.supported_parameters !== undefined ? { supported_parameters: model.supported_parameters } : {}),
		...(model?.supported_reasoning_levels !== undefined ? { supported_reasoning_levels: model.supported_reasoning_levels } : {}),
		...(model?.default_reasoning_level !== undefined ? { default_reasoning_level: model.default_reasoning_level } : {})
	};
}

/** Prefer fresh evidence without downgrading a normalized saved catalog. */
export function mergeReasoningMetadata(stored?: Partial<ProviderModelEntry>, fresh?: Partial<ProviderModelEntry>) {
	const merged = { ...copyReasoningMetadata(stored), ...copyReasoningMetadata(fresh) };
	const saved = stored?.reasoning_capabilities;
	const incoming = fresh?.reasoning_capabilities;
	// Unknown evidence cannot erase known support (including known unsupported []).
	if (saved && (saved.version > (incoming?.version ?? 0) ||
		(saved.version === incoming?.version && Array.isArray(saved.effort_levels) && incoming.effort_levels == null))) {
		merged.reasoning_capabilities = saved;
	}
	return merged;
}

/** Public (token-free) representation of a `model_providers` row. */
export interface ModelProvider {
	id: string;
	name: string;
	provider_type: string;
	base_url: string;
	/**
	 * Wire format a CUSTOM row's endpoint speaks: 'anthropic' (default when
	 * null — Anthropic Messages at {base_url}/v1/messages) or 'openai' (OpenAI
	 * chat/completions, e.g. xAI-direct / Groq / vLLM). Ignored on non-custom
	 * rows, whose provider_type already pins the dialect.
	 */
	api_dialect?: 'anthropic' | 'openai' | null;
	auth_token_set: boolean;
	auth_token_masked: string;
	models: ProviderModelEntry[];
	default_model: string | null;
	small_fast_model: string | null;
	extra_env: Record<string, string>;
	context_window: number | null;
	max_output_tokens: number | null;
	enabled: boolean;
	// ----- Codex / ChatGPT-subscription OAuth surface -----
	// The secret access/refresh blobs are NEVER returned — only the boolean
	// `codex_*_token_set` flags. The non-secret Codex fields (`auth_mode`,
	// `codex_account_id`, `needs_reauth`) come through directly. See
	// `app/api/model_providers.py::ModelProviderResponse` — `auth_mode` and
	// `codex_account_id` are `Optional[str] = None`; the three booleans default
	// to `false` in the response model, so they are always present on the wire.
	/** Non-secret credit tracking binding; managed only by the billing endpoints. */
	openai_billing_org_id?: string | null;
	openai_billing_instance_id?: string | null;
	auth_mode?: string | null;
	codex_account_id?: string | null;
	needs_reauth?: boolean;
	codex_access_token_set?: boolean;
	codex_refresh_token_set?: boolean;
	created_at: string | null;
	updated_at: string | null;
}

export interface ModelProvidersResponse {
	providers: ModelProvider[];
}

/** Request body to create a provider. */
export interface ModelProviderCreate {
	name: string;
	provider_type: string;
	base_url: string;
	/** Custom rows only: endpoint wire format ('anthropic' default | 'openai'). */
	api_dialect?: 'anthropic' | 'openai' | null;
	/** Omit or leave blank to store no token. */
	auth_token?: string | null;
	models?: ProviderModelEntry[];
	default_model?: string | null;
	small_fast_model?: string | null;
	extra_env?: Record<string, string>;
	context_window?: number | null;
	max_output_tokens?: number | null;
	enabled?: boolean;
}

/** Partial update — every field optional. Blank/omitted `auth_token` = unchanged. */
export type ModelProviderUpdate = Partial<ModelProviderCreate>;

export async function listModelProviders(): Promise<ModelProvidersResponse> {
	return api.get('/model-providers');
}

export async function createModelProvider(body: ModelProviderCreate): Promise<ModelProvider> {
	return api.post('/model-providers', body);
}

export async function updateModelProvider(
	id: string,
	body: ModelProviderUpdate
): Promise<ModelProvider> {
	return api.patch(`/model-providers/${id}`, body);
}

export async function deleteModelProvider(id: string): Promise<{ success: boolean } | void> {
	return api.delete<{ success: boolean }>(`/model-providers/${id}`);
}

/** Flip a provider's `enabled` flag; returns the updated row. */
export async function toggleModelProvider(id: string): Promise<ModelProvider> {
	return api.post(`/model-providers/${id}/toggle`);
}

/**
 * Result of a catalog fetch for a provider (phase-2 Chunk D).
 *
 * `source` is `"upstream"` for a live catalog pull (Codex `/codex/models` or
 * the provider's OpenAI-compatible `/models`). `"preset"` remains in the union
 * for older payloads. The caller
 * check-selects from `models` and persists the curated subset via
 * `updateModelProvider(id, { models })` — this call itself persists nothing.
 */
export interface FetchModelsResult {
	source: 'preset' | 'upstream';
	models: ProviderModelEntry[];
	/** Media endpoints the server held back: this picker is for chat models, and
	 *  audio/voice/image/video ones are configured elsewhere. Reported so the UI
	 *  can name their destination instead of looking like a failed fetch.
	 *  Absent on older payloads. */
	hidden?: (ProviderModelEntry & { media_kind?: string })[];
}

export async function fetchProviderModels(id: string): Promise<FetchModelsResult> {
	return api.post(`/model-providers/${id}/fetch-models`);
}

// ============================================================================
// Codex / ChatGPT-subscription device-code OAuth enrollment
// ============================================================================
//
// Two-step, admin-driven device-code flow backed by the routes in
// `app/api/model_providers.py` (`codex_device_start` / `codex_device_poll` /
// `codex_clear_reauth`). The interfaces below are derived from the ACTUAL
// backend response models (ruling PR7 — NOT the guessed shapes in plan §2.5),
// verified against `app/api/model_providers.py` at implementation time:
//
//   • start → `CodexDeviceStartResponse` = { user_code, verification_uri }.
//     Statuses: 200 on success; 400 (encryption locked), 404 (provider not
//     found), 502 (upstream Codex device-code start failed).
//   • poll  → `CodexDevicePollResponse` = { status, provider? }. `status` is
//     one of "pending" | "slow_down" | "complete" (a 200 in every one of those
//     cases); on `complete`, `provider` carries the fresh public row. There is
//     NO "error" status value — a terminal failure surfaces as an HTTP 400
//     (`detail: "Codex enrollment failed: <code>"`), and a poll with no pending
//     enrollment is also a 400 (`detail: "No pending Codex enrollment …"`).
//     Both are thrown as `ApiError` by the shared client — callers catch and,
//     per PR7, restart at `codexDeviceStart` rather than re-polling.
//   • clear-reauth → the fresh public `ModelProvider` row (200), 404 if the
//     provider has no stored Codex tokens.

/** Response of `POST /model-providers/{id}/codex/device/start`. */
export interface CodexDeviceStart {
	/** The code the admin types at `verification_uri` in their own browser. */
	user_code: string;
	/** The URL the admin opens to enter `user_code`. */
	verification_uri: string;
}

/** Non-terminal poll statuses. Terminal failure is an HTTP 400, not a status. */
export type CodexDevicePollStatus = 'pending' | 'slow_down' | 'complete';

/** Response of `POST /model-providers/{id}/codex/device/poll`. */
export interface CodexDevicePoll {
	status: CodexDevicePollStatus;
	/** Present only when `status === 'complete'` — the fresh, enrolled row. */
	provider?: ModelProvider;
}

/** Start Codex device-code enrollment for a provider. Throws on 400/404/502. */
export async function codexDeviceStart(id: string): Promise<CodexDeviceStart> {
	return api.post(`/model-providers/${id}/codex/device/start`);
}

/**
 * One poll step of the Codex enrollment. Returns `pending`/`slow_down` to keep
 * polling or `complete` with the enrolled `provider`. A terminal upstream error
 * (or an absent pending enrollment) is thrown as an `ApiError` (HTTP 400).
 */
export async function codexDevicePoll(id: string): Promise<CodexDevicePoll> {
	return api.post(`/model-providers/${id}/codex/device/poll`);
}

// ============================================================================
// xAI / SuperGrok device-code OAuth enrollment
// ============================================================================
//
// Same wire shape as the Codex helpers above (`CodexDeviceStart` /
// `CodexDevicePoll`). A separate in-memory pending map on the server keeps
// Grok and Codex enrollments from colliding.

/** Start SuperGrok device-code enrollment for a provider. Throws on 400/404/502. */
export async function xaiDeviceStart(id: string): Promise<CodexDeviceStart> {
	return api.post(`/model-providers/${id}/xai/device/start`);
}

/**
 * One poll step of the Grok enrollment. Returns `pending`/`slow_down` to keep
 * polling or `complete` with the enrolled `provider`. A terminal upstream error
 * (or an absent pending enrollment) is thrown as an `ApiError` (HTTP 400).
 */
export async function xaiDevicePoll(id: string): Promise<CodexDevicePoll> {
	return api.post(`/model-providers/${id}/xai/device/poll`);
}

// ============================================================================
// Codex / ChatGPT-subscription banked rate-limit resets
// ============================================================================

export type CodexRateLimitResetType = 'codex_rate_limits' | 'grok_rate_limits' | 'unknown';
export type CodexRateLimitResetStatus = 'available' | 'redeeming' | 'redeemed' | 'unknown';

export interface CodexRateLimitResetCredit {
	id: string;
	reset_type: CodexRateLimitResetType;
	status: CodexRateLimitResetStatus;
	/** Unix seconds. */
	granted_at: number;
	/** Unix seconds, or null when the upstream response exposes no expiration. */
	expires_at: number | null;
	title: string | null;
	description: string | null;
}

export interface CodexRateLimitResetCreditsResponse {
	available_count: number;
	credits: CodexRateLimitResetCredit[];
}

export interface ConsumeCodexRateLimitResetCreditRequest {
	credit_id: string;
	idempotency_key: string;
}

export interface ConsumeCodexRateLimitResetCreditResponse {
	outcome: 'reset' | 'nothing_to_reset' | 'no_credit' | 'already_redeemed';
	/** Number of eligible Codex windows reset; normalized by the backend. */
	windows_reset: number;
}

export async function listCodexRateLimitResetCredits(
	providerId: string
): Promise<CodexRateLimitResetCreditsResponse> {
	return api.get(`/model-providers/${providerId}/codex/rate-limit-reset-credits`);
}

export async function consumeCodexRateLimitResetCredit(
	providerId: string,
	body: ConsumeCodexRateLimitResetCreditRequest
): Promise<ConsumeCodexRateLimitResetCreditResponse> {
	return api.post(
		`/model-providers/${providerId}/codex/rate-limit-reset-credits/consume`,
		body
	);
}

// ============================================================================
// Grok / SuperGrok-subscription banked usage-limit resets
// Same public contract as Codex; only the routes and reset_type differ.
// ============================================================================

export type GrokRateLimitResetCreditsResponse = CodexRateLimitResetCreditsResponse;
export type ConsumeGrokRateLimitResetCreditRequest = ConsumeCodexRateLimitResetCreditRequest;
export type ConsumeGrokRateLimitResetCreditResponse = ConsumeCodexRateLimitResetCreditResponse;

export async function listGrokRateLimitResetCredits(
	providerId: string
): Promise<GrokRateLimitResetCreditsResponse> {
	return api.get(`/model-providers/${providerId}/grok/rate-limit-reset-credits`);
}

export async function consumeGrokRateLimitResetCredit(
	providerId: string,
	body: ConsumeGrokRateLimitResetCreditRequest
): Promise<ConsumeGrokRateLimitResetCreditResponse> {
	return api.post(
		`/model-providers/${providerId}/grok/rate-limit-reset-credits/consume`,
		body
	);
}
