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
import type { UsageSnapshot, ProviderUsageEntry } from '$lib/stores/rate-limit';
import { apiBase, withAuth } from './runtime';


export interface ApiError {
	detail: string;
	status: number;
}

/**
 * Auth-related endpoint path prefixes (relative to API_BASE). A 401 from any of
 * these is EXPECTED during the setup / status-check flow (not-yet-authenticated
 * status poll, workspace probes, etc.) — re-checking auth on those would loop on
 * the very page that needs to render the 401. Other endpoints can also report
 * provider authentication failures, so confirm worker auth first.
 */
const AUTH_PATH_PREFIXES = ['/auth/', '/owner/', '/workspace/', '/pairing/exchange'];

function isAuthPath(path: string): boolean {
	// `path` is the API-relative path (e.g. '/auth/status', '/sessions'). Strip a
	// leading query string before matching so '/auth/status?x=1' still counts.
	const clean = path.split('?')[0];
	return AUTH_PATH_PREFIXES.some((prefix) => clean.startsWith(prefix));
}

let pendingAuthCheck: Promise<void> | null = null;

function isSetupFlow(): boolean {
	return ['/setup', '/pair'].includes(window.location?.pathname ?? '');
}

/**
 * A failed request/socket can belong to an old cookie or to a provider login.
 * Re-check the current worker session, sharing one check across concurrent
 * failures. There is no login page to send anyone to: the root layout reacts to
 * the refreshed auth store (the local-network notice, or the mobile app's own
 * reconnect message). The failed operation is still rejected, never replayed.
 */
export function recheckAuthOn401(): Promise<void> {
	if (typeof window === 'undefined' || isSetupFlow()) return Promise.resolve();
	if (pendingAuthCheck) return pendingAuthCheck;
	pendingAuthCheck = (async () => {
		try {
			// Reuse the store's account/logout epoch fencing. Import lazily because the
			// auth API itself uses this client.
			const { auth } = await import('$lib/stores/auth.svelte');
			await auth.checkAuth();
		} catch {
			// A network/status failure does not establish that the session was revoked.
		}
	})().finally(() => { pendingAuthCheck = null; });
	return pendingAuthCheck;
}

/**
 * Extract a meaningful error message from a failed HTTP response.
 * Tries JSON first, then plain text, then falls back to status description.
 */
