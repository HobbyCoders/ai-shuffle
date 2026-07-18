/**
 * API client for AI Hub backend
 *
 * This is the core API module. Domain-specific types and functions have been
 * split into separate files (sessions, tags, analytics, etc.) and are
 * re-exported here for backward compatibility.
 */

// The usage-snapshot wire shapes live in the rate-limit store (their canonical
// consumer). Re-export them here under the API-layer names other modules use so
// there's a single source of truth. Types-only import — no runtime cycle.
import type {
	SnapshotWindow,
	SnapshotInstance,
	UsageSnapshot,
} from '$lib/stores/rate-limit';

const API_BASE = '/api/v1';

export interface ApiError {
	detail: string;
	status: number;
}

/**
 * Auth-related endpoint path prefixes (relative to API_BASE). A 401 from any of
 * these is EXPECTED during the login / setup / status-check flow (bad password,
 * not-yet-authenticated status poll, etc.) — redirecting on those would create a
 * redirect loop off the very page that needs to render the 401. Everything else
 * that 401s means the session expired mid-use → send the user to /login.
 */
const AUTH_PATH_PREFIXES = ['/auth/', '/workspace/'];

function isAuthPath(path: string): boolean {
	// `path` is the API-relative path (e.g. '/auth/status', '/sessions'). Strip a
	// leading query string before matching so '/auth/status?x=1' still counts.
	const clean = path.split('?')[0];
	return AUTH_PATH_PREFIXES.some((prefix) => clean.startsWith(prefix));
}

/**
 * Redirect to /login after a session-expiry 401 on a non-auth endpoint.
 *
 * Uses a dynamic `$app/navigation` import (mirrors the WS 4001 handler in
 * active-session.ts) so this module stays importable from plain unit tests that
 * don't run inside SvelteKit. Guards against redirect loops: never fires when
 * already on /login or /setup.
 */
export function redirectToLoginOn401(): void {
	if (typeof window === 'undefined') return;
	const path = window.location?.pathname ?? '';
	if (path === '/login' || path === '/setup') return;
	import('$app/navigation')
		.then(({ goto }) => goto('/login'))
		.catch(() => {
			// Fall back to a hard navigation if the dynamic import is unavailable.
			window.location.assign('/login');
		});
}

/**
 * Extract a meaningful error message from a failed HTTP response.
 * Tries JSON first, then plain text, then falls back to status description.
 */
async function extractErrorDetail(response: Response): Promise<string> {
	try {
		const json = await response.json();
		// Slice C: SDK 0.1.81+ structured api_error body — prefer its concrete
		// fields over generic 429 / 5xx canned strings when present.
		if (json && typeof json === 'object') {
			if (json.type === 'api_error') {
				const status = typeof json.status === 'number' ? json.status : response.status;
				const retry = typeof json.retry_after_seconds === 'number' ? json.retry_after_seconds : null;
				const explicit = typeof json.detail === 'string' ? json.detail
					: typeof json.message === 'string' ? json.message
					: null;
				if (explicit) return explicit;
				if (status === 429 && retry !== null) {
					return `Rate limited — retry in ${retry}s`;
				}
				if (status === 429) {
					return 'Rate limited — too many requests, try again shortly';
				}
				return `API error: HTTP ${status}`;
			}
			if (json.detail) return json.detail;
			if (json.message) return json.message;
			if (json.error) return typeof json.error === 'string' ? json.error : JSON.stringify(json.error);
			// Even when `type` is missing, an explicit `retry_after_seconds` on a
			// 429 response gives us a better message than the canned fallback.
			if (response.status === 429 && typeof json.retry_after_seconds === 'number') {
				return `Rate limited — retry in ${json.retry_after_seconds}s`;
			}
		}
	} catch {
		// JSON parsing failed — try plain text
		try {
			const text = await response.text();
			if (text && text.length < 500) return text.trim();
		} catch {
			// Text extraction also failed
		}
	}
	// Fall back to a descriptive status-based message
	const statusMessages: Record<number, string> = {
		400: 'Bad request — the server rejected the input',
		401: 'Authentication required — please log in again',
		403: 'Permission denied — you don\'t have access to this resource',
		404: 'Not found — the requested resource doesn\'t exist',
		408: 'Request timed out — the server took too long to respond',
		409: 'Conflict — the operation conflicts with the current state',
		422: 'Validation error — the input data is invalid',
		429: 'Rate limited — too many requests, try again shortly',
		500: 'Internal server error — something went wrong on the server',
		502: 'Bad gateway — the server is temporarily unavailable',
		503: 'Service unavailable — the server is overloaded or under maintenance',
		504: 'Gateway timeout — the upstream server didn\'t respond in time',
	};
	return statusMessages[response.status] || `Server error (HTTP ${response.status}: ${response.statusText || 'no details'})`;
}

