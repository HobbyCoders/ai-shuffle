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
}

export const api = new ApiClient();

// Auth types
export interface AuthStatus {
	authenticated: boolean;
	setup_required: boolean;
	claude_authenticated: boolean;
	username: string | null;
}

// Profile types
export interface ProfileConfig {
	model?: string;
	allowed_tools?: string[];
	disallowed_tools?: string[];
	permission_mode?: string;
	max_turns?: number;
	system_prompt?: {
		type: string;
		preset?: string;
		append?: string;
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
	turn_count: number;
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
