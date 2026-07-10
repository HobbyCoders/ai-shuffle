/**
 * Session API types and functions
 */

import { api, fetchAndDownload } from './client';

// ============================================================================
// Session Types
// ============================================================================

export interface SessionTag {
	id: string;
	name: string;
	color: string;
}

export interface Session {
	id: string;
	profile_id: string;
	project_id: string | null;
	title: string | null;
	status: string;
	is_favorite: boolean;
	api_user_id?: string | null;
	api_user_name?: string | null;
	total_cost_usd: number;
	total_tokens_in: number;
	total_tokens_out: number;
	cache_creation_tokens: number;  // Tokens used to create cache (counts toward context)
	cache_read_tokens: number;  // Tokens read from cache (doesn't count toward context)
	context_tokens: number;  // Current context window size (input + cache_creation + cache_read)
	turn_count: number;
	tags: SessionTag[];  // Tags attached to this session
	// Fork/branch fields
	parent_session_id: string | null;  // ID of parent session if this is a fork
	fork_point_message_index: number | null;  // Message index where fork occurred
	has_forks: boolean;  // True if this session has forked children
	// Worktree fields - for restoring worktree mode on session resume
	worktree_id?: string | null;
	worktree_branch?: string | null;
	worktree_path?: string | null;
	worktree_deleted?: boolean;
	created_at: string;
	updated_at: string;
	// Cross-install (cluster) annotation — populated server-side when sessions
	// are shared across installs on a shared database.
	instance_id?: string | null;  // Owning install (null = legacy/this install)
	device_name?: string | null;  // Human-friendly owning-device name
	device_online?: boolean;  // Owning install seen recently (heartbeat)
	is_remote?: boolean;  // Owned by another install (not this one)
	controllable?: boolean;  // Live send/interrupt reachable right now
	project_name?: string | null;  // Resolved server-side (cross-install projects)
	link_group_id?: string | null;  // Folds same-repo clones into one board group
	automation_id?: string | null;  // Set when an automation run created this session
}

export interface SessionMessage {
	id: number;
	role: string;
	content: string;
	tool_name?: string;
	tool_input?: Record<string, unknown>;
	metadata?: Record<string, unknown>;
	created_at: string;
}

export interface SessionWithMessages extends Session {
	messages: SessionMessage[];
}

export interface SessionSearchResult extends Session {
	match_type: 'title' | 'content';
	match_snippet: string | null;
	match_time: string | null;
	/**
	 * Number of matching messages in the session. Added by the store-FTS CTE on
	 * the paginated search endpoint; older/absent responses are treated as 1.
	 */
	match_count?: number;
}

/**
 * Paginated response with total count for infinite scroll
 */
export interface PaginatedSessions {
	items: Session[];
	total: number;
	offset: number;
	limit: number;
	has_more: boolean;
}

export interface CursorPaginatedSessions {
	items: Session[];
	next_cursor: string | null;
	next_cursor_id: string | null;
	has_more: boolean;
}

export interface PaginatedSessionSearchResults {
	items: SessionSearchResult[];
	total: number;
	offset: number;
	limit: number;
	has_more: boolean;
}

export interface ForkSessionResponse {
	session_id: string;
	title: string | null;
	parent_session_id: string;
	fork_point_message_index: number;
	message_count: number;
	status: string;
}

export interface AdoptSessionResponse {
	session_id: string;
	instance_id: string;
	/** True when ownership was re-pointed here; false when already local (no-op). */
	adopted: boolean;
	status: string;
	/** Non-blocking advisory from the cross-device rebind preflight (e.g. an unpushed branch). */
	warning?: string | null;
	/** Local clone the session was rebound onto, when a link-group sibling was used. */
	local_project_id?: string | null;
}

// ============================================================================
// Session API Functions
// ============================================================================

/**
 * Search sessions by title and message content
 */
export async function searchSessions(
	query: string,
	options: {
		projectId?: string;
		profileId?: string;
		adminOnly?: boolean;
		limit?: number;
	} = {}
): Promise<SessionSearchResult[]> {
	const params = new URLSearchParams({ q: query });
	if (options.projectId) params.set('project_id', options.projectId);
	if (options.profileId) params.set('profile_id', options.profileId);
	if (options.adminOnly) params.set('admin_only', 'true');
	if (options.limit) params.set('limit', options.limit.toString());

	return api.get<SessionSearchResult[]>(`/sessions/search/query?${params.toString()}`);
}

/**
 * Search sessions with pagination support
 */
export async function searchSessionsPaginated(
	query: string,
	options: {
		projectId?: string;
		profileId?: string;
		adminOnly?: boolean;
		limit?: number;
		offset?: number;
	} = {}
): Promise<PaginatedSessionSearchResults> {
	const params = new URLSearchParams({ q: query });
	if (options.projectId) params.set('project_id', options.projectId);
	if (options.profileId) params.set('profile_id', options.profileId);
	if (options.adminOnly) params.set('admin_only', 'true');
	if (options.limit) params.set('limit', options.limit.toString());
	if (options.offset) params.set('offset', options.offset.toString());

	return api.get<PaginatedSessionSearchResults>(`/sessions/search/paginated?${params.toString()}`);
}

/**
 * Toggle the favorite status of a session
 */
export async function toggleSessionFavorite(sessionId: string): Promise<Session> {
	return api.patch<Session>(`/sessions/${sessionId}/favorite`);
}

/**
 * Export a session in the specified format and trigger download
 */
export async function exportSession(
	sessionId: string,
	format: 'markdown' | 'json' | 'jsonl' = 'markdown'
): Promise<void> {
	const ext = format === 'json' ? 'json' : format === 'jsonl' ? 'jsonl' : 'md';
	const fallback = `session-export.${ext}`;
	await fetchAndDownload(`/sessions/${sessionId}/export?format=${format}`, fallback);
}

/**
 * Fork a session from a specific message index
 * @param sessionId The session to fork from
 * @param messageIndex The 0-based message index to fork after
 */
export async function forkSession(sessionId: string, messageIndex?: number): Promise<ForkSessionResponse> {
	// Omitting messageIndex forks the ENTIRE session (the sessions-board "Fork"
	// action). Passing an index forks up to and including that display message.
	return api.post<ForkSessionResponse>(`/sessions/${sessionId}/fork`, {
		message_index: messageIndex ?? null
	});
}

/**
 * Adopt a session created by another install so it can resume on this device
 * ("Resume here"). No-ops server-side when the session is already local. Rejects
 * with 409 while the owning device is still online (it may be streaming there).
 */
export async function adoptSession(sessionId: string): Promise<AdoptSessionResponse> {
	return api.post<AdoptSessionResponse>(`/sessions/${sessionId}/adopt`, {});
}

/** Result of stopping one in-flight background task. */
export interface StopTaskResult {
	session_id: string;
	task_id: string;
	ok: boolean;
	kind?: 'agent' | 'shell';
	detail?: string;
}

/**
 * Stop one in-flight background task (sub-agent or Monitor shell) by id.
 * Backs the "x" on the live background-tasks strip. A failed stop resolves
 * with `ok: false` rather than throwing (e.g. the task already finished).
 */
export async function stopSessionTask(sessionId: string, taskId: string): Promise<StopTaskResult> {
	return api.post<StopTaskResult>(
		`/sessions/${sessionId}/tasks/${encodeURIComponent(taskId)}/stop`
	);
}