export interface RequestOptions {
	/** AbortSignal for cancelling the request (e.g., timeout or component unmount) */
	signal?: AbortSignal;
	/**
	 * If ``true``, the browser is allowed to keep the request alive even if
	 * the page is backgrounded / the tab unloads. Used for fire-and-forget
	 * POSTs where the user might lock their phone or switch sessions mid-
	 * flight (e.g. auto-create worktree). Limited to ~64 KB payload.
	 */
	keepalive?: boolean;
}

export class ApiClient {
	private async request<T>(
		method: string,
		path: string,
		body?: unknown,
		options?: RequestOptions
	): Promise<T> {
		const response = await fetch(`${API_BASE}${path}`, {
			method,
			headers: {
				'Content-Type': 'application/json'
			},
			credentials: 'include',
			body: body ? JSON.stringify(body) : undefined,
			signal: options?.signal,
			keepalive: options?.keepalive
		});

		if (!response.ok) {
			const detail = await extractErrorDetail(response);
			// Global session-expiry handling: a 401 on any non-auth endpoint means
			// the cookie session lapsed mid-use — bounce to /login. Auth endpoints
			// (login/setup/status/workspace probes) are excluded so their expected
			// 401s don't loop the user off the page that must render them.
			if (response.status === 401 && !isAuthPath(path)) {
				redirectToLoginOn401();
			}
			throw {
				detail,
				status: response.status
			} as ApiError;
		}

		// Handle 204 No Content
		if (response.status === 204) {
			return undefined as T;
		}

		return response.json();
	}

	async get<T>(path: string, options?: RequestOptions): Promise<T> {
		return this.request<T>('GET', path, undefined, options);
	}

	async post<T>(path: string, body?: unknown, options?: RequestOptions): Promise<T> {
		return this.request<T>('POST', path, body, options);
	}

	async put<T>(path: string, body?: unknown, options?: RequestOptions): Promise<T> {
		return this.request<T>('PUT', path, body, options);
	}

	async patch<T>(path: string, body?: unknown, options?: RequestOptions): Promise<T> {
		return this.request<T>('PATCH', path, body, options);
	}

	async delete<T>(path: string): Promise<T | void> {
		return this.request<T>('DELETE', path);
	}

	async uploadFile(path: string, file: File, sessionId?: string, worktreeId?: string): Promise<FileUploadResponse> {
		const formData = new FormData();
		formData.append('file', file);
		if (sessionId) {
			formData.append('session_id', sessionId);
		}
		if (worktreeId) {
			formData.append('worktree_id', worktreeId);
		}

		const response = await fetch(`${API_BASE}${path}`, {
			method: 'POST',
			credentials: 'include',
			body: formData
		});

		if (!response.ok) {
			const detail = await extractErrorDetail(response);
			throw {
				detail,
				status: response.status
			} as ApiError;
		}

		return response.json();
	}
}

export const api = new ApiClient();

/**
 * Fetch a file from the API and trigger a browser download.
 * Centralizes the common pattern of: fetch → check ok → extract filename → blob → download link.
 */