async function extractErrorDetail(response: Response): Promise<string> {
	try {
		// Preserve the original body for the plain-text fallback below.
		const json = await response.clone().json();
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
			// FastAPI request validation (422) sends a list of {loc, msg}; the
			// detail must stay a string, so name each failing field.
			if (Array.isArray(json.detail)) {
				const messages = json.detail
					.filter((d: unknown) => d && typeof d === 'object' && typeof (d as { msg?: unknown }).msg === 'string')
					.map((d: { loc?: unknown; msg: string }) => {
						const field = Array.isArray(d.loc) ? d.loc[d.loc.length - 1] : null;
						return typeof field === 'string' ? `${field}: ${d.msg}` : d.msg;
					});
				if (messages.length) return messages.join('; ');
			} else if (json.detail) return json.detail;
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
	/** Fetch cache policy; auth status must observe the current cookie. */
	cache?: RequestCache;
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
		const response = await fetch(`${apiBase()}${path}`, withAuth({
			method,
			headers: {
				'Content-Type': 'application/json'
			},
			body: body ? JSON.stringify(body) : undefined,
			signal: options?.signal,
			keepalive: options?.keepalive,
			cache: options?.cache
		}));

		if (!response.ok) {
			const detail = await extractErrorDetail(response);
			// Confirm worker authentication separately from provider-specific or stale
			// failures. Auth endpoints render their own expected authentication errors.
			if (response.status === 401 && !isAuthPath(path)) {
				recheckAuthOn401();
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

	/**
	 * POST one file as multipart to `endpoint`.
	 *
	 * The three optional fields are the union of what the two upload endpoints
	 * take, and each is sent only when supplied. The project endpoint reads
	 * `session_id`/`worktree_id` to pick the right uploads directory inside the
	 * checkout; the projectless `/workspace/upload` reads `path` instead — the
	 * workspace-relative subdirectory to land in (the chat lane's
	 * `chat-uploads`). `destDir` rather than `path` on this side so it cannot be
	 * confused with `endpoint`, which is the URL.
	 */
	async uploadFile(
		endpoint: string,
		file: File,
		sessionId?: string,
		worktreeId?: string,
		destDir?: string
	): Promise<FileUploadResponse> {
		const formData = new FormData();
		formData.append('file', file);
		if (sessionId) {
			formData.append('session_id', sessionId);
		}
		if (worktreeId) {
			formData.append('worktree_id', worktreeId);
		}
		if (destDir) {
			formData.append('path', destDir);
		}

		const response = await fetch(`${apiBase()}${endpoint}`, withAuth({
			method: 'POST',
			body: formData
		}));

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
export async function fetchAndDownload(
	path: string,
	fallbackFilename: string,
	validateResponse?: (response: Response) => void
): Promise<void> {
	const response = await fetch(`${apiBase()}${path}`, withAuth({
		method: 'GET' }));

	if (!response.ok) {
		const error = await response.json().catch(() => ({ detail: 'Export failed' }));
		throw {
			detail: error.detail || 'Export failed',
			status: response.status
		} as ApiError;
	}

	// Allow callers to fail closed before reading or downloading the file.
	validateResponse?.(response);

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
	preview_access?: boolean;
	preference_identity?: { user_type: 'admin' | 'api_user'; user_id: string } | null;
	setup_required: boolean;
	auth_mode?: 'local' | 'account';
	local_access?: boolean;
	owner_linked?: boolean;
	credential_storage?: { ready: boolean; state: string };
	/** The Claude Code CLI reports a login (`claude auth status`). */
	claude_authenticated: boolean;
	/** Same as claude_authenticated; kept for older clients. */
	claude_token_valid?: boolean;
	/** The CLI probe. Account fields only for a caller signed in to the worker. */
	claude_cli?: Partial<import('./claude-cli').ClaudeCliStatus> & { installed: boolean; signed_in: boolean | null };
	/** True when ANY usable credential exists: the Claude subscription login OR
	 *  at least one enabled model provider with a stored credential (PA10).
	 *  May be undefined on older backends — fall back to claude_authenticated. */
	has_any_credential?: boolean;
	/** The server's one credential gate (license plus the active Claude Code
	 *  mode's credential). When `ready` is false, new Claude agent runs are
	 *  refused with `reason` (reason only for callers signed in to the worker). */
	agent_gate?: AgentGateStatus;
	github_authenticated: boolean;
	onboarding_complete: boolean;
	username: string | null;
}

export interface AgentGateStatus {
	ready: boolean;
	mode: 'subscription' | 'api_key' | 'off' | null;
	claude_credential_ready: boolean;
	reason?: string | null;
	license_allows_runs?: boolean;
	startup_state?: 'ready' | 'setup_required' | null;
	/** At least one agent provider (any brand) has a usable credential. Absent on older workers. */
	any_provider_ready?: boolean;
	/** The agent providers with a usable credential: `claude`, `openai`, `grok`, `openrouter`, `gw:<id>`. */
	ready_providers?: string[];
}

// Profile types — Fluid harness options. Legacy SDK keys remain tolerated on
// old rows; setting_sources selects the user/project/local settings scopes.
export interface ProfileConfig {
	setting_sources?: string[] | null;  // null/unset = all scopes; [] = none
	model?: string;
	provider?: string | null;  // model_providers.id this model runs through; unset/null = native Anthropic
	provider_models?: Record<string, string>;  // subagent model by caller provider; values are a Claude alias or prov:<id>:<model> (any leg). __native__ = native Claude callers; missing key = unavailable
	allowed_tools?: string[];
	disallowed_tools?: string[];
	tool_selection_mode?: 'all' | 'none' | 'allow' | 'disallow';  // UI-only: persists the editor dropdown so it round-trips even when the tool list is empty
	permission_mode?: string;
	effort?: 'none' | 'minimal' | 'low' | 'medium' | 'high' | 'xhigh' | 'max' | 'ultra';  // Reasoning depth — delegated CLI --effort / native provider effort param; chat can override per-turn
	max_turns?: number;  // Per-turn model-call cap (native legs → fluid max_model_calls_per_turn)
	include_partial_messages?: boolean;
	enable_file_checkpointing?: boolean;  // LEGACY (SDK-era): still read by chat rewind gating for old rows; no longer written
	ai_tools?: Record<string, boolean>;  // AI tool toggles (image_generation, video_generation, etc.)
	computer_use?: { enabled?: boolean; target_display?: number | null; require_confirmation?: boolean };  // Host desktop control (native mode only)
	enabled_agents?: string[];  // The agents this one may call under agent_invoke_mode 'custom'
	agent_invoke_mode?: 'all' | 'custom' | 'none' | null;  // Who it can call: 'all' = every agent, 'custom' = exactly enabled_agents (empty = no listed agent), 'none' = no subagents (Task/Swarm off), null/unset = legacy (non-empty list = those, empty = all)
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
		inject_env_details?: boolean;  // LEGACY: env is fluid-compiled now
	} | null;
}

export interface Profile {
	id: string;
	name: string;
	description: string | null;
	is_builtin: boolean;
	is_modified: boolean;
	session_count?: number;  // Number of conversations using this profile
	avatar_color?: string | null;  // Avatar colour name (avatar-colors.ts); absent on workers that predate it
	config: ProfileConfig;
	created_at: string;
	updated_at: string;
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
	/**
	 * Set when the assistant asks for this approval in its conversation instead
	 * of on a card (app/core/assistant_confirm.py). The owner answers yes or no
	 * in the message box; `chat_session_id` names the started chat that asked.
	 */
	assistant_confirm?: { question?: string | null; chat_session_id?: string | null } | null;
	/** Worker tag on a prompt raised for a subagent (app/core/provider_agent_approvals.py).
	 *  `worker_name` + `remote_approval_id` mark a prompt relayed from a subagent
	 *  on another worker (app/core/remote_task_approvals.py). */
	provider_agent?: PermissionWorkerTag | null;
	/** False on a prompt that cannot be deferred (Switch model, #1881): it is
	 *  answered yes or no only and the server refuses a defer for it. Absent
	 *  means the prompt may be deferred. */
	deferrable?: boolean;
}

export interface PermissionWorkerTag {
	worker_session_id?: string | null;
	label?: string | null;
	worker_name?: string | null;
	remote_approval_id?: string | null;
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
	/** Answer with this picker instead of options: 'model' answers a model id (#1827). */
	picker?: 'model';
}

export interface UserQuestionRequest {
	request_id: string;
	session_id?: string;
	tool_use_id: string;
	questions: UserQuestion[];
	/** AskUser posted without blocking the running turn. */
	nonblocking?: boolean;
	created_at?: string;
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

/** `code_sandbox` in GET /api/v1/deployment: whether agents' Python cells run
 * inside the bubblewrap sandbox (app/api/system.py code_sandbox_status). */
export interface CodeSandboxStatus {
	mode: 'auto' | 'required' | 'off';
	state: 'active' | 'checking' | 'unavailable' | 'off';
	active: boolean;
	/** Why the sandbox is not active: the self-test's own message, or the setting that turned it off. */
	reason: string;
}

// ============================================================================
// Agent Export/Import API Types and Functions
// ============================================================================

export interface AgentExportData {
	// Fluid-honored fields — the only ones the backend exports (ruling A12)
	name: string;
	description: string | null;
	model: string | null;
	provider_models?: Record<string, string> | null;
	permission_mode: string | null;
	effort?: 'none' | 'minimal' | 'low' | 'medium' | 'high' | 'xhigh' | 'max' | 'ultra' | null;  // harness-honored reasoning depth
	max_turns?: number | null;  // per-turn model-call cap (native legs)
	system_prompt_content: string | null;
	allowed_tools: string[] | null;
	disallowed_tools: string[] | null;
	enabled_agents: string[] | null;
	agent_invoke_mode: string | null;  // 'all' | 'custom' | 'none' | null — see ProfileConfig.agent_invoke_mode
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
 * Import an agent from JSON data
 */
export async function importAgent(data: AgentImportRequest): Promise<Profile> {
	return api.post<Profile>('/agents/import', data);
}

/** One non-Claude provider's usage entry within a snapshot (U4 wire shape:
 *  provider_id, provider_name, provider_type, plan_type, windows, credits,
 *  fetched_at). Surfaced on {@link UsageLimitsSnapshot} as `providers?`. */
export type { ProviderUsageEntry };
/** Latest usage snapshot. Top-level = LOCAL install; `snapshots` = per-install
 *  Claude; `providers?: ProviderUsageEntry[]` = non-Claude provider usage. */
export type UsageLimitsSnapshot = UsageSnapshot;

/**
 * Fetch the latest Claude usage-limits snapshot. Used on chat load to populate
 * the usage pill before the first `usage_snapshot` WS event arrives. Pass
 * `refresh=true` to force a live fetch (and provider-usage poll) on the server.
 */
export async function getUsageLimits(refresh = false): Promise<UsageLimitsSnapshot> {
	const path = refresh ? '/usage/limits?refresh=true' : '/usage/limits';
	return api.get<UsageLimitsSnapshot>(path);
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
export * from './notes';
export * from './hooks';
export * from './rewind';
export * from './generation';
