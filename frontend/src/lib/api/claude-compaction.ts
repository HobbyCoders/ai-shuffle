/**
 * The owner's Claude compaction setting: Claude sessions on the Claude Code
 * CLI compact through AI Shuffle instead of the CLI's own summary. On by
 * default; read when a Claude session starts.
 */

import { api } from './client';

export interface ClaudeCompaction {
	enabled: boolean;
}

export function getClaudeCompaction(): Promise<ClaudeCompaction> {
	return api.get('/settings/claude-compaction', { cache: 'no-store' });
}

export function updateClaudeCompaction(enabled: boolean): Promise<ClaudeCompaction> {
	return api.put('/settings/claude-compaction', { enabled });
}
