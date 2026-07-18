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

/** One curated model offered by a gateway. */
export interface ProviderModelEntry {
	id: string;
	label: string;
}

/** Public (token-free) representation of a `model_providers` row. */
export interface ModelProvider {
	id: string;
	name: string;
	provider_type: string;
	base_url: string;
	auth_token_set: boolean;
	auth_token_masked: string;
	models: ProviderModelEntry[];
	default_model: string | null;
	small_fast_model: string | null;
	extra_env: Record<string, string>;
	context_window: number | null;
	max_output_tokens: number | null;
	enabled: boolean;
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
 * `source` is `"preset"` for a ChatGPT/Codex provider (built-in roster, no
 * network) or `"upstream"` for a live `GET {base_url}/models` fetch. The caller
 * check-selects from `models` and persists the curated subset via
 * `updateModelProvider(id, { models })` — this call itself persists nothing.
 */
export interface FetchModelsResult {
	source: 'preset' | 'upstream';
	models: ProviderModelEntry[];
}

export async function fetchProviderModels(id: string): Promise<FetchModelsResult> {
	return api.post(`/model-providers/${id}/fetch-models`);
}
