/**
 * User Preferences API types and functions (for cross-device sync)
 */

import { api, type ApiError } from './client-core';

// ============================================================================
// Types
// ============================================================================

export interface PreferenceResponse {
	key: string;
	value: unknown;
	updated_at?: string;
}

export interface AllPreferencesResponse {
	preferences: Record<string, unknown>;
}

// ============================================================================
// Functions
// ============================================================================

/**
 * Get a user preference by key
 */
export async function getPreference<T = unknown>(key: string): Promise<PreferenceResponse | null> {
	try {
		return await api.get<PreferenceResponse>(`/preferences/${key}`);
	} catch (e) {
		// Return null if not found (404)
		if ((e as ApiError).status === 404) {
			return null;
		}
		throw e;
	}
}

/**
 * Set a user preference
 */
export async function setPreference<T = unknown>(key: string, value: T): Promise<PreferenceResponse> {
	return api.put<PreferenceResponse>(`/preferences/${key}`, { key, value });
}

/**
 * Delete a user preference
 */
export async function deletePreference(key: string): Promise<{ deleted: boolean }> {
	return api.delete<{ deleted: boolean }>(`/preferences/${key}`) as Promise<{ deleted: boolean }>;
}

/**
 * Get all user preferences in a single request.
 * More efficient than fetching preferences individually on app startup.
 */
export async function getAllPreferences(): Promise<Record<string, unknown>> {
	try {
		const response = await api.get<AllPreferencesResponse>('/preferences/all');
		return response.preferences;
	} catch (e) {
		console.error('[API] Failed to get all preferences:', e);
		return {};
	}
}