export async function fetchAndDownload(path: string, fallbackFilename: string): Promise<void> {
	const response = await fetch(`${API_BASE}${path}`, {
		method: 'GET',
		credentials: 'include'
	});

	if (!response.ok) {
		const error = await response.json().catch(() => ({ detail: 'Export failed' }));
		throw {
			detail: error.detail || 'Export failed',
			status: response.status
		} as ApiError;
	}

	const contentDisposition = response.headers.get('Content-Disposition');
	let filename = fallbackFilename;
	if (contentDisposition) {
		const match = contentDisposition.match(/filename="?([^"]+)"?/);
		if (match) {
			filename = match[1];
		}
	}

	const blob = await response.blob();
	const url = URL.createObjectURL(blob);
	const a = document.createElement('a');
	a.href = url;
	a.download = filename;
	document.body.appendChild(a);
	a.click();
	document.body.removeChild(a);
	URL.revokeObjectURL(url);
}

// ============================================================================
// Core types (used by multiple modules)
// ============================================================================

// Auth types
export interface AuthStatus {
	authenticated: boolean;
	is_admin: boolean;
	setup_required: boolean;
	claude_authenticated: boolean;
	/** True only when the Claude OAuth token is present AND not expired.
	 *  May be undefined on older backends — treat undefined as "unknown". */
	claude_token_valid?: boolean;
	github_authenticated: boolean;
	onboarding_complete: boolean;
	username: string | null;
}

// Profile types
export interface ProfileConfig {
	model?: string;
	provider?: string | null;  // model_providers.id this model runs through; unset/null = native Anthropic
	allowed_tools?: string[];
	disallowed_tools?: string[];
	tool_selection_mode?: 'all' | 'none' | 'allow' | 'disallow';  // UI-only: persists the editor dropdown so it round-trips even when the tool list is empty
	tools?: string[];  // SDK 0.1.12+ - explicit tool list
	tools_preset?: string;  // SDK 0.1.12+ - "claude_code" preset
	permission_mode?: string;
	max_turns?: number;
	include_partial_messages?: boolean;
	enable_file_checkpointing?: boolean;  // SDK 0.1.15+ - enable file snapshots for rewind
	thinking_mode?: 'default' | 'disabled' | 'adaptive' | 'think' | 'think_harder' | 'ultra_think';  // Thinking config: default=SDK decides, disabled=off, adaptive=auto, think/think_harder/ultra_think=token budgets
	effort?: 'low' | 'medium' | 'high' | 'max';  // Effort level for reasoning depth (independent of thinking config)
	ai_tools?: Record<string, boolean>;  // AI tool toggles (image_generation, video_generation, etc.)
	computer_use?: { enabled?: boolean; target_display?: number | null; require_confirmation?: boolean };  // Host desktop control (native mode only)
	enabled_agents?: string[];  // Subagents enabled for this profile
	setting_sources?: string[];  // Where to load settings from (e.g., 'claude_code')
	cwd?: string;  // Working directory override
	add_dirs?: string[];  // Additional directories to include
	user?: string;  // User identifier override
	max_buffer_size?: number;  // Maximum buffer size for output
	env?: Record<string, string>;  // Environment variables to set
	app_config_tools?: boolean;  // Allow AI to configure hooks/automations/MCP/profiles via chat
	strict_mcp_config?: boolean;  // SDK 0.1.81+ — fail-fast on misconfigured MCP servers
	system_prompt?: {
		type: string;
		preset?: string;
		content?: string;
		append?: string;
		inject_env_details?: boolean;
	} | null;
}

export interface Profile {
	id: string;
	name: string;
	description: string | null;
	is_builtin: boolean;
	is_modified: boolean;
	session_count?: number;  // Number of conversations using this profile
	config: ProfileConfig;
	created_at: string;
	updated_at: string;
}

// Query types
export interface QueryRequest {
	prompt: string;
	profile?: string;
	project?: string;
	overrides?: {
		model?: string;
		system_prompt_append?: string;
	};
}

export interface ConversationRequest {
	prompt: string;
	session_id?: string;
	profile?: string;
	project?: string;
	overrides?: {
		model?: string;
		system_prompt_append?: string;
	};
}

