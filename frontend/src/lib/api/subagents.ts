/**
 * Subagent Autocomplete API types and functions
 */

import { api } from './client-core';

// ============================================================================
// Types
// ============================================================================

/**
 * Unified subagent item for autocomplete - aggregates from all sources
 */
export interface AutocompleteSubagent {
	id: string;
	name: string;
	description: string;
	prompt: string;
	tools: string[] | null;
	model: string | null;
	source: 'profile' | 'plugin' | 'project';
	source_name: string | null;
}

// ============================================================================
// Functions
// ============================================================================

/**
 * Get all available subagents for autocomplete from multiple sources:
 * - Profile-enabled subagents (from database)
 * - Plugin file-based agents (from enabled plugins)
 * - Project agents (if projectId provided)
 *
 * @param profileId - The profile ID to get enabled agents from
 * @param projectId - Optional project ID to include project agents
 * @param search - Optional search filter for name/description
 * @param limit - Maximum number of results (default 30)
 */
export async function getSubagentsForAutocomplete(
	profileId: string,
	projectId?: string,
	worktreeId?: string,
	search?: string,
	limit: number = 30
): Promise<AutocompleteSubagent[]> {
	const params = new URLSearchParams();
	if (projectId) params.set('project_id', projectId);
	if (worktreeId) params.set('worktree_id', worktreeId);
	if (search) params.set('search', search);
	params.set('limit', limit.toString());

	return api.get<AutocompleteSubagent[]>(`/profiles/${profileId}/subagents/all?${params.toString()}`);
}
