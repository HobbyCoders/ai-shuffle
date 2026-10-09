/**
 * API client for API User management (admin only)
 */

import { api } from './client';

// ============================================================================
// Types
// ============================================================================

export interface ApiUser {
	id: string;
	name: string;
	username: string | null;
	description: string | null;
	project_id: string | null;
	profile_ids: string[] | null;
	is_active: boolean;
	model: string | null;
	effort: string | null;
	created_at: string;
	updated_at: string;
	last_used_at: string | null;
	/**
	 * Set when this user's saved model (or an assigned agent's) runs on the
	 * owner's subscription sign-in: its runs are refused until it is changed.
	 */
	subscription_warning?: string | null;
	/** Runs a connector that knows the owner's own account: the owner's
	 * messages on it may use a subscription model, nobody else's. */
	owner_sender_connector?: boolean;
}

export interface ApiUserCreate {
	name: string;
	description?: string;
	project_id?: string;
	profile_ids?: string[];
	/** Preconfigured run selection every connector session for this user binds. */
	model?: string | null;
	effort?: string | null;
}

export interface ApiUserUpdate {
	name?: string;
	/** null clears it; omitted leaves it unchanged. */
	description?: string | null;
	/** null removes the project restriction; omitted leaves it unchanged. */
	project_id?: string | null;
	profile_ids?: string[];
	is_active?: boolean;
	model?: string | null;
	effort?: string | null;
}

export interface ApiUserCreateResponse extends ApiUser {
	api_key: string;
}

export interface ApiUserRotateKeyResponse {
	id: string;
	api_key: string;
}

// ============================================================================
// API Functions
// ============================================================================

export async function getApiUsers(): Promise<ApiUser[]> {
	return api.get<ApiUser[]>('/api-users');
}

export async function createApiUser(data: ApiUserCreate): Promise<ApiUserCreateResponse> {
	return api.post<ApiUserCreateResponse>('/api-users', data);
}

export async function updateApiUser(id: string, data: ApiUserUpdate): Promise<ApiUser> {
	return api.patch<ApiUser>(`/api-users/${id}`, data);
}

export async function deleteApiUser(id: string): Promise<void> {
	await api.delete(`/api-users/${id}`);
}

export async function rotateApiUserKey(id: string): Promise<ApiUserRotateKeyResponse> {
	return api.post<ApiUserRotateKeyResponse>(`/api-users/${id}/rotate-key`);
}
