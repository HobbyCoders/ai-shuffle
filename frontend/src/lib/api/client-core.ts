/**
 * Core API client for AI Hub backend
 *
 * This module contains only the HTTP infrastructure:
 * - ApiError interface
 * - Error extraction helper
 * - ApiClient class with HTTP methods
 * - Singleton api instance
 * - FileUploadResponse type
 *
 * Domain-specific types and functions live in sibling modules.
 */

export const API_BASE = '/api/v1';

export interface ApiError {
	detail: string;
	status: number;
}

/**
 * Extract a meaningful error message from a failed HTTP response.
 * Tries JSON first, then plain text, then falls back to status description.
 */
async function extractErrorDetail(response: Response): Promise<string> {
	try {
		const json = await response.json();
		if (json.detail) return json.detail;
		if (json.message) return json.message;
		if (json.error) return typeof json.error === 'string' ? json.error : JSON.stringify(json.error);
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
			const detail = await extractErrorDetail(response);
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
			const detail = await extractErrorDetail(response);
			throw {
				detail,
				status: response.status
			} as ApiError;
		}

		// Handle 204 No Content
		if (response.status === 204) {
			return;
		}

		return response.json();
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

// File upload types
export interface FileUploadResponse {
	filename: string;
	path: string;
	full_path: string;
	size: number;
}
