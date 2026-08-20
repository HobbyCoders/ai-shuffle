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
	ProviderUsageEntry,
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
	/** True when ANY usable credential exists: the Claude subscription login OR
	 *  at least one enabled model provider with a stored credential (PA10).
	 *  May be undefined on older backends — fall back to claude_authenticated. */
	has_any_credential?: boolean;
	github_authenticated: boolean;
	onboarding_complete: boolean;
	username: string | null;
}

// Profile types — omni harness options only. SDK-era keys (thinking_mode,
// setting_sources, max_buffer_size, user, strict_mcp_config, …) were dropped
// from the editor; the backend tolerates them on old rows but the omni
// harness ignores them. effort and max_turns ARE honored (adapter maps them
// onto --effort / provider effort params and the per-turn model-call cap).
export interface ProfileConfig {
	model?: string;
	provider?: string | null;  // model_providers.id this model runs through; unset/null = native Anthropic
	provider_models?: Record<string, string>;  // subagent model by provider; __native__ = native Claude, missing key = unavailable
	allowed_tools?: string[];
	disallowed_tools?: string[];
	tool_selection_mode?: 'all' | 'none' | 'allow' | 'disallow';  // UI-only: persists the editor dropdown so it round-trips even when the tool list is empty
	permission_mode?: string;
	effort?: 'low' | 'medium' | 'high' | 'xhigh' | 'max';  // Reasoning depth — delegated CLI --effort / native provider effort param; chat can override per-turn
	max_turns?: number;  // Per-turn model-call cap (native legs → omni max_model_calls_per_turn)
	include_partial_messages?: boolean;
	enable_file_checkpointing?: boolean;  // LEGACY (SDK-era): still read by chat rewind gating for old rows; no longer written
	ai_tools?: Record<string, boolean>;  // AI tool toggles (image_generation, video_generation, etc.)
	computer_use?: { enabled?: boolean; target_display?: number | null; require_confirmation?: boolean };  // Host desktop control (native mode only)
	enabled_agents?: string[];  // Optional invoke allowlist: agents this agent may dispatch via the Task tool; empty/unset = any
	memory?: string;  // Per-agent memory (plain text); agent-editable at runtime via the Memory tool, injected into the system prompt
	cwd?: string;  // Working directory override
	add_dirs?: string[];  // Additional directories the agent may access
	env?: Record<string, string>;  // Environment variables to set
	app_config_tools?: boolean;  // Allow AI to configure hooks/automations/MCP/profiles via chat
	system_prompt?: {
		type: string;  // always 'custom' when written by the editor; 'preset' only on legacy rows
		preset?: string;  // LEGACY
		content?: string;
		append?: string;  // LEGACY: preset-shape append text
		inject_env_details?: boolean;  // LEGACY: env is omni-compiled now
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
	// Omni-honored fields — the only ones the backend exports (ruling A12)
	name: string;
	description: string | null;
	model: string | null;
	provider_models?: Record<string, string> | null;
	permission_mode: string | null;
	effort?: 'low' | 'medium' | 'high' | 'xhigh' | 'max' | null;  // harness-honored reasoning depth
	max_turns?: number | null;  // per-turn model-call cap (native legs)
	system_prompt_content: string | null;
	allowed_tools: string[] | null;
	disallowed_tools: string[] | null;
	enabled_agents: string[] | null;
	ai_tools: Record<string, boolean> | null;
	memory?: string | null;
	include_partial_messages: boolean | null;
	app_config_tools: boolean | null;
	env: Record<string, string> | null;
	// LEGACY (pre-A12 export files, import-only): a preset prompt's append
	// text is mapped onto the custom prompt content; everything else is
	// ignored by the importer.
	thinking_mode?: string | null;
	system_prompt_type?: string | null;
	system_prompt_preset?: string | null;
	system_prompt_append?: string | null;
	system_prompt_inject_env?: boolean | null;
	max_buffer_size?: number | null;
	continue_conversation?: boolean | null;
	fork_session?: boolean | null;
	setting_sources?: string[] | null;
	enable_file_checkpointing?: boolean | null;
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
	await fetchAndDownload(`/agents/${profileId}/export`, `${profileId}-agent.json`);
}

/**
 * Get agent export data without downloading (for preview)
 */
export async function getAgentExportData(profileId: string): Promise<AgentExport> {
	return api.get<AgentExport>(`/agents/${profileId}/export`);
}

/**
 * Import an agent from JSON data
 */
export async function importAgent(data: AgentImportRequest): Promise<Profile> {
	return api.post<Profile>('/agents/import', data);
}

/** One rate-limit window in a usage snapshot. */
export type UsageLimitsWindow = SnapshotWindow;
/** One install's usage bucket within a snapshot (clustered deployment). */
export type UsageLimitsInstanceSnapshot = SnapshotInstance;
/** One non-Claude provider's usage entry within a snapshot (U4 wire shape:
 *  provider_id, provider_name, provider_type, plan_type, windows, credits,
 *  fetched_at). Surfaced on {@link UsageLimitsSnapshot} as `providers?`. */
export type { ProviderUsageEntry };
/** Latest usage snapshot. Top-level = LOCAL install; `snapshots` = per-install
 *  Claude; `providers?: ProviderUsageEntry[]` = non-Claude provider usage. */
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

	return api.get<AutocompleteSubagent[]>(`/agents/${profileId}/subagents/all?${params.toString()}`);
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
