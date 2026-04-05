/**
 * Chat-related constants
 */

/**
 * Auto-compaction reserve in tokens.
 * Claude Code reserves approximately 21k tokens for auto-compaction overhead
 * (system prompt, tool definitions, output buffer, etc.).
 */
export const AUTO_COMPACTION_BASE = 21000;

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
 * Models containing "1m" (case-insensitive) use the extended 1M window.
 */
export function getContextMaxForModel(model: string | undefined | null): number {
	if (!model) return CONTEXT_MAX;
	// Match patterns like "opus[1m]", "sonnet-1m", "claude-sonnet-4-1m", etc.
	if (/1m/i.test(model)) return CONTEXT_MAX_1M;
	return CONTEXT_MAX;
}