export interface QueryResponse {
	response: string;
	session_id: string;
	metadata: {
		model?: string;
		duration_ms?: number;
		total_cost_usd?: number;
		num_turns?: number;
	};
}

// Health types
export interface HealthResponse {
	status: string;
	service: string;
	version: string;
	authenticated: boolean;
	setup_required: boolean;
	claude_authenticated: boolean;
}

// File upload types
export interface FileUploadResponse {
	filename: string;
	path: string;
	full_path: string;
	size: number;
}

// Permission types
export interface PermissionRequest {
	request_id: string;
	session_id?: string;
	tool_name: string;
	tool_input: Record<string, unknown>;
	queue_position?: number;
	queue_total?: number;
	created_at?: string;
}

export interface PermissionResponse {
	request_id: string;
	decision: 'allow' | 'deny';
	remember?: 'none' | 'session' | 'profile';
	pattern?: string;
}

export interface PermissionRule {
	id: string;
	profile_id: string | null;
	tool_name: string;
	tool_pattern: string | null;
	decision: 'allow' | 'deny';
	created_at: string;
}

export interface PermissionQueueUpdate {
	resolved_ids: string[];
	remaining_count: number;
}

// User Question types (for AskUserQuestion tool)
export interface UserQuestionOption {
	label: string;
	description: string;
}

export interface UserQuestion {
	question: string;
	header: string;
	options: UserQuestionOption[];
	multiSelect: boolean;
}

export interface UserQuestionRequest {
	request_id: string;
	session_id?: string;
	tool_use_id: string;
	questions: UserQuestion[];
	created_at?: string;
}

export interface UserQuestionResponse {
	request_id: string;
	tool_use_id: string;
	answers: Record<string, string | string[]>;
	remaining_count: number;
}

// Workspace configuration types
export interface WorkspaceConfig {
	workspace_path: string;
	is_configured: boolean;
	is_local_mode: boolean;
	default_path: string;
	exists: boolean;
}

export interface WorkspaceValidation {
	valid: boolean;
	error: string | null;
	path: string;
	exists: boolean;
	writable: boolean;
}

// ============================================================================
// Agent Export/Import API Types and Functions
// ============================================================================

export interface AgentExportData {
	name: string;
	description: string | null;
	model: string | null;
	permission_mode: string | null;
	thinking_mode: string | null;
	effort: string | null;
	system_prompt_type: string | null;
	system_prompt_preset: string | null;
	system_prompt_content: string | null;
	system_prompt_append: string | null;
	system_prompt_inject_env: boolean | null;
	allowed_tools: string[] | null;
	disallowed_tools: string[] | null;
	enabled_agents: string[] | null;
	ai_tools: Record<string, boolean> | null;
	max_turns: number | null;
	max_buffer_size: number | null;
	include_partial_messages: boolean | null;
	continue_conversation: boolean | null;
	fork_session: boolean | null;
	setting_sources: string[] | null;
	enable_file_checkpointing: boolean | null;
	app_config_tools: boolean | null;
	env: Record<string, string> | null;
}

export interface AgentExport {
	version: string;
	type: string;
	exported_at: string;
	agent: AgentExportData;
}

export interface AgentImportRequest extends AgentExport {
	new_id?: string;
	new_name?: string;
}

/**
 * Export an agent/profile as JSON and trigger download
 */
export async function exportAgent(profileId: string): Promise<void> {
	await fetchAndDownload(`/profiles/${profileId}/export`, `${profileId}-agent.json`);
}

/**
 * Get agent export data without downloading (for preview)
 */
export async function getAgentExportData(profileId: string): Promise<AgentExport> {
	return api.get<AgentExport>(`/profiles/${profileId}/export`);
}

/**
 * Import an agent from JSON data
 */
export async function importAgent(data: AgentImportRequest): Promise<Profile> {
	return api.post<Profile>('/profiles/import', data);
}

