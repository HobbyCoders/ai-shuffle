/**
 * Advanced Search API types and functions
 */

import { api } from './client';

// ============================================================================
// Search Types
// ============================================================================

export interface SearchMatch {
	message_index: number;
	snippet: string;
	role: string;
	timestamp: string | null;
}

export interface AdvancedSearchResult {
	session_id: string;
	session_title: string | null;
	created_at: string;
	updated_at: string;
	profile_id: string | null;
	profile_name: string | null;
	project_id: string | null;
	matches: SearchMatch[];
	match_count: number;
}

export interface AdvancedSearchParams {
	q: string;
	start_date?: string;
	end_date?: string;
	profile_id?: string;
	project_id?: string;
	in_code_only?: boolean;
	regex?: boolean;
	limit?: number;
	offset?: number;
}

// ============================================================================
// Search API Functions
// ============================================================================

/**
 * Advanced search across all sessions with filters
 */
export async function advancedSearch(params: AdvancedSearchParams): Promise<AdvancedSearchResult[]> {
	const searchParams = new URLSearchParams({ q: params.q });
	if (params.start_date) searchParams.set('start_date', params.start_date);
	if (params.end_date) searchParams.set('end_date', params.end_date);
	if (params.profile_id) searchParams.set('profile_id', params.profile_id);
	if (params.project_id) searchParams.set('project_id', params.project_id);
	if (params.in_code_only) searchParams.set('in_code_only', 'true');
	if (params.regex) searchParams.set('regex', 'true');
	if (params.limit) searchParams.set('limit', params.limit.toString());
	if (params.offset) searchParams.set('offset', params.offset.toString());

	return api.get<AdvancedSearchResult[]>(`/search?${searchParams.toString()}`);
}
