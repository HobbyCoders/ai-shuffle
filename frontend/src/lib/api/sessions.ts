/**
 * Session API types and functions
 */

import { api, fetchAndDownload, type RequestOptions } from './client';
// Type-only: erased at build, so this cannot pull the store's runtime (and its
// localStorage access) into the API layer.
import type { SessionMode } from '$lib/stores/sessionMode.svelte';

// ============================================================================
// Session Types
// ============================================================================

export interface SessionTag {
	id: string;
	name: string;
	color: string;
}

export interface Session {
	workspace_project_ids?: string[];
	id: string;
	profile_id: string;
	project_id: string | null;
	title: string | null;
	status: string;
	/** Durable background-work marker, including sessions not open on this device. */
	parked_task_kind?: string | null;
	/** Running durable tasks, including non-park-gating Monitor commands. */
	has_running_tasks?: boolean;
	is_active: boolean;
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
	// V2 branch-intake (Phase 3): a NEW branch session records only its logical
	// branch here (no legacy worktree row). The branch badge falls back to this
	// when worktree_branch is absent.
	branch?: string | null;
	/** Persisted pull-request lifecycle. Null = unknown. */
	pr_number?: number | null;
	pr_url?: string | null;
	pr_state?: 'open' | 'merged' | 'closed' | null;
	created_at: string;
	updated_at: string;
	// Cross-install (cluster) annotation — populated server-side when sessions
	// are shared across installs on a shared database.
	instance_id?: string | null;  // Owning install (null = legacy/this install)
	device_name?: string | null;  // Human-friendly owning-device name
	device_online?: boolean;  // Owning install seen recently (heartbeat)
	is_remote?: boolean;  // Owned by another install (not this one)
	controllable?: boolean;  // Live send/interrupt reachable right now
	// V2 Q7: transient, derived server-side (never persisted). True when the
	// session has a queued turn but no capable runner is online — it's waiting,
	// read-only, until an owning device returns.
	degraded?: boolean;
	project_name?: string | null;  // Resolved server-side (cross-install projects)
	link_group_id?: string | null;  // Folds same-repo clones into one board group
	// V2 projects (Phase 3): origin-keyed project identity. `origin` is the board
	// grouping key (1:1 with link_group_id for grouped projects → identical fold).
	project_v2_id?: string | null;  // Origin-keyed project (install-independent)
	origin?: string | null;  // Grouping token (git:… / manual:… / local:<id>)
	is_pinned?: boolean;  // Explicit runner:<id> placement pin (computed at read)
	automation_id?: string | null;  // Set when an automation run created this session
	provider_id?: string | null;  // Bound model provider (null = native Claude)
	// The run binding chosen when this session was created (agents no longer
	// pin one). `model` is the raw pick: a bare id/alias, or a
	// `prov:<providerId>:<modelId>` composite. Null on sessions created before
	// the binding moved onto the row.
	model?: string | null;
	effort?: string | null;
	permission_mode?: string | null;
	/** Fast service tier for this session's run binding. Null on sessions
	 *  created before the flag existed, and on models that never supported it. */
	fast?: boolean | null;
	/** Optional persisted concrete context ceiling once the backend stores the override. */
	context_window?: number | null;
	/** Which sidepanel lane this session belongs to: 'code' (the full agentic
	 *  workspace), 'chat' (web-only assistant) or 'voice' (Realtime call).
	 *  Stored as NULL for code sessions — every row predating modes is one — so
	 *  the server coalesces NULL to 'code' on read. Widened with `string`
	 *  because an older install can still put an unrecognized value on the wire;
	 *  run it through `normalizeSessionMode` rather than comparing directly. */
	mode?: SessionMode | string;
	v2_stream?: boolean;  // Per-session opt-in to the V2 streaming path (V2 Phase 1)
	hidden?: boolean;  // List-hidden internal sessions (for example delegated workers)
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
 * Set whether a session is in the Active group. Passing `false` marks it complete
 * (it drops into Recent); passing `true` moves it back to Active. The backend
 * `PATCH /sessions/{id}/active` endpoint takes the new value as a JSON body and
 * returns the updated Session.
 */
export async function setSessionActive(sessionId: string, isActive: boolean): Promise<Session> {
	return api.patch<Session>(`/sessions/${sessionId}/active`, { is_active: isActive });
}

/**
 * Rename a session. The backend `PATCH /sessions/{id}` endpoint takes the new
 * title as a query parameter (not a JSON body) and returns the updated Session.
 */
export async function renameSession(sessionId: string, title: string): Promise<Session> {
	const params = new URLSearchParams({ title });
	return api.patch<Session>(`/sessions/${sessionId}?${params.toString()}`);
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

export interface AdoptSessionResponse {
	session_id: string;
	instance_id: string;
	adopted: boolean;
	status: 'adopted' | 'already_local' | 'unpinned' | (string & {});
	warning?: string | null;
	local_project_id?: string | null;
}

export interface MoveSessionResponse {
	session_id: string;
	instance_id: string;
	moved: boolean;
	status: string;
	pending_live_run: boolean;
}

/** Pin this session to the viewing install (Resume here). */
export async function adoptSession(sessionId: string): Promise<AdoptSessionResponse> {
	return api.post<AdoptSessionResponse>(`/sessions/${sessionId}/adopt`, {});
}

/** Take ownership here and drop the sticky pin (follow-me). */
export async function unpinSession(sessionId: string): Promise<AdoptSessionResponse> {
	return api.post<AdoptSessionResponse>(`/sessions/${sessionId}/adopt`, { follow: true });
}

/** Re-pin this session to another runner. */
export async function moveSession(
	sessionId: string,
	targetInstanceId: string
): Promise<MoveSessionResponse> {
	return api.post<MoveSessionResponse>(`/sessions/${sessionId}/move`, {
		target_instance_id: targetInstanceId
	});
}

// ============================================================================
// Context usage (live SDK breakdown)
// ============================================================================

/**
 * A single context-window category from the Claude Agent SDK's
 * ContextUsageResponse (e.g. "System prompt", "MCP tools", "Messages",
 * "Free space"). Only these fields are relied upon — the SDK may include more.
 */
export interface ContextUsageCategory {
	name: string;
	tokens: number;
	color: string;
	/** True for categories reserved but not yet materialised (e.g. autocompact buffer). */
	isDeferred?: boolean;
}

/**
 * The SDK ContextUsageResponse passed through verbatim by the backend. Extra
 * fields may be present; only those declared here are consumed by the UI.
 */
export interface SdkContextUsage {
	categories: ContextUsageCategory[];
	totalTokens: number;
	/**
	 * The context window, or `null` when the backend cannot resolve one — a
	 * fluid NATIVE leg running a model with no registered/curated window
	 * reports the window as unknown rather than guessing at it. Consumers must
	 * treat null as "unknown" (see `mapSdkContextUsage`), never as a number.
	 */
	maxTokens: number | null;
	rawMaxTokens: number | null;
	/** 0–100, or `null` when `maxTokens` is unknown. */
	percentage: number | null;
	model: string;
	/**
	 * True when `totalTokens` is the prompt size the provider actually billed
	 * rather than the chars/4 estimate. Absent on breakdowns produced before
	 * the engine reported it.
	 */
	measured?: boolean;
	isAutoCompactEnabled: boolean;
}

/**
 * Response of `GET /sessions/{id}/context-usage`. The `usage` shape is identical
 * whether it came from the live SDK (session connected) or the final snapshot the
 * backend persisted at the session's last turn end (`source: "persisted"`, with
 * the ISO `captured_at`). When neither is available a machine reason is returned
 * and the caller falls back to the poller estimate.
 */
export type ContextUsageResponse =
	| { available: false; reason: 'not_connected' | 'unsupported' | 'timeout' }
	| { available: true; source: 'live'; usage: SdkContextUsage }
	| { available: true; source: 'persisted'; captured_at: string; usage: SdkContextUsage };

/**
 * Fetch the live per-category context-window breakdown for a session. Cheap and
 * meant to be called on demand (e.g. when opening the context-usage popover) —
 * context changes every turn, so nothing should be cached across calls. Pass a
 * `signal` to abort a stale in-flight request (popover closed / session change).
 */
export async function getContextUsage(
	sessionId: string,
	options?: RequestOptions
): Promise<ContextUsageResponse> {
	return api.get<ContextUsageResponse>(`/sessions/${sessionId}/context-usage`, options);
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

export interface ResumeSessionResponse {
	turn_id: string;
	session_id: string;
}

/**
 * Re-run the last turn from the session head without adding a message.
 * Backs the Retry button on an exhausted provider-retry card: the failed turn
 * left the head on a user node the model never answered, so the engine picks
 * the loop back up from exactly that node and the transcript gains only the
 * answer. Fluid sessions only — the backend refuses anything else with a 409.
 */
export async function resumeSession(sessionId: string): Promise<ResumeSessionResponse> {
	return api.post<ResumeSessionResponse>(`/sessions/${sessionId}/resume`);
}
