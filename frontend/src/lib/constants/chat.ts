/**
 * Chat-related constants
 */

import { getClaudeContextWindow, getProviderContextWindow } from '$lib/constants/models.svelte';

/**
 * Extended context window size in tokens (1M models).
 */
export const CONTEXT_MAX_1M = 1_000_000;

/**
 * Determine the context window size for a given model identifier, or null
 * when nothing declares one.
 *
 * Resolution order:
 *   1. Registry, provider side — the `context_window` resolved for this model
 *      id (optionally scoped to `providerId`): the curated model entry's own
 *      value from the provider catalog, already per-model on the backend.
 *      Checked FIRST so a provider session always uses its own window.
 *   2. Registry, Claude side — the native window of a Claude preset slug
 *      (haiku/sonnet/opus/fable = 1M).
 *   3. null — the window is unknown. It is NEVER guessed: an OpenRouter or
 *      Grok model with no advertised context length would otherwise show a
 *      percentage of a made-up 200k window (#2274). The meter then shows the
 *      tokens used without a percentage.
 *
 * The session's server-resolved bound (the context-usage report's
 * `maxTokens`) outranks all of these; `resolveContextWindow` applies that.
 *
 * There is deliberately NO "the id contains 1m" heuristic any more: the
 * registry carries real windows on both sides, and the `[1m]` suffix that the
 * heuristic existed to catch is a retired mechanism.
 */
export function getContextMaxForModel(
	model: string | undefined | null,
	providerId?: string | null
): number | null {
	if (!model) return null;
	const fromProvider = getProviderContextWindow(model, providerId);
	if (fromProvider && fromProvider > 0) return fromProvider; // provider wins
	const fromClaude = getClaudeContextWindow(model);
	if (fromClaude && fromClaude > 0) return fromClaude;
	return null;
}
