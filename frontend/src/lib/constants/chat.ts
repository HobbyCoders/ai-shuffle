/**
 * Chat-related constants
 */

/**
 * Auto-compaction reserve in tokens.
 * Claude Code reserves approximately 33k tokens for auto-compaction overhead
 * (system prompt, tool definitions, output buffer, etc.).
 */
export const AUTO_COMPACTION_BASE = 33000;

/**
 * Maximum context window size in tokens (Claude 3.5 Sonnet)
 */
export const CONTEXT_MAX = 200000;
