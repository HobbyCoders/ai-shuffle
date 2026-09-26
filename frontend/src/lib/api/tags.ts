/**
 * Tag API types and functions
 */

import { api } from './client';

// ============================================================================
// Tag Types
// ============================================================================

export interface Tag {
	id: string;
	name: string;
	color: string;
	created_at: string;
	updated_at: string;
}

// Note: SessionTag is defined in sessions.ts since it's part of the Session interface

// ============================================================================
// Tag API Functions
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
