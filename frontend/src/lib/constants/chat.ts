/**
 * Chat-related constants
 */

import { getClaudeContextWindow, getProviderContextWindow } from '$lib/constants/models.svelte';

/**
 * Default context window size in tokens (standard Claude models).
 */
export const CONTEXT_MAX = 200_000;

/**
 * Extended context window size in tokens (1M models).
 */
export const CONTEXT_MAX_1M = 1_000_000;

/**
 * Determine the context window size for a given model identifier.
 *
 * Resolution order:
 *   1. Registry, provider side — the `context_window` resolved for this model
 *      id (optionally scoped to `providerId`), already per-model on the
 *      backend. Checked FIRST so a provider session always uses its own window.
 *   2. Registry, Claude side — the native window of a Claude preset slug
 *      (haiku/sonnet/opus/fable = 1M).
 *   3. Default — the standard 200k window for genuinely unknown models.
 *
 * There is deliberately NO "the id contains 1m" heuristic any more: the
 * registry carries real windows on both sides, and the `[1m]` suffix that the
 * heuristic existed to catch is a retired mechanism.
 */
export function getContextMaxForModel(
	model: string | undefined | null,
	providerId?: string | null
): number {
	if (!model) return CONTEXT_MAX;
	const fromProvider = getProviderContextWindow(model, providerId);
	if (fromProvider && fromProvider > 0) return fromProvider; // provider wins
	const fromClaude = getClaudeContextWindow(model);
	if (fromClaude && fromClaude > 0) return fromClaude;
	return CONTEXT_MAX;
}
