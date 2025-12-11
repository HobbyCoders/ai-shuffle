/**
 * API client for AI Hub backend
 */

const API_BASE = '/api/v1';

export interface ApiError {
	detail: string;
	status: number;
}

export class ApiClient {
	private async request<T>(
		method: string,
		path: string,
		body?: unknown
	): Promise<T> {
		const response = await fetch(`${API_BASE}${path}`, {
			method,
			headers: {
				'Content-Type': 'application/json'
			},
			credentials: 'include',
			body: body ? JSON.stringify(body) : undefined
		});

		if (!response.ok) {
			const error = await response.json().catch(() => ({ detail: 'Unknown error' }));
			throw {
				detail: error.detail || 'Request failed',
				status: response.status
			} as ApiError;
		}

		return response.json();
	}

	async get<T>(path: string): Promise<T> {
		return this.request<T>('GET', path);
	}

	async post<T>(path: string, body?: unknown): Promise<T> {
		return this.request<T>('POST', path, body);
	}

	async put<T>(path: string, body?: unknown): Promise<T> {
		return this.request<T>('PUT', path, body);
	}

	async patch<T>(path: string, body?: unknown): Promise<T> {
		return this.request<T>('PATCH', path, body);
	}

	async delete<T>(path: string): Promise<T | void> {
		const response = await fetch(`${API_BASE}${path}`, {
			method: 'DELETE',
			headers: {
				'Content-Type': 'application/json'
			},
			credentials: 'include'
		});

		if (!response.ok) {
			const error = await response.json().catch(() => ({ detail: 'Unknown error' }));
			throw {
				detail: error.detail || 'Request failed',
				status: response.status
			} as ApiError;
		}

		// Handle 204 No Content
		if (response.status === 204) {
			return;
		}

		return response.json();
	}

	async uploadFile(path: string, file: File): Promise<FileUploadResponse> {
		const formData = new FormData();
		formData.append('file', file);

		const response = await fetch(`${API_BASE}${path}`, {
			method: 'POST',
			credentials: 'include',
			body: formData
		});

		if (!response.ok) {
			const error = await response.json().catch(() => ({ detail: 'Unknown error' }));
			throw {
				detail: error.detail || 'Upload failed',
				status: response.status
			} as ApiError;
		}

		return response.json();
	}
}

export const api = new ApiClient();

// Auth types
export interface ApiUserInfo {
	id: string;
	name: string;
	project_id: string | null;
	profile_id: string | null;
}

export interface AuthStatus {
	authenticated: boolean;
	is_admin: boolean;
	setup_required: boolean;
	claude_authenticated: boolean;
	github_authenticated: boolean;
	username: string | null;
	api_user: ApiUserInfo | null;
}

// Profile types
export interface ProfileConfig {
	model?: string;
	allowed_tools?: string[];
	disallowed_tools?: string[];
	permission_mode?: string;
	max_turns?: number;
	include_partial_messages?: boolean;
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
	config: ProfileConfig;
	created_at: string;
	updated_at: string;
}

// Session types
export interface Session {
	id: string;
	profile_id: string;
	project_id: string | null;
	title: string | null;
	status: string;
	total_cost_usd: number;
	total_tokens_in: number;
	total_tokens_out: number;
	cache_creation_tokens: number;  // Tokens used to create cache (counts toward context)
	cache_read_tokens: number;  // Tokens read from cache (doesn't count toward context)
	context_tokens: number;  // Current context window size (input + cache_creation + cache_read)
	turn_count: number;
	first_user_message: string | null;  // Preview of first user message (used as title)
	last_assistant_message: string | null;  // Preview of last assistant response
	created_at: string;
	updated_at: string;
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
}

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

// API User types
export interface ApiUser {
	id: string;
	name: string;
	description: string | null;
	project_id: string | null;
	profile_id: string | null;
	is_active: boolean;
	web_login_allowed: boolean;
	username: string | null;
	created_at: string;
	updated_at: string;
	last_used_at: string | null;
}

export interface ApiUserWithKey extends ApiUser {
	api_key: string;
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
