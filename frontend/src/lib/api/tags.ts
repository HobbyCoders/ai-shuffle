/**
 * Tag API types and functions
 */

import { api } from './client-core';

// ============================================================================
// Types
// ============================================================================

export interface Tag {
	id: string;
	name: string;
	color: string;
	created_at: string;
	updated_at: string;
}

// ============================================================================
// Functions
// ============================================================================

/**
 * Get all tags
 */
export async function getTags(): Promise<Tag[]> {
	return api.get<Tag[]>('/tags');
}

/**
 * Create a new tag
 */
export async function createTag(name: string, color: string): Promise<Tag> {
	return api.post<Tag>('/tags', { name, color });
}

/**
 * Update a tag
 */
export async function updateTag(tagId: string, data: { name?: string; color?: string }): Promise<Tag> {
	return api.patch<Tag>(`/tags/${tagId}`, data);
}

/**
 * Delete a tag
 */
export async function deleteTag(tagId: string): Promise<void> {
	await api.delete(`/tags/${tagId}`);
}

/**
 * Get tags for a session
 */
export async function getSessionTags(sessionId: string): Promise<Tag[]> {
	return api.get<Tag[]>(`/tags/session/${sessionId}`);
}

/**
 * Set all tags for a session (replaces existing)
 */
export async function setSessionTags(sessionId: string, tagIds: string[]): Promise<Tag[]> {
	return api.put<Tag[]>(`/tags/session/${sessionId}`, { tag_ids: tagIds });
}

/**
 * Add a tag to a session
 */
export async function addSessionTag(sessionId: string, tagId: string): Promise<void> {
	await api.post(`/tags/session/${sessionId}/${tagId}`);
}

/**
 * Remove a tag from a session
 */
export async function removeSessionTag(sessionId: string, tagId: string): Promise<void> {
	await api.delete(`/tags/session/${sessionId}/${tagId}`);
}