/** One rate-limit window in a usage snapshot. */
export type UsageLimitsWindow = SnapshotWindow;
/** One install's usage bucket within a snapshot (clustered deployment). */
export type UsageLimitsInstanceSnapshot = SnapshotInstance;
/** Latest Claude usage snapshot (top-level = LOCAL install; `snapshots` = per-install). */
export type UsageLimitsSnapshot = UsageSnapshot;

/**
 * Fetch the latest Claude usage-limits snapshot. Used on chat load to populate
 * the usage pill before the first `usage_snapshot` WS event arrives.
 */
export async function getUsageLimits(): Promise<UsageLimitsSnapshot> {
	return api.get<UsageLimitsSnapshot>('/usage/limits');
}

/**
 * Parse and validate agent export file
 */
export function parseAgentExportFile(content: string): AgentExport {
	const parsed = JSON.parse(content);

	// Basic validation
	if (typeof parsed !== 'object' || parsed === null) {
		throw new Error('Invalid JSON structure');
	}

	if (parsed.type !== 'ai-shuffle-agent') {
		throw new Error(`Invalid file type: ${parsed.type}. Expected 'ai-shuffle-agent'`);
	}

	if (!parsed.version) {
		throw new Error('Missing version field');
	}

	if (!parsed.agent || typeof parsed.agent !== 'object') {
		throw new Error('Missing or invalid agent data');
	}

	if (!parsed.agent.name) {
		throw new Error('Agent name is required');
	}

	return parsed as AgentExport;
}

// ============================================================================
// Subagent Autocomplete API Types and Functions
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
	source: 'profile' | 'plugin' | 'project' | 'user';
	source_name: string | null;
}

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

// ============================================================================
// Agent Teams API Types and Functions
// ============================================================================

export interface Teammate {
	team_name: string;
	teammate_name: string;
	teammate_session_id?: string;
	pid?: number;
	status: 'running' | 'completed' | 'error' | 'killed';
	usage?: { total_tokens?: number; tool_uses?: number; duration_ms?: number };
}

export interface Team {
	team_name: string;
	parent_session_id: string;
	teammates: Teammate[];
}

/**
 * List all agent teams (and their teammates) for a parent session.
 */
export async function listTeams(sessionId: string): Promise<Team[]> {
	const r = await fetch(`${API_BASE}/teams/${sessionId}`, { credentials: 'include' });
	if (!r.ok) throw new Error(`listTeams: ${r.status}`);
	return r.json();
}

/**
 * Kill a running teammate subprocess by its team/name pair.
 */
export async function killTeammate(
	sessionId: string,
	teamName: string,
	teammateName: string
): Promise<{ success: boolean; pid?: number }> {
	const r = await fetch(
		`${API_BASE}/teams/${sessionId}/${encodeURIComponent(teamName)}/members/${encodeURIComponent(teammateName)}/kill`,
		{ method: 'POST', credentials: 'include' }
	);
	if (!r.ok) throw new Error(`killTeammate: ${r.status}`);
	return r.json();
}

// ============================================================================
// Utility functions
// ============================================================================

/**
 * Type guard: checks if a caught value is an ApiError thrown by ApiClient.
 */
export function isApiError(e: unknown): e is ApiError {
	return e !== null && typeof e === 'object' && 'detail' in e && 'status' in e;
}

/**
 * Extract a human-readable message from an unknown caught value.
 * Handles ApiError (from ApiClient), standard Error, and arbitrary values.
 */
export function getErrorMessage(e: unknown, fallback = 'Unknown error'): string {
	if (isApiError(e)) return e.detail;
	if (e instanceof Error) return e.message;
	if (typeof e === 'string') return e;
	return fallback;
}

// ============================================================================
// Re-exports from domain-specific modules (backward compatibility)
// ============================================================================

export * from './sessions';
export * from './tags';
export * from './analytics';
export * from './templates';
export * from './notes';
export * from './hooks';
export * from './preferences';
export * from './rewind';
export * from './knowledge';
export * from './generation';
