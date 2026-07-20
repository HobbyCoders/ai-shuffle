/**
 * Chat-related constants
 */

import { getProviderContextWindow } from '$lib/constants/models.svelte';

/**
 * Auto-compaction reserve in tokens.
 * Claude Code reserves approximately 33k tokens for auto-compaction overhead
 * (system prompt, tool definitions, output buffer, etc.).
 */
export const AUTO_COMPACTION_BASE = 33000;

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
 *   1. Registry — a provider-declared `context_window` for this model id
 *      (optionally scoped to `providerId`). Checked FIRST so a provider model
 *      id that merely contains "1m" cannot spoof a 1M window.
 *   2. Heuristic — models containing "1m" (case-insensitive) use the 1M window.
 *   3. Default — the standard 200k window.
 */
export function getContextMaxForModel(
	model: string | undefined | null,
	providerId?: string | null
): number {
	if (!model) return CONTEXT_MAX;
	const fromRegistry = getProviderContextWindow(model, providerId);
	if (fromRegistry && fromRegistry > 0) return fromRegistry; // provider wins
	// Match patterns like "opus[1m]", "sonnet-1m", "claude-sonnet-4-1m", etc.
	if (/1m/i.test(model)) return CONTEXT_MAX_1M;
	return CONTEXT_MAX;
}
