/**
 * Chat-related constants
 */

/**
 * Auto-compaction reserve base in tokens.
 * Claude Code reserves approximately 13k tokens as a base for auto-compaction,
 * plus any additional CLAUDE_CODE_MAX_OUTPUT_TOKENS configured in the profile.
 */
export const AUTO_COMPACTION_BASE = 13000;

/**
 * Maximum context window size in tokens (Claude 3.5 Sonnet)
 */
export const CONTEXT_MAX = 200000;
